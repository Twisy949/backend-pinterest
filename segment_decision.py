"""
SPEC v2 §4/§4.1/§5 — Moteur de décision de segmentation : large vs isolé.

Cœur PUR (matrice SIGNAL×VOLUME + FDR Benjamini-Hochberg + rôles par type) + job ORM
`build_segment_decisions` qui lit `segment_performance` et écrit `segment_decisions`.

Décision (matrice §1) :
  Pas de signal           → KEEP_BROAD   (le large optimise mieux ; défaut Pinterest)
  Signal, sous-volume     → WAIT         (gagnant mais à garder dans le large)
  Signal, volume OK, +    → SPLIT        (isoler en AG/campagne)
  Perdant net fiable      → DROP         (exclure du large)

Tous les ROAS/CPA sont web-click (INV-2). FDR déterministe sans scipy.
"""
from __future__ import annotations

import logging
import math
from typing import Iterable, Optional

from optimizer.config import CONSTANTS, Constants

logger = logging.getLogger(__name__)


# Rôle et capacité d'isolation par type (SPEC §0/§5).
#   split_allowed=False → ne peut JAMAIS fonder un SPLIT seul (PINNER §5.2) ;
#                         un signal y est rétrogradé en WAIT (corroborer par test).
#   drop_allowed=False  → données trop peu fiables pour exclure (PINNER).
SEGMENT_TYPE_ROLE = {
    "KEYWORD":           {"role": "intention",            "split_allowed": True,  "drop_allowed": True},
    "TARGETED_INTEREST": {"role": "decouverte_primaire",  "split_allowed": True,  "drop_allowed": True},
    "PINNER_INTEREST":   {"role": "decouverte_secondaire", "split_allowed": False, "drop_allowed": False},
    "AUDIENCE_INCLUDE":  {"role": "comportement",          "split_allowed": True,  "drop_allowed": True,
                          "retargeting_cap": True},
    "GENDER":            {"role": "demo", "split_allowed": True, "drop_allowed": True},
    "AGE_BUCKET":        {"role": "demo", "split_allowed": True, "drop_allowed": True},
    "COUNTRY":           {"role": "demo", "split_allowed": True, "drop_allowed": True},
    "REGION":            {"role": "demo", "split_allowed": True, "drop_allowed": True},
}

PINNER_TYPE = "PINNER_INTEREST"


# --- baseline & statistiques (pures) ----------------------------------------

def account_baseline_roas(total_revenue: float, total_spend: float) -> Optional[float]:
    """ROAS web-click du COMPTE sur la fenêtre = Σrevenue / Σspend (None si pas de spend)."""
    return round(total_revenue / total_spend, 6) if total_spend > 0 else None


def _segment_pvalue(seg: dict, baseline_conv_per_spend: float) -> float:
    """
    p-value two-sided que les conversions du segment diffèrent du compte.
    Modèle Poisson : exp_conv = spend × (conv/spend compte) ; z = (n-exp)/√exp ;
    p = erfc(|z|/√2). Déterministe (pas de scipy).
    """
    spend = float(seg.get("spend", 0) or 0)
    n     = float(seg.get("n_conv", 0) or 0)
    exp   = spend * baseline_conv_per_spend
    if exp <= 0:
        return 1.0
    z = (n - exp) / math.sqrt(exp)
    return math.erfc(abs(z) / math.sqrt(2))


def benjamini_hochberg(pvalues: list[float], alpha: float) -> list[bool]:
    """Procédure BH : retourne, par index, si l'hypothèse est significative (FDR ≤ alpha)."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    max_rank = 0
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= (rank / m) * alpha:
            max_rank = rank
    significant = [False] * m
    for rank, idx in enumerate(order, start=1):
        if rank <= max_rank:
            significant[idx] = True
    return significant


# --- décision d'un segment (§4 + garde de rôle §5) --------------------------

def decide_segment(
    seg: dict,
    baseline_roas: Optional[float],
    available_budget: Optional[float],
    *,
    significant: bool,
    period_days: int,
    constants: Constants = CONSTANTS,
) -> dict:
    """Applique la matrice SIGNAL×VOLUME à un segment agrégé (niveau compte)."""
    ttype  = seg.get("targeting_type")
    n_conv = int(seg.get("n_conv", 0) or 0)
    spend  = float(seg.get("spend", 0) or 0)
    # roas/cpa web-click : fournis (segment agrégé) ou dérivés des sommes brutes (INV-2)
    roas = seg.get("roas_web_click")
    if roas is None:
        rev = float(seg.get("revenue", 0) or 0)
        roas = round(rev / spend, 6) if spend > 0 else None
    cpa = seg.get("cpa_web_click")
    if cpa is None:
        cpa = round(spend / n_conv, 4) if n_conv > 0 else None
    role = SEGMENT_TYPE_ROLE.get(ttype, {"split_allowed": True, "drop_allowed": True})

    weeks     = max(period_days / 7.0, 1.0)
    conv_week = round(n_conv / weeks, 2)

    delta    = (roas - baseline_roas) if (roas is not None and baseline_roas is not None) else None
    material = delta is not None and abs(delta) >= constants.MATERIAL_DELTA
    reliable = n_conv >= constants.MIN_CONV_SEGMENT
    signal   = bool(material and reliable and significant)

    volume_ok = bool(
        cpa is not None and available_budget is not None
        and available_budget >= constants.BUDGET_FLOOR_X_CPA * cpa
        and conv_week >= constants.LEARNING_CONV_WEEK
    )

    winner = signal and delta is not None and delta > 0
    loser  = signal and delta is not None and delta < 0

    if winner and volume_ok:
        decision, why = "SPLIT", "signal + volume → isoler en AG/campagne"
    elif winner and not volume_ok:
        decision, why = "WAIT", "gagnant mais sous-volume → garder dans le large"
    elif loser:
        decision, why = "DROP", "perdant net fiable → exclure du large"
    else:
        decision, why = "KEEP_BROAD", "pas d'écart matériel/fiable → le large optimise mieux"

    # Gardes de rôle (§5)
    if decision == "SPLIT" and not role.get("split_allowed", True):
        decision, why = "WAIT", "base non isolable seule (PINNER) → corroborer par un test"
    if decision == "DROP" and not role.get("drop_allowed", True):
        decision, why = "KEEP_BROAD", "données trop peu fiables pour exclure (PINNER)"

    db = f"{available_budget:.0f}€" if available_budget is not None else "?"
    rationale = (
        f"{decision} : ROAS {roas if roas is not None else '—'} vs baseline "
        f"{baseline_roas if baseline_roas is not None else '—'} "
        f"(Δ={delta:.2f} ; matériel={material} ; n={n_conv}≥{constants.MIN_CONV_SEGMENT}={reliable} ; "
        f"FDR={significant}) ; volume {conv_week}/sem, budget {db} — {why}."
    ) if delta is not None else (
        f"KEEP_BROAD : ROAS indisponible (n={n_conv}) — {why}."
    )

    return {
        "targeting_type": ttype, "targeting_value": seg.get("targeting_value"),
        "decision": decision, "signal": signal, "volume_ok": volume_ok,
        "significant": bool(significant),
        "delta_roas": round(delta, 6) if delta is not None else None,
        "roas_web_click": roas, "cpa_web_click": cpa,
        "spend": round(float(seg.get("spend", 0) or 0), 4), "n_conv": n_conv,
        "conv_week": conv_week, "account_baseline_roas": baseline_roas,
        "rationale": rationale,
    }


def allow_combination(cells: Iterable[dict], *, max_layers: int = CONSTANTS.MAX_LAYERS,
                      min_conv: int = CONSTANTS.MIN_CONV_SEGMENT) -> bool:
    """§4.1 — superposer ne réduit le volume que si chaque cellule reste au-dessus du plancher."""
    cells = list(cells)
    if len(cells) > max_layers:
        return False
    return all(int(c.get("n_conv", 0) or 0) >= min_conv for c in cells)


# --- orchestration pure -----------------------------------------------------

def decide_segments(
    segments: list[dict],
    *,
    baseline_roas: Optional[float],
    baseline_conv_per_spend: float,
    available_budget: Optional[float],
    period_days: int,
    use_pinner: bool,
    constants: Constants = CONSTANTS,
) -> list[dict]:
    """
    Décide une liste de segments agrégés (niveau compte). Exclut PINNER si non peuplé.
    FDR (BH) calculé sur l'ensemble de la famille testée.
    """
    judged = [s for s in segments if not (s.get("targeting_type") == PINNER_TYPE and not use_pinner)]
    if not judged:
        return []

    pvalues = [_segment_pvalue(s, baseline_conv_per_spend) for s in judged]
    sig = benjamini_hochberg(pvalues, constants.FDR_ALPHA)

    return [
        decide_segment(s, baseline_roas, available_budget,
                       significant=sig[i], period_days=period_days, constants=constants)
        for i, s in enumerate(judged)
    ]


# --- agrégation compte (par type, value) ------------------------------------

def aggregate_account_segments(perf_rows: Iterable[dict]) -> list[dict]:
    """
    Replie les lignes par-ad_group de `segment_performance` en segments NIVEAU COMPTE
    (par targeting_type, targeting_value). source_status = ACTIVE si au moins un AG actif.
    """
    agg: dict[tuple, dict] = {}
    for r in perf_rows:
        key = (r["targeting_type"], r["targeting_value"])
        g = agg.get(key)
        if g is None:
            g = agg[key] = {"targeting_type": r["targeting_type"],
                            "targeting_value": r["targeting_value"],
                            "spend": 0.0, "n_conv": 0, "revenue": 0.0,
                            "impressions": 0, "clicks": 0, "any_active": False}
        g["spend"]       += float(r.get("spend", 0) or 0)
        g["n_conv"]      += int(r.get("n_conv", 0) or 0)
        g["revenue"]     += float(r.get("revenue", 0) or 0)
        g["impressions"] += int(r.get("impressions", 0) or 0)
        g["clicks"]      += int(r.get("clicks", 0) or 0)
        if (r.get("source_status") or "").upper() == "ACTIVE":
            g["any_active"] = True

    out = []
    for g in agg.values():
        spend = g["spend"]
        out.append({
            "targeting_type": g["targeting_type"], "targeting_value": g["targeting_value"],
            "spend": round(spend, 4), "n_conv": g["n_conv"], "revenue": round(g["revenue"], 4),
            "impressions": g["impressions"], "clicks": g["clicks"],
            "cpa_web_click": round(spend / g["n_conv"], 4) if g["n_conv"] > 0 else None,
            "roas_web_click": round(g["revenue"] / spend, 6) if spend > 0 else None,
            "source_status": "ACTIVE" if g["any_active"] else "PAUSED",
        })
    return out


# --- job ORM ----------------------------------------------------------------

def _account_totals(db, start, end) -> tuple[float, float, float]:
    """(spend, n_conv, revenue) web-click du compte sur [start,end] depuis ad_group_metrics."""
    from models import AdGroupMetric
    rows = (
        db.query(AdGroupMetric)
        .filter(AdGroupMetric.date >= start, AdGroupMetric.date <= end)
        .all()
    )
    spend = sum(float(m.spend or 0) for m in rows)
    conv  = sum(int(m.web_click_checkout or 0) for m in rows)
    rev   = sum(float(m.web_click_checkout_value or 0) for m in rows)
    return spend, conv, rev


def _default_available_budget(db) -> Optional[float]:
    """Budget déployable : TOTAL_BUDGET_TARGET sinon Σ budgets/jour des campagnes ACTIVE."""
    from optimizer.config import load_config
    cfg = load_config()
    if cfg.total_budget_target is not None:
        return float(cfg.total_budget_target)
    from models import Campaign
    total = sum(float(c.daily_budget or 0)
                for c in db.query(Campaign).filter(Campaign.status == "ACTIVE").all())
    return total or None


def build_segment_decisions(
    db, *, days: int = 180, today=None, available_budget: Optional[float] = None,
    constants: Constants = CONSTANTS,
) -> dict:
    """
    Lit `segment_performance` (fenêtre [today-days, today]), calcule les décisions,
    écrit `segment_decisions` (idempotent). Retourne un résumé.
    """
    from datetime import date as _date, datetime, timedelta
    from models import SegmentPerformance, SegmentDecision

    today = today or _date.today()
    start = today - timedelta(days=days)

    perf = (
        db.query(SegmentPerformance)
        .filter(SegmentPerformance.period_start == start,
                SegmentPerformance.period_end == today)
        .all()
    )
    perf_rows = [{
        "targeting_type": p.targeting_type, "targeting_value": p.targeting_value,
        "ad_group_id": p.ad_group_id, "source_status": p.source_status,
        "spend": float(p.spend or 0), "n_conv": int(p.n_conv or 0),
        "revenue": float(p.revenue or 0), "impressions": int(p.impressions or 0),
        "clicks": int(p.clicks or 0),
    } for p in perf]

    from optimizer.segment_analysis import pinner_coverage
    coverage = pinner_coverage(perf_rows)
    pinner_on = coverage >= constants.PINNER_MIN_COVERAGE

    segments = aggregate_account_segments(perf_rows)

    spend_t, conv_t, rev_t = _account_totals(db, start, today)
    baseline_roas = account_baseline_roas(rev_t, spend_t)
    conv_per_spend = (conv_t / spend_t) if spend_t > 0 else 0.0
    if available_budget is None:
        available_budget = _default_available_budget(db)

    decisions = decide_segments(
        segments, baseline_roas=baseline_roas, baseline_conv_per_spend=conv_per_spend,
        available_budget=available_budget, period_days=days, use_pinner=pinner_on,
        constants=constants,
    )

    db.query(SegmentDecision).filter(
        SegmentDecision.period_start == start, SegmentDecision.period_end == today,
    ).delete(synchronize_session=False)

    now = datetime.utcnow()
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1
        db.add(SegmentDecision(
            targeting_type=d["targeting_type"], targeting_value=d["targeting_value"],
            decision=d["decision"], signal=d["signal"], volume_ok=d["volume_ok"],
            significant=d["significant"], delta_roas=d["delta_roas"],
            roas_web_click=d["roas_web_click"], cpa_web_click=d["cpa_web_click"],
            spend=d["spend"], n_conv=d["n_conv"], conv_week=d["conv_week"],
            account_baseline_roas=d["account_baseline_roas"], rationale=d["rationale"],
            period_start=start, period_end=today, computed_at=now,
        ))
    db.commit()

    logger.info("build_segment_decisions: %d décisions %s (baseline=%s, pinner=%s)",
                len(decisions), counts, baseline_roas, pinner_on)
    return {
        "n_decisions": len(decisions), "counts": counts,
        "account_baseline_roas": baseline_roas, "use_pinner": pinner_on,
        "pinner_coverage": coverage, "available_budget": available_budget,
        "period_start": start.isoformat(), "period_end": today.isoformat(),
    }
