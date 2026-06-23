"""
Orchestrateur — assemble les phases en un pipeline exécutable depuis la DB.

Flux : économie (INV-1) → calibration → diagnostic → candidats → arbitrage →
validation → (alerte d'inaction). Si le compte est froid (0 active) → cold-start.

Adaptateurs minces ORM → structures du cœur déterministe (testable sans DB).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from optimizer.calibration import compute_calibration
from optimizer.candidates import EntityContext, generate_candidates
from optimizer.arbitration import arbitrate
from optimizer.cold_start import account_is_cold, cold_start_plan
from optimizer.config import OptimizerConfig, derive_economics, require_margin, load_config
from optimizer.diagnostics_entity import detect_phase, learning_gating, score_entity
from optimizer.metrics_access import aggregate, as_date, consolidated_window
from optimizer.schemas import EntityMetrics
from optimizer.validator import detect_inaction, validate_batch

logger = logging.getLogger(__name__)

_METRIC_FIELDS = (
    "spend", "impressions", "clicks", "saves", "outbound_clicks",
    "web_click_checkout", "web_view_checkout", "web_click_checkout_value",
    "add_to_cart", "video_3sec_views", "video_p100_complete", "impression_frequency",
)


def _metric_rows(rows) -> list[dict]:
    out = []
    for m in rows:
        d = {"date": getattr(m, "date", None)}
        for f in _METRIC_FIELDS:
            d[f] = getattr(m, f, 0) or 0
        out.append(d)
    return out


def _agg_dict(em: EntityMetrics) -> dict:
    return {
        "spend": em.spend, "web_click_checkout": em.web_click_checkout,
        "web_click_checkout_value": em.web_click_checkout_value,
        "roas_web_click": em.roas_web_click, "cpa_web_click": em.cpa_web_click,
        "n_days": em.n_days,
    }


def load_account(db) -> tuple[list[dict], list[dict]]:
    """Charge campagnes + ad groups avec agrégats full-history (depuis l'ORM)."""
    from models import AdGroup, AdGroupMetric, Campaign, Metric

    metrics_by_campaign: dict[str, list] = {}
    for m in db.query(Metric).all():
        metrics_by_campaign.setdefault(m.campaign_id, []).append(m)
    metrics_by_adgroup: dict[str, list] = {}
    for m in db.query(AdGroupMetric).all():
        metrics_by_adgroup.setdefault(m.ad_group_id, []).append(m)

    campaigns = []
    for c in db.query(Campaign).all():
        em = aggregate(_metric_rows(metrics_by_campaign.get(c.id, [])), level="campaign", entity_id=c.id)
        campaigns.append({"id": c.id, "name": c.name, "status": c.status,
                          "objective": c.objective, "daily_budget": c.daily_budget or 0.0,
                          "agg": _agg_dict(em)})

    ad_groups = []
    for g in db.query(AdGroup).all():
        em = aggregate(_metric_rows(metrics_by_adgroup.get(g.id, [])), level="ad_group", entity_id=g.id)
        ad_groups.append({"id": g.id, "campaign_id": g.campaign_id, "name": g.name,
                          "status": g.status, "daily_budget": g.daily_budget or 0.0,
                          "bid_strategy": g.bid_strategy, "learning_mode": g.learning_mode,
                          "start_time": g.start_time, "optimization_goal": g.optimization_goal,
                          "targeting_spec": g.targeting_spec, "agg": _agg_dict(em),
                          "_metrics": metrics_by_adgroup.get(g.id, [])})
    return campaigns, ad_groups


def load_ads(db) -> list[dict]:
    """Charge les ads avec agrégats web-click full-history (pour la sélection créa)."""
    from models import Ad, AdGroup, AdMetric
    by_ad: dict[str, list] = {}
    for m in db.query(AdMetric).all():
        by_ad.setdefault(m.ad_id, []).append(m)
    camp_of_ag = {g.id: g.campaign_id for g in db.query(AdGroup).all()}
    out = []
    for a in db.query(Ad).all():
        rows = by_ad.get(a.id, [])
        em = aggregate(_metric_rows(rows), level="ad", entity_id=a.id)
        vp75 = sum(float(getattr(m, "video_p75", 0) or 0) for m in rows)
        out.append({
            "id": a.id, "ad_group_id": a.ad_group_id,
            "campaign_id": camp_of_ag.get(a.ad_group_id), "pin_id": a.pin_id, "name": a.name,
            "agg": {"spend": em.spend, "web_click_checkout": em.web_click_checkout,
                    "web_click_checkout_value": em.web_click_checkout_value,
                    "impressions": em.impressions, "saves": em.saves,
                    "video_3sec_views": em.video_3sec_views,
                    "video_p100_complete": em.video_p100_complete, "video_p75": vp75},
        })
    return out


def _has_conv_tag(ad_group: dict) -> bool:
    from optimizer.audit import _conv_tag_id
    return bool(_conv_tag_id(ad_group))


def build_audit_input(db, config: Optional[OptimizerConfig] = None):
    """Assemble le contexte d'audit depuis la DB (adaptateur ORM → AuditInput)."""
    from optimizer.audit import AuditInput
    from optimizer.diagnostics_account import build_clusters
    config = config or load_config()
    campaigns, ad_groups = load_account(db)
    ads = load_ads(db)

    economics = None
    if config.margin_contrib is not None:
        history = {
            "web_click_checkout_value": sum(c["agg"]["web_click_checkout_value"] for c in campaigns),
            "web_click_checkout": sum(c["agg"]["web_click_checkout"] for c in campaigns),
        }
        try:
            economics = derive_economics(config.margin_contrib, history)
        except (ValueError, RuntimeError):
            economics = None

    calibration = {r["key"]: r["value"] for r in compute_calibration(campaigns)}
    target_cpa = (calibration.get("MEDIAN_CPA", {}) or {}).get("value")
    if economics is not None:
        target_cpa = economics.break_even_cpa if target_cpa is None else target_cpa
    clusters = build_clusters(
        [{"id": g["id"], "targeting_spec": g.get("targeting_spec")} for g in ad_groups],
        threshold=0.3, level="ad_group")

    creative_attrs = 0
    try:
        from models import CreativeAttribute
        creative_attrs = db.query(CreativeAttribute).count()
    except Exception:
        creative_attrs = 0

    return AuditInput(
        campaigns=campaigns, ad_groups=ad_groups, ads=ads, clusters=clusters,
        break_even_cpa=economics.break_even_cpa if economics else None,
        target_cpa=target_cpa, creative_attributes_count=creative_attrs,
        total_budget_target=config.total_budget_target,
    )


def run_account_audit(db, config: Optional[OptimizerConfig] = None) -> dict:
    """Audit Santé déterministe du compte (score 0-100 + findings + quick wins)."""
    from optimizer.audit import run_audit
    return run_audit(build_audit_input(db, config))


def run(db, config: Optional[OptimizerConfig] = None, today: Optional[date] = None) -> dict:
    """Exécute le pipeline complet ; renvoie un dict sérialisable."""
    config = config or load_config()
    today = today or date.today()

    campaigns, ad_groups = load_account(db)
    history = {
        "web_click_checkout_value": sum(c["agg"]["web_click_checkout_value"] for c in campaigns),
        "web_click_checkout": sum(c["agg"]["web_click_checkout"] for c in campaigns),
    }
    # économie dérivée seulement si la marge est connue (sinon None → provisoire au cold-start)
    economics = None
    if config.margin_contrib is not None:
        try:
            economics = derive_economics(config.margin_contrib, history)
        except (ValueError, RuntimeError):
            economics = None

    # --- compte froid (P-ST5 FAIL) → cold-start : 1 prescription (Patch #3) ---
    if account_is_cold(campaigns):
        ads = load_ads(db)
        # P-TR1 (GATE-CAPI) : tag de conversion présent ET achats web-click remontés
        tr1_status = "PASS" if (sum(c["agg"]["web_click_checkout"] for c in campaigns) > 0
                                and any(_has_conv_tag(g) for g in ad_groups)) else "FAIL"
        plan = cold_start_plan(campaigns, ad_groups, ads, config, economics,
                               tr1_status=tr1_status, today=today)
        return {
            "ok": True, "mode": "cold_start",
            "margin_configured": plan.margin_configured,
            "provisional": not plan.margin_configured,
            "blocked": plan.blocked, "alerts": plan.alerts,
            "economics": economics.__dict__ if economics else None,
            "notes": plan.notes, "pool_size": plan.pool_size, "n_eligible": plan.n_eligible,
            "candidates": [c.to_dict() for c in plan.candidates],
        }

    # --- compte actif → décision live : INV-1 strict ---
    try:
        require_margin(config.margin_contrib)
    except Exception as e:
        return {"ok": False, "decision_ready": False, "reason": str(e)}
    if economics is None:
        return {"ok": False, "reason": "historique insuffisant pour dériver l'AOV."}

    calib_rows = compute_calibration(campaigns)
    calibration = {r["key"]: r["value"] for r in calib_rows}

    # --- compte actif → diagnostic + candidats live ---
    target_cpa = (calibration.get("MEDIAN_CPA", {}) or {}).get("value") or economics.break_even_cpa
    all_cands = []
    known_ids, learning_ids, bleeders = set(), set(), []
    entity_cpa: dict = {}
    for c in campaigns:
        known_ids.add(str(c["id"]))
        if (c["status"] or "").upper() != "ACTIVE":
            continue
        rows = []
        for g in ad_groups:
            if str(g["campaign_id"]) == str(c["id"]):
                rows += _metric_rows(g["_metrics"])
        win = consolidated_window(today)
        em = aggregate(rows, level="campaign", entity_id=c["id"], window=win)
        if em.cpa_web_click is not None:
            entity_cpa[str(c["id"])] = em.cpa_web_click
        score = score_entity(em, economics, level="campaign", calibration=calibration)
        phase = detect_phase(em, economics, c["daily_budget"], target_cpa)
        ctx = EntityContext(level="campaign", entity_id=str(c["id"]), entity_name=c["name"],
                            metrics=em, objective=c["objective"], status=c["status"],
                            daily_budget=c["daily_budget"], target_cpa=target_cpa, phase=phase,
                            score=score, calibration=calibration)
        all_cands += generate_candidates(ctx, config, economics, today=today)
        if em.cpa_web_click and em.cpa_web_click > (calibration.get("KILL_CPA", {}) or {}).get("value", 1e9):
            bleeders.append({"entity_level": "campaign", "entity_id": str(c["id"]),
                             "entity_name": c["name"], "metrics": em, "trend_days_bleeding": em.n_days})

    # Quality Gates (claude-ads §4) : tracking vérifié + 3×Kill + 5×CPA + diversité
    from optimizer.validator import GateContext
    tracking_ok = (sum(c["agg"]["web_click_checkout"] for c in campaigns) > 0
                   and any(_has_conv_tag(g) for g in ad_groups))
    gate_ctx = GateContext(break_even_cpa=economics.break_even_cpa, entity_cpa=entity_cpa,
                           tracking_ok=tracking_ok)

    arb = arbitrate(all_cands, total_budget_target=config.total_budget_target)
    batch = validate_batch(arb.retained, known_entity_ids=known_ids,
                           learning_entity_ids=learning_ids, gate_ctx=gate_ctx)
    kill_cpa = (calibration.get("KILL_CPA", {}) or {}).get("value", economics.break_even_cpa * 1.3)
    alerts = detect_inaction(bleeders, batch.accepted, kill_cpa)

    return {
        "ok": True, "mode": "live", "economics": economics.__dict__,
        "candidates": [c.to_dict() for c in batch.accepted],
        "merged": [c.to_dict() for c in arb.merged],
        "rejected": [{"candidate": r.candidate.to_dict(), "errors": r.errors} for r in batch.rejected],
        "inaction_alerts": [a.__dict__ for a in alerts],
    }


def persist_calibration(db, config: Optional[OptimizerConfig] = None) -> int:
    """Recalcule et écrit la calibration dans la table `calibration`."""
    from optimizer.calibration import write_calibration
    campaigns, _ = load_account(db)
    rows = compute_calibration(campaigns)
    return write_calibration(db, rows)
