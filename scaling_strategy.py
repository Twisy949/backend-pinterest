"""
SPEC scaling §1/§7 — Orchestrateur de la stratégie de scale.

Pour chaque campagne en phase SCALING : Gate 0 readiness → détecteur de saturation →
arbitrage VERTICAL/HORIZONTAL/FIX_FIRST → construction du move (branche verticale =
BUDGET_UP existant ; horizontale = catalogue séquencé) → barrière cannibalisme a priori
→ validation. Persiste `scaling_decisions` et `scaling_moves`.

Le module n'est actif qu'en SCALING (v2 §7) : ni cold-start, ni UNDERPOWERED.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def _metric_rows(rows) -> list[dict]:
    from optimizer.pipeline import _METRIC_FIELDS
    out = []
    for m in rows:
        d = {"date": getattr(m, "date", None)}
        for f in _METRIC_FIELDS:
            d[f] = getattr(m, f, 0) or 0
        out.append(d)
    return out


def _budget_steps(snaps: list, daily: list[dict], constants) -> list[dict]:
    """
    Reconstruit les hausses de budget passées (config_snapshots) et mesure, autour de
    chaque hausse, Δspend/Δconv sur les fenêtres avant/après (pour la courbe marginale §3).
    """
    from optimizer.metrics_access import aggregate, as_date
    snaps = sorted(snaps, key=lambda s: s.snapshot_date)
    steps: list[dict] = []
    prev_budget = None
    w = constants.EVAL_WINDOW
    for s in snaps:
        b = float(s.daily_budget or 0)
        if prev_budget is not None and b > prev_budget * 1.05:   # hausse ≥ 5 %
            d = as_date(s.snapshot_date)
            pre = aggregate(daily, level="campaign", window=(d - timedelta(days=w), d - timedelta(days=1)))
            post = aggregate(daily, level="campaign", window=(d + timedelta(days=1), d + timedelta(days=w)))
            steps.append({"budget_level": b,
                          "delta_spend": post.spend - pre.spend,
                          "delta_conv": post.web_click_checkout - pre.web_click_checkout})
        if b > 0:
            prev_budget = b
    return steps


def _saturation_signals(daily: list[dict], steps: list[dict], today: date, constants):
    from optimizer.metrics_access import aggregate, consolidated_window
    from optimizer.saturation import SaturationSignals, marginal_return_curve, recent_marginal_cpa

    w = constants.EVAL_WINDOW
    w1 = consolidated_window(today, w)                               # récent [J-8..J-2]
    w0_end = w1[0] - timedelta(days=1)
    w0 = (w0_end - timedelta(days=w - 1), w0_end)                    # précédent
    em1 = aggregate(daily, level="campaign", window=w1)
    em0 = aggregate(daily, level="campaign", window=w0)
    em30 = aggregate(daily, level="campaign", window=consolidated_window(today, 30))

    curve = marginal_return_curve(steps)
    cpa1, cpa0 = em1.cpa_web_click, em0.cpa_web_click
    roas1, roas0 = em1.roas_web_click, em0.roas_web_click

    sig = SaturationSignals(
        avg_cpa=em30.cpa_web_click, marginal_cpa=recent_marginal_cpa(curve),
        has_history=bool(curve),
        reach_flat=(em0.impressions > 0 and abs(em1.impressions - em0.impressions) / em0.impressions <= 0.10),
        frequency_rising=(em1.impression_frequency > em0.impression_frequency > 0),
        roas_declining=(roas1 is not None and roas0 is not None and roas1 < roas0 * 0.9),
        cpa_rising=(cpa1 is not None and cpa0 is not None and cpa1 > cpa0),
        conv_flat=(abs(em1.web_click_checkout - em0.web_click_checkout) / max(em0.web_click_checkout, 1) <= 0.10),
        spend_stable_or_up=(em1.spend >= em0.spend * 0.95),
    )
    return sig, em1


def build_scaling_strategy(db, *, config=None, today: Optional[date] = None) -> dict:
    """Exécute l'arbitrage de scale sur les campagnes SCALING ; persiste décisions + moves."""
    from sqlalchemy import func

    from optimizer.config import CONSTANTS as C, derive_economics, load_config
    from optimizer.candidates import EntityContext, gen_scale
    from optimizer.diagnostics_account import campaign_targeting_union, pool_all_creatives
    from optimizer.diagnostics_entity import detect_phase
    from optimizer.horizontal_scaling import build_horizontal_move, next_horizontal_lever, precheck_cannibal
    from optimizer.metrics_access import aggregate, consolidated_window
    from optimizer.pipeline import load_account, load_ads
    from optimizer.saturation import arbitrate_scale, near_saturation, vertical_blocked_by_saturation, VERTICAL
    from optimizer.scaling_readiness import assess_readiness, daily_cpa_series, READY
    from optimizer.segment_analysis import pinner_coverage
    from optimizer.segment_decision import aggregate_account_segments
    from optimizer.schemas import EntityMetrics, Phase
    from optimizer.segment_levers import propose_keyword_campaign
    from optimizer.validator import GateContext, validate_batch
    from optimizer.audit import _conv_tag_id
    from models import ConfigSnapshot, Metric, ScalingDecision, ScalingMove, SegmentPerformance

    config = config or load_config()
    today = today or date.today()

    campaigns, ad_groups = load_account(db)
    history = {"web_click_checkout_value": sum(c["agg"]["web_click_checkout_value"] for c in campaigns),
               "web_click_checkout": sum(c["agg"]["web_click_checkout"] for c in campaigns)}
    try:
        economics = derive_economics(config.margin_contrib, history)
    except Exception as e:
        return {"ok": False, "decision_ready": False, "reason": str(e)}

    target_cpa = economics.break_even_cpa
    tracking_ok = history["web_click_checkout"] > 0 and any(_conv_tag_id(g) for g in ad_groups)
    days = max((c["agg"]["n_days"] for c in campaigns), default=0)
    conv_week_total = history["web_click_checkout"] / max(days / 7.0, 1.0)

    pool = pool_all_creatives(load_ads(db))
    n_elite = max(1, min(C.CREATIVES_PER_COLDSTART,
                         sum(1 for c in pool if (c.get("web_click_checkout") or 0) >= C.MIN_CONV_TRUST)))

    # segments compte (dernière fenêtre) pour le levier KEYWORD — nécessite /segments/build
    latest_perf = db.query(func.max(SegmentPerformance.period_end)).scalar()
    perf_rows = []
    if latest_perf is not None:
        perf_rows = [{"targeting_type": p.targeting_type, "targeting_value": p.targeting_value,
                      "ad_group_id": p.ad_group_id, "source_status": p.source_status,
                      "spend": float(p.spend or 0), "n_conv": int(p.n_conv or 0),
                      "revenue": float(p.revenue or 0), "impressions": int(p.impressions or 0),
                      "clicks": int(p.clicks or 0)}
                     for p in db.query(SegmentPerformance).filter(SegmentPerformance.period_end == latest_perf).all()]
    account_segments = aggregate_account_segments(perf_rows)
    use_pinner_flag = pinner_coverage(perf_rows) >= C.PINNER_MIN_COVERAGE

    metrics_by_campaign: dict[str, list] = {}
    for m in db.query(Metric).all():
        metrics_by_campaign.setdefault(m.campaign_id, []).append(m)
    ag_specs_by_camp: dict[str, list] = {}
    for g in ad_groups:
        ag_specs_by_camp.setdefault(str(g["campaign_id"]), []).append(g.get("targeting_spec"))

    candidates = []
    decisions_out = []
    now = datetime.utcnow()

    for c in campaigns:
        if (c["status"] or "").upper() != "ACTIVE":
            continue
        agg = c["agg"]
        em_full = EntityMetrics(level="campaign", entity_id=c["id"], spend=agg["spend"],
                                web_click_checkout=agg["web_click_checkout"],
                                web_click_checkout_value=agg["web_click_checkout_value"], n_days=agg["n_days"])
        phase = detect_phase(em_full, economics, c["daily_budget"], target_cpa)
        if phase != Phase.SCALING:
            continue   # module actif uniquement en SCALING (§7)

        daily = _metric_rows(metrics_by_campaign.get(c["id"], []))
        w14 = consolidated_window(today, 14)
        recent_series = daily_cpa_series([
            r for r in daily
            if r["date"] and w14[0] <= (r["date"].date() if hasattr(r["date"], "date") else r["date"]) <= w14[1]
        ])
        conv30 = aggregate(daily, level="campaign", window=consolidated_window(today, 30)).web_click_checkout

        # --- Gate 0 readiness (§2) ---
        rd = assess_readiness(tracking_ok=tracking_ok, in_learning=False,
                              cpa_series=recent_series, conv_count=conv30)
        dec = {"campaign_id": str(c["id"]), "campaign_name": c["name"]}
        if not rd.ready:
            dec.update({"readiness": rd.status, "choice": "FIX_FIRST", "reason": rd.reason, "saturation": None})
            decisions_out.append(dec)
            _persist_decision(db, c, dec, em_full, None, conv30, today, now)
            continue

        # --- détecteur de saturation + arbitrage (§3) ---
        snaps = db.query(ConfigSnapshot).filter(ConfigSnapshot.entity_level == "campaign",
                                                ConfigSnapshot.entity_id == str(c["id"])).all()
        sig, em1 = _saturation_signals(daily, _budget_steps(snaps, daily, C), today, C)
        sat = near_saturation(sig)
        choice = arbitrate_scale(sat)
        dec.update({"readiness": READY, "choice": choice, "saturation": sat.saturated,
                    "reason": sat.reason, "avg_cpa": sig.avg_cpa, "marginal_cpa": sig.marginal_cpa})
        decisions_out.append(dec)
        _persist_decision(db, c, dec, em_full, sig, conv30, today, now)

        core_spec = campaign_targeting_union(ag_specs_by_camp.get(str(c["id"]), []))
        baseline = {"window": "7d_consolidated", "cpa_web_click": em1.cpa_web_click,
                    "roas_web_click": em1.roas_web_click, "conv": em1.web_click_checkout}

        if choice == VERTICAL:
            if vertical_blocked_by_saturation(sig.marginal_cpa, sig.avg_cpa):
                continue
            ctx = EntityContext(level="campaign", entity_id=str(c["id"]), entity_name=c["name"],
                                metrics=em_full, objective=c["objective"], status="ACTIVE",
                                daily_budget=c["daily_budget"], target_cpa=target_cpa,
                                phase=Phase.SCALING, gate_ready=True)
            for cand in gen_scale(ctx, economics, today, C):
                candidates.append(cand)
                _persist_move(db, c, "VERTICAL", "BUDGET_UP", cand.params.get("to"),
                              target_cpa, baseline, cand.rationale, now)
        else:  # HORIZONTAL
            open_levers = {mv.lever for mv in db.query(ScalingMove).filter(
                ScalingMove.campaign_id == str(c["id"]), ScalingMove.direction == "HORIZONTAL").all()}
            last = (db.query(ScalingMove).filter(ScalingMove.campaign_id == str(c["id"]),
                    ScalingMove.direction == "HORIZONTAL").order_by(ScalingMove.id.desc()).first())
            lever = next_horizontal_lever(open_levers, prev_incremental=(last.incremental if last else None),
                                          conv_week_total=conv_week_total)
            if lever is None:
                continue
            cand = _build_lever_candidate(lever, c, em_full, economics, n_elite, baseline, core_spec,
                                          target_cpa, conv_week_total, account_segments, use_pinner_flag,
                                          today, C)
            if cand is None:
                continue
            candidates.append(cand)
            _persist_move(db, c, "HORIZONTAL", lever.name, cand.params.get("daily_budget"),
                          target_cpa, baseline, cand.rationale, now)

    known = {str(x["id"]) for x in campaigns} | {str(g["id"]) for g in ad_groups}
    gate_ctx = GateContext(break_even_cpa=economics.break_even_cpa, tracking_ok=tracking_ok)
    batch = validate_batch(candidates, known_entity_ids=known, gate_ctx=gate_ctx)
    db.commit()

    return {
        "ok": True, "economics": economics.__dict__, "conv_week_total": round(conv_week_total, 1),
        "decisions": decisions_out,
        "candidates": [c.to_dict() for c in batch.accepted],
        "rejected": [{"candidate": r.candidate.to_dict(), "errors": r.errors} for r in batch.rejected],
    }


def _build_lever_candidate(lever, c, em_full, economics, n_elite, baseline, core_spec,
                           target_cpa, conv_week_total, account_segments, use_pinner_flag, today, C):
    """Candidat du levier horizontal sélectionné, avec barrière cannibalisme a priori (§6.1)."""
    from optimizer.horizontal_scaling import build_horizontal_move, precheck_cannibal
    from optimizer.schemas import ActionType, Hypothesis, Level
    from optimizer.segment_levers import _mk, propose_keyword_campaign

    if lever.name == "CREATIVE_DIVERSITY":
        hyp = Hypothesis("cpa_web_click", "7d_consolidated", baseline=em_full.cpa_web_click,
                         expected=economics.target_scale_cpa, min_acceptable=economics.break_even_cpa,
                         horizon_t1=C.REVIEW_HORIZON_T1, horizon_t2=C.REVIEW_HORIZON_T2)
        return _mk(ActionType.ADD_CREATIVES, level=Level.CAMPAIGN,
                   params={"lever": "CREATIVE_DIVERSITY", "n_creatives": n_elite,
                           "hold_days": C.AGE_LEARNING_DAYS, "baseline": baseline},
                   rationale=("HORIZONTAL #1 (créa) : inventaire frais sans toucher à l'audience "
                              "— combat la fatigue, risque cannibalisme nul."),
                   hypothesis=hyp, metrics=em_full, today=today, constants=C,
                   entity_id=str(c["id"]), entity_name=c["name"], account_check="diversité créative.")

    if lever.name == "KEYWORD_CAMPAIGN":
        core = {"id": c["id"], "name": c["name"], "phase": "SCALING", "daily_budget": c["daily_budget"],
                "roas_web_click": em_full.roas_web_click, "cpa_web_click": em_full.cpa_web_click,
                "n_conv": em_full.web_click_checkout}
        kw = propose_keyword_campaign(core, {"conv_week_total": conv_week_total}, account_segments,
                                      economics, n_elite_creatives=n_elite, use_pinner=use_pinner_flag,
                                      period_days=180, today=today)
        if kw is None:
            return None
        blocked, _ = precheck_cannibal({"KEYWORD": kw.params["targeting"]["KEYWORD"]}, core_spec)
        return None if blocked else kw

    # Leviers d'audience (adjacents/actalike/pays) : nécessitent un ciblage concret
    # (taxonomie d'intérêts / source actalike) non disponible déterministiquement ici.
    # On émet le move-gabarit (flag needs_targeting) + barrière cannibalisme dès qu'un
    # ciblage sera renseigné. Risque cannibalisme noté pour revue.
    move = build_horizontal_move(lever, target_cpa=target_cpa, n_elite_creatives=n_elite,
                                 baseline=baseline, constants=C)
    from optimizer.schemas import Hypothesis as _Hyp
    hyp = _Hyp("roas_web_click", "7d_consolidated", baseline=em_full.roas_web_click,
               expected=economics.target_scale_roas, min_acceptable=economics.break_even_roas,
               horizon_t1=C.REVIEW_HORIZON_T1, horizon_t2=C.REVIEW_HORIZON_T2)
    return _mk(ActionType.CREATE_CAMPAIGN, level=Level.CAMPAIGN,
               params={"lever": lever.name, "daily_budget": move["budget"], "target_cpa": target_cpa,
                       "n_creatives": n_elite, "bid": "AUTOMATIC_BID", "hold_days": C.AGE_LEARNING_DAYS,
                       "needs_targeting": True, "baseline": baseline, "cannibal_risk": lever.cannibal_risk},
               rationale=(f"HORIZONTAL #{lever.rank} ({lever.name}) : {lever.note}. "
                          f"Budget incrémental {move['budget']} €, créas élite, bid auto."),
               hypothesis=hyp, metrics=em_full, today=today, constants=C,
               entity_name=f"[{lever.name}]", account_check="barrière cannibalisme + incrémentalité (§6).")


def _persist_decision(db, c, dec, em_full, sig, conv30, today, now):
    from models import ScalingDecision
    existing = (db.query(ScalingDecision)
                .filter(ScalingDecision.campaign_id == str(c["id"]),
                        ScalingDecision.decision_date == today).one_or_none())
    payload = dict(readiness=dec.get("readiness"), saturation=dec.get("saturation"),
                   choice=dec["choice"], reason=dec.get("reason"),
                   avg_cpa=(sig.avg_cpa if sig else None),
                   marginal_cpa=(sig.marginal_cpa if sig else None),
                   roas_web_click=em_full.roas_web_click, conv_count=int(conv30), computed_at=now)
    if existing:
        for k, v in payload.items():
            setattr(existing, k, v)
    else:
        db.add(ScalingDecision(campaign_id=str(c["id"]), decision_date=today, **payload))


def _persist_move(db, c, direction, lever, budget, target_cpa, baseline, rationale, now):
    from models import ScalingMove
    db.add(ScalingMove(campaign_id=str(c["id"]), direction=direction, lever=lever,
                       budget=float(budget) if budget is not None else None, target_cpa=target_cpa,
                       baseline=baseline, status="PROPOSED", rationale=rationale,
                       created_at=now, updated_at=now))
