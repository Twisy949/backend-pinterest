"""
Phase 11 — Validateur bidirectionnel (réf. spec §11.6). DERNIER REMPART.

Rejette une action qui : viole §5 ; cible une entité inexistante ; scale sur un
cluster déjà couvert ; agit pendant le learning (INV-5) ; s'appuie sur un ROAS
blended/view-through (INV-2) ; a un champ requis manquant ; porte une
`confidence` non dérivée.

COÛT DE L'INACTION : si une entité saigne (CPA > KILL_CPA sur fenêtre consolidée
+ tendance confirmée ≥ N jours) sans action corrective → alerte de faux négatif.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from optimizer.config import CONSTANTS, Constants
from optimizer.schemas import ActionType, Candidate

_FORBIDDEN_METRICS = {"roas", "revenue", "roas_blended"}
_CORRECTIVE = {ActionType.KILL, ActionType.BUDGET_DOWN}
# actions sans entity_id existant (création / scoping segment)
_NO_ENTITY = {ActionType.CREATE_CAMPAIGN, ActionType.SPLIT_SEGMENT, ActionType.EXCLUDE_SEGMENT}
# actions qui consomment un budget sur une audience (anti-cannibalisme, Patch B / §6)
_BUDGETED = {ActionType.CREATE_CAMPAIGN, ActionType.RELAUNCH, ActionType.BUDGET_UP,
             ActionType.SPLIT_SEGMENT}
_SCALING = {ActionType.BUDGET_UP, ActionType.RELAUNCH}
_LAUNCH = {ActionType.CREATE_CAMPAIGN, ActionType.RELAUNCH, ActionType.SPLIT_SEGMENT}


@dataclass
class GateContext:
    """Faits du compte pour les Quality Gates nommés (import claude-ads §4)."""
    break_even_cpa: Optional[float] = None
    entity_cpa: dict = field(default_factory=dict)   # {entity_id: cpa réalisé}
    tracking_ok: bool = True                          # P-TR* Critical non FAIL
    diversity_min: int = 3
    cpa_floor_mult: float = 5.0


def _apply_named_gates(c: Candidate, gate: GateContext, constants: Constants) -> list[str]:
    """Gates durs appliqués à chaque reco (claude-ads §4). Retourne les violations."""
    errors: list[str] = []

    # GATE-CAPI : pas d'optimisation si le tracking n'est pas vérifié
    if not gate.tracking_ok:
        errors.append("GATE-CAPI : tracking de conversion non vérifié — aucune optimisation.")

    # GATE-KILL3X : ne jamais scaler une entité à CPA > 3×break-even
    if c.type in _SCALING and gate.break_even_cpa and c.entity_id is not None:
        cpa = gate.entity_cpa.get(str(c.entity_id))
        if cpa is not None and cpa > 3 * gate.break_even_cpa:
            errors.append(f"GATE-KILL3X : CPA {cpa:.0f} € > 3×break-even "
                          f"({3*gate.break_even_cpa:.0f} €) — pauser, ne pas scaler.")

    # GATE-5XCPA : pas de structure dont un AG a un budget < 5×CPA
    p = c.params or {}
    if c.type in _BUDGETED and p.get("daily_budget") is not None and p.get("target_cpa"):
        if float(p["daily_budget"]) < gate.cpa_floor_mult * float(p["target_cpa"]):
            errors.append(f"GATE-5XCPA : budget {p['daily_budget']} € < 5×CPA "
                          f"({gate.cpa_floor_mult*float(p['target_cpa']):.0f} €).")

    # GATE-DIVERSITY : ne pas lancer une audience avec < N concepts créa distincts
    if c.type in _LAUNCH and p.get("n_creatives") is not None:
        if int(p["n_creatives"]) < gate.diversity_min:
            errors.append(f"GATE-DIVERSITY : {p['n_creatives']} concept(s) créa "
                          f"< {gate.diversity_min} requis.")

    return errors


@dataclass
class ValidationResult:
    candidate: Candidate
    ok: bool
    errors: list[str] = field(default_factory=list)


def _metric_clean(metric: Optional[str]) -> bool:
    if not metric:
        return False
    m = metric.lower()
    if m in _FORBIDDEN_METRICS or "view" in m:
        return False
    return "web_click" in m  # doit être une métrique web-click (INV-2)


def validate_candidate(
    c: Candidate,
    *,
    known_entity_ids: Optional[set] = None,
    learning_entity_ids: Optional[set] = None,
    covered_clusters: Optional[set] = None,
    gate_ctx: Optional[GateContext] = None,
    constants: Constants = CONSTANTS,
) -> ValidationResult:
    errors: list[str] = []
    known = known_entity_ids or set()
    learning = learning_entity_ids or set()
    covered = covered_clusters or set()

    # --- champs requis ---
    if not c.type or not c.entity_level:
        errors.append("champ requis manquant : type/entity_level")
    if c.hypothesis is None:
        errors.append("hypothèse falsifiable manquante")
    if c.review_date_t1 is None or c.review_date_t2 is None:
        errors.append("dates de review manquantes")
    if c.confidence is None:
        errors.append("confidence non dérivée (absente)")
    elif not (0.0 <= c.confidence <= 1.0):
        errors.append(f"confidence hors bornes : {c.confidence}")

    # --- ROAS web-click only (INV-2) ---
    if c.hypothesis is not None and not _metric_clean(c.hypothesis.metric):
        errors.append(f"métrique d'hypothèse interdite (blended/view ?) : {c.hypothesis.metric}")

    # --- entité existante ---
    if c.type not in _NO_ENTITY:
        if c.entity_id is None or str(c.entity_id) not in {str(x) for x in known}:
            errors.append(f"entité inexistante : {c.entity_id}")

    # --- pas d'action pendant le learning (INV-5) ---
    if c.entity_id is not None and str(c.entity_id) in {str(x) for x in learning}:
        errors.append("action pendant l'apprentissage (in_learning) interdite (INV-5)")

    # --- garde-fous budget par palier (§5 / INV-6 / INV-7) ---
    if c.type == ActionType.BUDGET_UP:
        pct = (c.params or {}).get("pct")
        if pct is not None and pct > constants.BUDGET_STEP_MAX_PCT + 1e-9:
            errors.append(f"hausse budget > 25% : {pct}")
        cooldown = (c.params or {}).get("cooldown_days")
        patience = (c.params or {}).get("patience_days", constants.PATIENCE_POST_CHANGE)
        high_vol = (c.params or {}).get("high_volume", False)
        if cooldown is not None and cooldown < patience and not high_vol:
            errors.append(f"cooldown {cooldown} < patience {patience} (hors volume élevé)")

    # --- GATE-CANNIBAL : une seule action budgétée par audience (Patch B) ---
    if c.type in _BUDGETED and c.cluster_key and c.cluster_key in covered:
        errors.append(
            f"GATE-CANNIBAL : audiences chevauchantes / budgets additifs sur le même "
            f"cluster : {c.cluster_key}")

    # --- Quality Gates nommés (claude-ads §4) ---
    if gate_ctx is not None:
        errors.extend(_apply_named_gates(c, gate_ctx, constants))

    return ValidationResult(candidate=c, ok=not errors, errors=errors)


@dataclass
class BatchValidation:
    accepted: list[Candidate]
    rejected: list[ValidationResult]


def validate_batch(
    candidates: Iterable[Candidate],
    *,
    known_entity_ids: Optional[set] = None,
    learning_entity_ids: Optional[set] = None,
    gate_ctx: Optional[GateContext] = None,
    constants: Constants = CONSTANTS,
) -> BatchValidation:
    """Valide en lot ; accumule les clusters couverts pour bloquer les doublons."""
    accepted: list[Candidate] = []
    rejected: list[ValidationResult] = []
    covered: set = set()

    for c in candidates:
        res = validate_candidate(
            c, known_entity_ids=known_entity_ids, learning_entity_ids=learning_entity_ids,
            covered_clusters=covered, gate_ctx=gate_ctx, constants=constants)
        if res.ok:
            accepted.append(c)
            if c.type in _BUDGETED and c.cluster_key:
                covered.add(c.cluster_key)
        else:
            rejected.append(res)
    return BatchValidation(accepted=accepted, rejected=rejected)


# --- coût de l'inaction (faux négatif) --------------------------------------

@dataclass
class InactionAlert:
    entity_level: str
    entity_id: str
    entity_name: str
    cpa_web_click: float
    kill_cpa: float
    trend_days: int
    message: str


def detect_inaction(
    bleeding_entities: Iterable[dict],
    candidates: Iterable[Candidate],
    kill_cpa: float,
    *,
    min_trend_days: int = 3,
) -> list[InactionAlert]:
    """
    `bleeding_entities` : dicts {entity_level, entity_id, entity_name, metrics
    (EntityMetrics), trend_days_bleeding}. Alerte si CPA > KILL_CPA + tendance
    confirmée et AUCUNE action corrective ne couvre l'entité.
    """
    covered = {
        str(c.entity_id) for c in candidates
        if c.type in _CORRECTIVE and c.entity_id is not None
    }
    alerts: list[InactionAlert] = []
    for e in bleeding_entities:
        m = e["metrics"]
        cpa = m.cpa_web_click
        trend = int(e.get("trend_days_bleeding", 0))
        if cpa is None or cpa <= kill_cpa or trend < min_trend_days:
            continue
        if str(e["entity_id"]) in covered:
            continue
        alerts.append(InactionAlert(
            entity_level=e["entity_level"], entity_id=str(e["entity_id"]),
            entity_name=e.get("entity_name", ""), cpa_web_click=round(cpa, 2),
            kill_cpa=round(kill_cpa, 2), trend_days=trend,
            message=(f"INACTION : {e.get('entity_name','?')} saigne "
                     f"(CPA {cpa:.0f} > KILL {kill_cpa:.0f}, {trend} j) sans action "
                     f"corrective — faux négatif probable."),
        ))
    return alerts
