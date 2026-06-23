"""
Phase 2 — Économie & config (réf. spec §4).

Constantes de cadence/volume + dérivation de l'économie unitaire à partir de la
marge contributive et de l'historique.

INV-1 : si `MARGIN_CONTRIB` est absente, le mode décision est bloqué — aucune
économie dérivée, aucun candidat produit. `require_margin()` lève
`MissingMarginError`, que la génération de candidats laisse remonter.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable, Optional, Union


class MissingMarginError(RuntimeError):
    """Levée quand une décision est demandée sans `MARGIN_CONTRIB` (INV-1)."""


# --- constantes de cadence / volume / structure (spec §4) -------------------

@dataclass(frozen=True)
class Constants:
    BUDGET_FLOOR_X_CPA: float = 5.0      # budget plancher = 5 × CPA cible
    MIN_ADS_PER_ADGROUP: int = 10
    BUDGET_STEP_MAX_PCT: float = 0.25    # hausse budget max par palier (INV-7)
    PATIENCE_POST_CHANGE: int = 7        # jours d'attente après un changement
    BUDGET_STEP_COOLDOWN: int = 7        # cooldown entre paliers budget (INV-6)
    LEARNING_CONV_WEEK: int = 50         # conv/sem pour sortir du learning structurel
    DECISION_LAG_DAYS: int = 2           # exclut J et J-1 (INV-3)
    REVIEW_HORIZON_T1: int = 7
    REVIEW_HORIZON_T2: int = 18          # 2e review (traîne d'attribution)
    SYNC_STALE_HOURS: int = 36           # au-delà : données périmées (gate)
    ATTRIBUTION_WINDOW: str = "7d_click"
    CONSOLIDATED_WINDOW_DAYS: int = 7
    HIGH_VOLUME_CONV_WEEK: int = 100     # branche "volume élevé" (déroge cooldown)
    MIN_CONV_TRUST: int = 3              # gate de fiabilité d'une créa (web-click conv)
    CREATIVES_PER_CONSOLIDATED_ADGROUP: int = 6   # top N créas réutilisées (borne 5-8)
    RELAUNCH_CONCENTRATION_MIN: int = 5  # créas gagnantes dans 1 ad_group → relancer
    # --- Cold-start = UNE seule campagne de redémarrage (Patch #3) ---
    CREATIVES_PER_COLDSTART: int = 6     # top N absolu du compte (borne 5-8)
    AD_GROUPS_PER_COLDSTART: int = 1     # la Découverte splitte plus tard si prouvé
    MIN_CREATIVES_KEPT: int = 5          # GATE-DIVERSITY (plancher de créas)
    MIN_CONV_TRUST_COLDSTART: int = 10   # fiabilité minimum par créa (cold-start)
    # --- Segmentation par targeting_types (SPEC v2 §10, défauts prudents) ---
    MATERIAL_DELTA: float = 0.5          # écart ROAS matériel vs baseline (§4)
    MIN_CONV_SEGMENT: int = 20           # conv web-click mini pour juger un segment (15-30)
    MAX_LAYERS: int = 2                  # profondeur max de superposition (§4.1)
    PINNER_MIN_COVERAGE: float = 0.3     # sous ce seuil, PINNER_INTEREST ignoré (§3/§5.2)
    FDR_ALPHA: float = 0.10              # seuil Benjamini-Hochberg (tests multiples, §4)
    # --- Posés pour la passe B (§6/§7) ---
    KEYWORD_MIN: int = 25                # keywords mini par AG KEYWORD (Pinterest, §5.1/§7)
    KEYWORD_SCALE_BUDGET: float = 150.0  # budget/jour déclenchant le levier KEYWORD (§7)
    CANNIBAL_THRESHOLD: float = 0.5      # seuil de chevauchement inter-campagnes (§6)
    # --- Stratégie de scale vertical/horizontal (SPEC scaling §10) ---
    EVAL_WINDOW: int = 7                 # jours de stabilité requis avant scale (§2)
    STABILITY_BAND: float = 0.15         # ±15 % de CV du CPA = stable (§2)
    MIN_CONV_SCALE: int = 40             # conv mini pour autoriser un scale (§2)
    SATURATION_CPA_MULT: float = 1.3     # CPA marginal / CPA moyen → horizontal (§3)
    CORE_PROTECT_TOLERANCE: float = 0.10 # le cœur ne doit pas chuter de plus de 10 % (§6.2)
    INCREMENTAL_NOISE_BAND: float = 0.05 # bande de bruit de l'incrémentalité compte (§6.2)
    AGE_LEARNING_DAYS: int = 14          # durée de hold d'un move horizontal (sortie learning)


# Pondérations du score d'élite créatif (somme = 1.0) — surchargeables par la Découverte.
# (clé `roas_web_click` et non `roas` : INV-2 interdit le token brut `roas`.)
# Rentabilité (ROAS + CPA) = 0.60 du score : signal dominant.
CREATIVE_SCORE_WEIGHTS = {
    "roas_web_click": 0.40, "cpa_web_click": 0.20, "volume": 0.15,
    "save_rate": 0.13, "hook": 0.07, "retention": 0.05,
}
assert abs(sum(CREATIVE_SCORE_WEIGHTS.values()) - 1.0) < 1e-9

# Score d'audience (somme = 1.0) — au-delà du ROAS : volume, fiabilité (trust),
# potentiel (meilleur sous-ensemble). Évite d'écarter une grosse audience (US) à
# ROAS moyen au profit d'une micro-audience à ROAS élevé mais sans volume.
AUDIENCE_SCORE_WEIGHTS = {
    "volume": 0.30, "trust": 0.25, "roas_web_click": 0.25, "potential": 0.20,
}
assert abs(sum(AUDIENCE_SCORE_WEIGHTS.values()) - 1.0) < 1e-9


def discovery_overrides(params: dict) -> dict:
    """
    Hook couture Découverte : si la couche Découverte est active, ses paramètres
    priment sur les défauts cold-start. Aujourd'hui no-op (Découverte non construite).
    """
    return dict(params)


def normalize(value: Optional[float], floor: float, ceiling: float) -> float:
    """Normalise dans [0,1] entre floor et ceiling (clamp). Déterministe."""
    if value is None:
        return 0.0
    if ceiling <= floor:
        return 1.0 if value >= ceiling else 0.0
    return max(0.0, min(1.0, (value - floor) / (ceiling - floor)))


def normalize_low(value: Optional[float], best: float, worst: float) -> float:
    """Normalise dans [0,1] où BAS = mieux (best→1, worst→0). Déterministe."""
    if value is None:
        return 0.0
    if worst <= best:
        return 1.0 if value <= best else 0.0
    return max(0.0, min(1.0, (worst - value) / (worst - best)))


CONSTANTS = Constants()


# --- économie unitaire dérivée ----------------------------------------------

@dataclass(frozen=True)
class Economics:
    margin_contrib: float
    avg_basket: float
    break_even_cpa: float
    break_even_roas: float
    target_scale_cpa: float
    target_scale_roas: float


def require_margin(margin: Optional[float]) -> float:
    """Garde dur INV-1. Retourne la marge si valide, sinon lève."""
    if margin is None:
        raise MissingMarginError(
            "MARGIN_CONTRIB non configurée : le mode décision est bloqué "
            "(INV-1). Renseignez la marge contributive (0–1) pour produire "
            "des candidats."
        )
    if not (0.0 < margin <= 1.0):
        raise MissingMarginError(
            f"MARGIN_CONTRIB invalide ({margin!r}) : attendu dans ]0, 1]."
        )
    return margin


def _derive_avg_basket(history: Union[dict, Iterable[dict], None]) -> Optional[float]:
    """AOV = Σ web_click_checkout_value / Σ web_click_checkout (spec §4, ≈53€)."""
    if history is None:
        return None
    if isinstance(history, dict):
        value = float(history.get("web_click_checkout_value", 0.0) or 0.0)
        count = float(history.get("web_click_checkout", 0.0) or 0.0)
    else:
        value = 0.0
        count = 0.0
        for row in history:
            value += float(row.get("web_click_checkout_value", 0.0) or 0.0)
            count += float(row.get("web_click_checkout", 0.0) or 0.0)
    return value / count if count > 0 else None


def derive_economics(
    margin: Optional[float],
    history: Union[dict, Iterable[dict], None] = None,
    *,
    avg_basket: Optional[float] = None,
    scale_safety: float = 0.8,
) -> Economics:
    """
    Dérive l'économie unitaire. `scale_safety` ∈ ]0,1] : marge de sécurité pour
    les cibles de scaling (0.8 = viser un CPA 20 % sous le break-even).

    Lève `MissingMarginError` si la marge est absente/invalide (INV-1).
    """
    m = require_margin(margin)
    aov = avg_basket if avg_basket is not None else _derive_avg_basket(history)
    if aov is None or aov <= 0:
        raise ValueError(
            "AVG_BASKET indisponible : fournissez `avg_basket` ou un `history` "
            "contenant des web_click_checkout(_value)."
        )
    break_even_cpa = aov * m
    break_even_roas = 1.0 / m
    return Economics(
        margin_contrib=m,
        avg_basket=round(aov, 2),
        break_even_cpa=round(break_even_cpa, 2),
        break_even_roas=round(break_even_roas, 3),
        target_scale_cpa=round(break_even_cpa * scale_safety, 2),
        target_scale_roas=round(break_even_roas / scale_safety, 3),
    )


# --- config runtime (inputs humains, spec §17) ------------------------------

def anchor_target_cpa(
    creative_cpas: Iterable[Optional[float]],
    *,
    break_even_cpa: Optional[float] = None,
    margin: Optional[float] = None,
    phase: str = "TESTING",
) -> Optional[float]:
    """
    Patch D — ancre le CPA cible sur l'économie PROUVÉE, jamais sur le blended.

    - anchor = médiane du CPA web-click des créas retenues (~11-14 €) ;
    - si break-even connu : target = min(anchor, break_even) ;
      en SCALING avec marge : break-even *= (1 - marge) (plus exigeant) ;
    - sans marge : on garde l'ancrage créa pur (provisoire) — pas de fallback blended.
    """
    cpas = sorted(c for c in creative_cpas if c is not None and c > 0)
    anchor = median(cpas) if cpas else None
    if break_even_cpa is not None:
        be = break_even_cpa * (1 - margin) if (phase == "SCALING" and margin is not None) else break_even_cpa
        if anchor is None:
            return round(be, 2)
        return round(min(anchor, be), 2)
    return round(anchor, 2) if anchor is not None else None


def recommend_daily_budget(target_cpa: float, *, conv_week: int = None,
                           constants: "Constants" = None) -> float:
    """Budget/jour = max(5×CPA, (conv_week/7)×CPA). Avec CPA≈13 € → ≈93 €/jour."""
    c = constants or CONSTANTS
    cw = conv_week if conv_week is not None else c.LEARNING_CONV_WEEK
    floor = c.BUDGET_FLOOR_X_CPA * target_cpa
    aim = target_cpa * cw / 7.0
    return round(max(floor, aim), 2)


@dataclass
class OptimizerConfig:
    margin_contrib: Optional[float] = None        # §17.1 — bloque décision si None
    total_budget_target: Optional[float] = None   # §17.2
    scale_safety: float = 0.8
    constants: Constants = field(default_factory=lambda: CONSTANTS)

    @property
    def decision_ready(self) -> bool:
        try:
            require_margin(self.margin_contrib)
            return True
        except MissingMarginError:
            return False


def _env_float(name: str) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def load_config() -> OptimizerConfig:
    """Charge la config depuis l'environnement (MARGIN_CONTRIB, TOTAL_BUDGET_TARGET)."""
    return OptimizerConfig(
        margin_contrib=_env_float("MARGIN_CONTRIB"),
        total_budget_target=_env_float("TOTAL_BUDGET_TARGET"),
    )
