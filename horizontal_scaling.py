"""
SPEC scaling §5/§6 — Branche HORIZONTAL : catalogue + séquencement des leviers,
puis barrière cannibalisme (a priori) + incrémentalité (a posteriori).

Principe Pinterest : déployer les leviers du MOINS au PLUS risqué en cannibalisme,
UN à la fois, en mesurant entre chaque ; un move n'est validé que s'il AJOUTE des
conversions au COMPTE sans faire chuter le cœur (sinon faux scale → rollback).

Cœur PUR. Réutilise `overlap_score` (§6 a priori). ROAS/CPA web-click (INV-2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from optimizer.config import CONSTANTS, Constants, recommend_daily_budget


# Catalogue ordonné (du moins au plus risqué en cannibalisme) — SPEC §5.
@dataclass(frozen=True)
class Lever:
    name: str
    rank: int
    cannibal_risk: str        # nul | faible | moyen | moyen-élevé
    reuses_elite_creatives: bool
    note: str


LEVER_CATALOGUE: list[Lever] = [
    Lever("CREATIVE_DIVERSITY", 1, "nul", False,
          "inventaire frais sans toucher à l'audience ; combat la fatigue"),
    Lever("KEYWORD_CAMPAIGN", 2, "faible", True,
          "poche d'intention distincte du pool d'intérêt (segment §7)"),
    Lever("ADJACENT_INTEREST", 3, "moyen", True,
          "élargit le prospecting (TARGETED_INTEREST voisins) — vérifier overlap"),
    Lever("FRESH_ACTALIKE", 4, "moyen-élevé", True,
          "nouvelle prospection qualifiée ; source partagée → risque overlap"),
    Lever("NEW_COUNTRY", 5, "faible", True,
          "duplication géo (geo disjoint) si produit/logistique le permettent ; coûteux"),
]
_LEVER_BY_NAME = {lv.name: lv for lv in LEVER_CATALOGUE}


def account_supports_additional_campaign(conv_week_total: float, *,
                                         constants: Constants = CONSTANTS) -> bool:
    """Garde-fou volume : le compte soutient-il une campagne de PLUS à 50 conv/sem ?"""
    return float(conv_week_total or 0) >= 2 * constants.LEARNING_CONV_WEEK


def next_horizontal_lever(
    open_levers: set[str], *, prev_incremental: Optional[bool], conv_week_total: float,
    constants: Constants = CONSTANTS,
) -> Optional[Lever]:
    """
    Sélectionne le PROCHAIN levier à ouvrir (un seul à la fois, dans l'ordre).
    - n'ouvre pas si le compte ne soutient pas +1 campagne à 50 conv/sem ;
    - n'ouvre le levier N+1 que si le levier précédent était incrémental ou neutre
      (`prev_incremental` True/None) ; s'il a échoué (False) → on n'enchaîne pas.
    """
    if not account_supports_additional_campaign(conv_week_total, constants=constants):
        return None
    if open_levers and prev_incremental is False:
        return None
    for lv in LEVER_CATALOGUE:
        if lv.name not in open_levers:
            return lv
    return None


def build_horizontal_move(
    lever: Lever, *, target_cpa: float, n_elite_creatives: int, baseline: dict,
    hypothesis: str = "", constants: Constants = CONSTANTS,
) -> dict:
    """Gabarit commun d'un move horizontal (§5). Budget = incrémental, JAMAIS pris au cœur."""
    budget = recommend_daily_budget(target_cpa, constants=constants)
    return {
        "lever": lever.name, "rank": lever.rank, "cannibal_risk": lever.cannibal_risk,
        "budget": budget, "budget_source": "incrémental dispo",
        "target_cpa": target_cpa,
        # le levier créa (rank 1) vise justement de NOUVELLES créas
        "creatives": "new" if not lever.reuses_elite_creatives else f"top_{n_elite_creatives}_elite",
        "n_creatives": n_elite_creatives,
        "bid": "AUTOMATIC_BID", "hold_days": constants.AGE_LEARNING_DAYS,
        "baseline": baseline, "hypothesis": hypothesis or lever.note,
    }


# --- §6.1 barrière cannibalisme a priori ------------------------------------

def precheck_cannibal(move_spec: dict, core_spec: dict, *,
                      threshold: float = CONSTANTS.CANNIBAL_THRESHOLD) -> tuple[bool, float]:
    """
    Avant lancement : chevauchement move↔cœur. Retourne (bloqué, score). Bloqué si
    overlap_score ≥ seuil (ex. actalike de même source que le cœur).
    """
    from optimizer.diagnostics_account import overlap_score
    score = overlap_score(move_spec, core_spec)
    return (score >= threshold, score)


# --- §6.2 incrémentalité a posteriori (niveau COMPTE) -----------------------

@dataclass
class IncrementalityResult:
    incremental: bool
    core_protected: bool
    validated: bool
    rollback: bool
    account_delta: float
    core_delta: float
    reason: str


def incrementality_check(
    account_conv_before: float, account_conv_after: float,
    core_conv_before: float, core_conv_after: float,
    *, noise_band: float = CONSTANTS.INCREMENTAL_NOISE_BAND,
    tolerance: float = CONSTANTS.CORE_PROTECT_TOLERANCE,
) -> IncrementalityResult:
    """
    §6.2 — un move n'est validé que s'il AJOUTE des conversions au COMPTE (au-delà du
    bruit) ET que le cœur n'a pas chuté de plus de `tolerance`. Sinon → rollback (faux scale).
    Mesure au niveau COMPTE, jamais de l'entité seule.
    """
    incremental = account_conv_after > account_conv_before * (1 + noise_band)
    core_protected = core_conv_after >= core_conv_before * (1 - tolerance)
    validated = incremental and core_protected
    account_delta = round(account_conv_after - account_conv_before, 2)
    core_delta = round(core_conv_after - core_conv_before, 2)
    if validated:
        reason = "incrémental au niveau compte, cœur protégé → vrai scale."
    elif not incremental:
        reason = "le total compte ne grandit pas (≤ bruit) → déplacement, pas de croissance."
    else:
        reason = f"le cœur a chuté (> {tolerance:.0%}) → cannibalisation du cœur."
    return IncrementalityResult(incremental, core_protected, validated, not validated,
                                account_delta, core_delta, reason)
