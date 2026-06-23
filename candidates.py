"""
Phase 8 — Génération de candidats (réf. spec §11). CŒUR DÉTERMINISTE.

Un générateur par type d'action, chacun émet un `Candidate` (schéma v2 §11.1)
porteur d'une hypothèse falsifiable + dates de review. Le LLM ne calcule jamais
de métrique et ne pose jamais `confidence` (INV-4, voir llm_layer).

Invariants encodés ici :
  INV-1 : pas de candidat si MARGIN_CONTRIB absente (require_margin en tête).
  INV-6 : cooldown ≥ patience (sauf branche volume élevé).
  INV-7 : hausse budget ≤ 25 % par palier (cap_budget_step).
  INV-8 : objectif lu sur l'entité, jamais hardcodé SALES.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from optimizer.config import CONSTANTS, Constants, Economics, require_margin, OptimizerConfig
from optimizer.schemas import (
    ActionType, Candidate, EntityMetrics, Hypothesis, Level, Phase,
)


# --- contexte d'une entité diagnostiquée ------------------------------------

@dataclass
class EntityContext:
    level: str
    entity_id: str
    entity_name: str
    metrics: EntityMetrics
    objective: str = "WEB_CONVERSION"          # INV-8 : lu, jamais supposé
    status: str = "ACTIVE"
    daily_budget: float = 0.0
    target_cpa: float = 18.0
    phase: Optional[str] = None
    gate_ready: bool = True
    in_learning: bool = False
    score: dict = field(default_factory=dict)
    prev: Optional[EntityMetrics] = None
    cluster_key: Optional[str] = None
    calibration: Optional[dict] = None


# --- structure recommandée (v2 §11.3) ---------------------------------------

def recommend_structure(
    budget: float, target_cpa: float, conv_week: float,
    *, constants: Constants = CONSTANTS,
) -> dict:
    """
    Combien d'ad groups / ads / budget par ad group. Sous 50 conv/sem → 1 ad
    group (on consolide pour sortir du learning, spec §11.3 / §14).
    """
    budget_floor = constants.BUDGET_FLOOR_X_CPA * target_cpa
    if conv_week < constants.LEARNING_CONV_WEEK:
        n_adgroups = 1
    else:
        # chaque ad group doit pouvoir atteindre ~50 conv/sem ET être nourri
        by_volume = int(conv_week // constants.LEARNING_CONV_WEEK)
        by_budget = int(budget // budget_floor) if budget_floor > 0 else 1
        n_adgroups = max(1, min(by_volume, by_budget))
    return {
        "n_adgroups": n_adgroups,
        "n_ads_per_adgroup": constants.MIN_ADS_PER_ADGROUP,
        "budget_per_adgroup": round(budget / n_adgroups, 2) if n_adgroups else budget,
        "budget_floor": budget_floor,
    }


# --- garde-fous budget (INV-7) ----------------------------------------------

def cap_budget_step(
    current: float, *, desired_pct: Optional[float] = None,
    desired_amount: Optional[float] = None, max_pct: float = CONSTANTS.BUDGET_STEP_MAX_PCT,
) -> tuple[float, float]:
    """Plafonne une hausse de budget à `max_pct` (défaut 25 %). Retourne (new, pct)."""
    if current <= 0:
        target = desired_amount or 0.0
        return round(target, 2), 1.0
    cap = current * (1 + max_pct)
    if desired_amount is not None:
        target = desired_amount
    else:
        target = current * (1 + (desired_pct if desired_pct is not None else max_pct))
    new = min(target, cap)
    return round(new, 2), round((new - current) / current, 4)


# --- hypothèse + dates de review --------------------------------------------

def _review_dates(today: date, constants: Constants) -> tuple[date, date]:
    return (today + timedelta(days=constants.REVIEW_HORIZON_T1),
            today + timedelta(days=constants.REVIEW_HORIZON_T2))


def _hypothesis(action: str, ctx: EntityContext, eco: Economics,
                constants: Constants) -> Hypothesis:
    roas = ctx.metrics.roas_web_click
    cpa = ctx.metrics.cpa_web_click
    if action in (ActionType.BUDGET_UP, ActionType.RELAUNCH, ActionType.CREATE_CAMPAIGN):
        return Hypothesis("roas_web_click", "7d_consolidated", baseline=roas,
                          expected=eco.target_scale_roas, min_acceptable=eco.break_even_roas,
                          horizon_t1=constants.REVIEW_HORIZON_T1, horizon_t2=constants.REVIEW_HORIZON_T2)
    if action in (ActionType.KILL, ActionType.BUDGET_DOWN):
        return Hypothesis("account_roas_web_click", "7d_consolidated", baseline=roas,
                          expected=None, min_acceptable=None,
                          horizon_t1=constants.REVIEW_HORIZON_T1, horizon_t2=constants.REVIEW_HORIZON_T2)
    return Hypothesis("cpa_web_click", "7d_consolidated", baseline=cpa,
                      expected=eco.target_scale_cpa, min_acceptable=eco.break_even_cpa,
                      horizon_t1=constants.REVIEW_HORIZON_T1, horizon_t2=constants.REVIEW_HORIZON_T2)


# --- confiance DÉRIVÉE (jamais posée par le LLM, INV-4) ----------------------

def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def derive_confidence(
    candidate: Candidate, metrics: EntityMetrics,
    *, prev: Optional[EntityMetrics] = None, memory: Optional[dict] = None,
    full_conf_conv: int = 30,
) -> tuple[float, str]:
    """
    Confiance = IC sur le CPA (∝ volume) × netteté de tendance × taux de succès
    des actions similaires (account_heuristics). Déterministe & reproductible.
    `memory` : {pattern_key: {"n_trials": int, "n_success": int}}.
    """
    conv = metrics.web_click_checkout
    conf_vol = _clamp(conv / full_conf_conv)        # plus de conversions → IC resserré

    if prev is not None and prev.roas_web_click and metrics.roas_web_click is not None:
        delta = abs(metrics.roas_web_click - prev.roas_web_click) / prev.roas_web_click
        conf_trend = _clamp(0.4 + delta)            # tendance nette → plus sûr
    else:
        conf_trend = _clamp(metrics.n_days / 14.0)

    pattern = f"{candidate.type}_{candidate.phase or 'NA'}"
    stats = (memory or {}).get(pattern, {})
    n_t = int(stats.get("n_trials", 0))
    n_s = int(stats.get("n_success", 0))
    conf_mem = (n_s + 1) / (n_t + 2)                # moyenne de Beta (lissage Laplace)

    confidence = round(0.5 * conf_vol + 0.2 * conf_trend + 0.3 * conf_mem, 3)
    basis = (f"vol={conf_vol:.2f}(n={conv:.0f}) × trend={conf_trend:.2f} × "
             f"mem={conf_mem:.2f}(s{n_s}/t{n_t}) [{pattern}]")
    return confidence, basis


# --- générateurs ------------------------------------------------------------

def _mk(action: str, ctx: EntityContext, eco: Economics, today: date,
        constants: Constants, *, params: dict, rationale: str,
        account_check: str = "") -> Candidate:
    t1, t2 = _review_dates(today, constants)
    return Candidate(
        type=action, entity_level=ctx.level, entity_id=ctx.entity_id,
        entity_name=ctx.entity_name, params=params, phase=ctx.phase,
        rationale=rationale, account_check=account_check,
        hypothesis=_hypothesis(action, ctx, eco, constants),
        review_date_t1=t1, review_date_t2=t2, cluster_key=ctx.cluster_key,
    )


def gen_scale(ctx, eco, today, constants) -> list[Candidate]:
    roas = ctx.metrics.roas_web_click
    if ctx.phase != Phase.SCALING or not ctx.gate_ready or roas is None:
        return []
    if roas < eco.target_scale_roas:
        return []
    weeks = max(ctx.metrics.n_days / 7.0, 1.0)
    conv_week = ctx.metrics.web_click_checkout / weeks
    high_volume = conv_week >= constants.HIGH_VOLUME_CONV_WEEK
    new_budget, pct = cap_budget_step(ctx.daily_budget, desired_pct=constants.BUDGET_STEP_MAX_PCT)
    cooldown = 3 if high_volume else constants.BUDGET_STEP_COOLDOWN  # INV-6 (dérogation volume)
    params = {
        "from": ctx.daily_budget, "to": new_budget, "pct": pct,
        "cooldown_days": cooldown, "patience_days": constants.PATIENCE_POST_CHANGE,
        "high_volume": high_volume, "conv_week": round(conv_week, 1),
    }
    return [_mk(ActionType.BUDGET_UP, ctx, eco, today, constants, params=params,
               rationale=(f"SCALING : ROAS web-click {roas:.2f} ≥ cible {eco.target_scale_roas} "
                          f"— hausse budget +{pct*100:.0f}% (palier)."),
               account_check="vérifier que le cluster n'est pas déjà couvert (anti-cannibalisme).")]


def gen_kill(ctx, eco, today, constants) -> list[Candidate]:
    if ctx.score.get("verdict") != "KILL" or ctx.score.get("save_guard"):
        return []
    return [_mk(ActionType.KILL, ctx, eco, today, constants,
               params={"reason": "verdict KILL", "cpa": ctx.metrics.cpa_web_click},
               rationale=(f"KILL : verdict perf KILL (P={ctx.score.get('P')}), "
                          f"CPA {ctx.metrics.cpa_web_click} — saigne sans signal de save."),
               account_check="confirmer que tuer ne détruit pas le volume du compte.")]


def gen_budget_down(ctx, eco, today, constants) -> list[Candidate]:
    """CPA au-dessus du kill mais save fort (anti-KILL) → réduire, pas tuer."""
    if not ctx.score.get("save_guard"):
        return []
    cpa = ctx.metrics.cpa_web_click
    if cpa is None or cpa <= eco.break_even_cpa:
        return []
    new_budget = round(ctx.daily_budget * 0.7, 2)
    return [_mk(ActionType.BUDGET_DOWN, ctx, eco, today, constants,
               params={"from": ctx.daily_budget, "to": new_budget, "pct": -0.30},
               rationale=(f"CPA {cpa} > break-even {eco.break_even_cpa} mais save-rate fort "
                          f"— réduire le budget (-30%) plutôt que tuer (traîne d'attribution)."))]


def gen_underpowered(ctx, eco, today, constants) -> list[Candidate]:
    if ctx.phase != Phase.UNDERPOWERED:
        return []
    floor = constants.BUDGET_FLOOR_X_CPA * ctx.target_cpa
    if ctx.daily_budget >= floor:
        return []
    new_budget, pct = cap_budget_step(ctx.daily_budget, desired_amount=floor)
    params = {
        "from": ctx.daily_budget, "to": new_budget, "pct": pct, "floor": floor,
        "cooldown_days": constants.BUDGET_STEP_COOLDOWN,
        "patience_days": constants.PATIENCE_POST_CHANGE,
        "note": "plusieurs paliers nécessaires pour atteindre le plancher (≤25%/palier).",
    }
    return [_mk(ActionType.BUDGET_UP, ctx, eco, today, constants, params=params,
               rationale=(f"UNDERPOWERED : budget {ctx.daily_budget:.0f}€ sous le plancher "
                          f"{floor:.0f}€ (=5×CPA) — remonter par paliers de 25%."))]


def gen_change_bid(ctx, eco, today, constants) -> list[Candidate]:
    """
    Patch C — passage en bid MANUEL : uniquement comme plafond de coût TARDIF.
    Conditions strictes : SCALING, hors learning, ≥ 50 conv/sem, ET CPA réalisé
    qui dérive AU-DESSUS du break-even. Jamais au cold-start / 1re campagne
    (le cold-start n'appelle pas ce générateur ; ici l'entité est ACTIVE prouvée).
    """
    if ctx.phase != Phase.SCALING or ctx.in_learning or not ctx.gate_ready:
        return []
    weeks = max(ctx.metrics.n_days / 7.0, 1.0)
    conv_week = ctx.metrics.web_click_checkout / weeks
    cpa = ctx.metrics.cpa_web_click
    if conv_week < constants.LEARNING_CONV_WEEK:
        return []
    if cpa is None or cpa <= eco.break_even_cpa:
        return []
    return [_mk(ActionType.CHANGE_BID_STRATEGY, ctx, eco, today, constants,
               params={"to": "MANUAL_BID", "cost_cap": eco.break_even_cpa,
                       "from": "AUTOMATIC_BID", "conv_week": round(conv_week, 1)},
               rationale=(f"CPA réalisé {cpa} € au-dessus du break-even {eco.break_even_cpa} € "
                          f"sur une entité prouvée (SCALING, {conv_week:.0f} conv/sem) — poser "
                          f"un plafond de coût dur (bid manuel)."),
               account_check="plafond de coût tardif, jamais au démarrage.")]


_GENERATORS = (gen_scale, gen_kill, gen_budget_down, gen_underpowered, gen_change_bid)


def generate_candidates(
    ctx: EntityContext,
    config: OptimizerConfig,
    economics: Economics,
    *,
    today: Optional[date] = None,
    memory: Optional[dict] = None,
    constants: Constants = CONSTANTS,
) -> list[Candidate]:
    """
    Produit les candidats déterministes pour une entité diagnostiquée.

    INV-1 : lève MissingMarginError si MARGIN_CONTRIB est absente.
    Une entité en pause ne produit AUCUNE décision live (→ cold-start, Phase 13).
    """
    require_margin(config.margin_contrib)          # INV-1
    today = today or date.today()

    if ctx.status != "ACTIVE":
        return []                                   # pas de décision live sur entité en pause
    if ctx.in_learning:
        return []                                   # INV-5 amont : rien pendant le learning

    out: list[Candidate] = []
    for gen in _GENERATORS:
        out.extend(gen(ctx, economics, today, constants))

    for c in out:                                   # confiance DÉRIVÉE (INV-4)
        c.confidence, c.confidence_basis = derive_confidence(
            c, ctx.metrics, prev=ctx.prev, memory=memory)
    return out
