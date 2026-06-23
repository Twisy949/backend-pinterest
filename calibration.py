"""
Phase 4 — Calibration depuis l'historique (réf. spec §13).

Les seuils ne sont PAS des constantes d'industrie : ce sont les percentiles des
entités passées du compte. P75 = « bon » (métriques où haut = mieux) ; P25 = kill.
Pour le CPA (bas = mieux) : GOOD_CPA = P25, KILL_CPA = P75.

Tout est écrit dans la table `calibration` (key/scope/value JSON).
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# --- statistiques (pures, sans numpy pour rester déterministe & léger) -------

def percentile(values: list[float], q: float) -> Optional[float]:
    """Percentile par interpolation linéaire. q ∈ [0,100]. None si vide."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    rank = (q / 100.0) * (len(xs) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(xs) - 1)
    frac = rank - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def _extract(entity) -> dict:
    """Normalise EntityMetrics | dict({agg}) | dict(raw) → dict de sommes brutes."""
    if hasattr(entity, "web_click_checkout"):  # EntityMetrics
        return {
            "spend": entity.spend, "impressions": entity.impressions,
            "clicks": entity.clicks, "saves": entity.saves,
            "outbound_clicks": entity.outbound_clicks,
            "add_to_cart": entity.add_to_cart,
            "web_click_checkout": entity.web_click_checkout,
        }
    src = entity.get("agg", entity) if isinstance(entity, dict) else {}
    keys = ("spend", "impressions", "clicks", "saves", "outbound_clicks",
            "add_to_cart", "web_click_checkout")
    return {k: float(src.get(k, 0) or 0) for k in keys}


def _ratios(e: dict) -> dict:
    imp = e["impressions"]
    clk = e["clicks"]
    out = {}
    out["ctr"] = e["clicks"] / imp if imp > 0 else None
    out["outbound_ctr"] = e["outbound_clicks"] / imp if imp > 0 else None
    out["save_rate"] = e["saves"] / imp if imp > 0 else None
    out["add_to_cart_cr"] = e["add_to_cart"] / clk if clk > 0 else None
    out["checkout_cr"] = e["web_click_checkout"] / clk if clk > 0 else None
    out["cpa"] = e["spend"] / e["web_click_checkout"] if e["web_click_checkout"] > 0 else None
    return out


# --- benchmarks --------------------------------------------------------------

# métriques où "plus haut = mieux"
_HIGHER_BETTER = ("ctr", "outbound_ctr", "save_rate", "add_to_cart_cr", "checkout_cr")


def compute_benchmarks(entities: Iterable, min_conv: int = 5) -> dict:
    """
    Calcule les percentiles par métrique sur les entités ayant un volume crédible
    (≥ `min_conv` conversions web-click pour les ratios de fond de funnel + CPA).
    """
    rows = [_extract(e) for e in entities]
    # on ne garde, pour les ratios fiables, que les entités avec assez de signal
    ratios = [_ratios(r) for r in rows if r["impressions"] >= 1000]
    cpa_pool = [
        r["spend"] / r["web_click_checkout"]
        for r in rows if r["web_click_checkout"] >= min_conv and r["spend"] > 0
    ]

    out: dict = {}
    for metric in _HIGHER_BETTER:
        vals = [r[metric] for r in ratios if r[metric] is not None]
        if not vals:
            continue
        out[f"BENCH_{metric.upper()}"] = {
            "p25": _round(percentile(vals, 25)),
            "p50": _round(percentile(vals, 50)),
            "p75": _round(percentile(vals, 75)),
            "good": _round(percentile(vals, 75)),   # P75 = bon
            "kill": _round(percentile(vals, 25)),   # P25 = kill
            "n": len(vals),
        }

    if cpa_pool:
        out["GOOD_CPA"] = {"value": _round(percentile(cpa_pool, 25)), "n": len(cpa_pool),
                           "basis": "P25 du CPA web-click des entités du compte"}
        out["KILL_CPA"] = {"value": _round(percentile(cpa_pool, 75)), "n": len(cpa_pool),
                           "basis": "P75 du CPA web-click des entités du compte"}
        out["MEDIAN_CPA"] = {"value": _round(percentile(cpa_pool, 50)), "n": len(cpa_pool)}
    return out


def fatigue_curve(daily_series: Optional[dict] = None) -> dict:
    """
    Estime à partir de quelle fréquence cumulée / après combien de jours les pins
    déclinent (CTR sous 80 % de son pic trailing alors que la fréquence monte).
    `daily_series` : {entity_id: [lignes journalières triées]}.
    """
    if not daily_series:
        return {"basis": "default", "decline_freq": 2.5, "median_days_to_decline": None}

    freqs_at_decline = []
    days_to_decline = []
    for rows in daily_series.values():
        srt = sorted(rows, key=lambda r: (r.get("date") or ""))
        peak_ctr = 0.0
        for i, r in enumerate(srt):
            imp = float(r.get("impressions") or 0)
            if imp <= 0:
                continue
            ctr = float(r.get("clicks") or 0) / imp
            peak_ctr = max(peak_ctr, ctr)
            freq = float(r.get("impression_frequency") or 0)
            if peak_ctr > 0 and ctr < 0.8 * peak_ctr and freq > 1.0:
                if freq > 0:
                    freqs_at_decline.append(freq)
                days_to_decline.append(i + 1)
                break

    return {
        "basis": "computed" if freqs_at_decline else "insufficient",
        "decline_freq": _round(percentile(freqs_at_decline, 50)) if freqs_at_decline else 2.5,
        "median_days_to_decline": percentile(days_to_decline, 50) if days_to_decline else None,
        "n": len(freqs_at_decline),
    }


def latencies(history: Optional[dict] = None) -> dict:
    """
    Latences de restabilisation / sortie de learning / achat → calent
    PATIENCE_POST_CHANGE et REVIEW_HORIZON_T2. Faute d'historique d'actions
    (ledger quasi vide), renvoie les défauts du spec avec basis='default'.
    """
    from optimizer.config import CONSTANTS
    base = {
        "patience_post_change": CONSTANTS.PATIENCE_POST_CHANGE,
        "review_horizon_t2": CONSTANTS.REVIEW_HORIZON_T2,
        "basis": "default",
    }
    if history:
        base.update(history)
        base["basis"] = "computed"
    return base


def _round(v):
    return round(v, 6) if isinstance(v, float) else v


# --- assemblage + écriture ---------------------------------------------------

def compute_calibration(
    entities: Iterable,
    daily_series: Optional[dict] = None,
    history: Optional[dict] = None,
    scope: str = "account",
) -> list[dict]:
    """Produit les lignes {key, scope, value} prêtes pour la table `calibration`."""
    rows: list[dict] = []
    for key, value in compute_benchmarks(entities).items():
        rows.append({"key": key, "scope": scope, "value": value})
    rows.append({"key": "FATIGUE_CURVE", "scope": scope, "value": fatigue_curve(daily_series)})
    rows.append({"key": "LATENCIES", "scope": scope, "value": latencies(history)})
    return rows


def write_calibration(db, rows: list[dict]) -> int:
    """Upsert idempotent dans la table `calibration` (par key+scope)."""
    from datetime import datetime

    from models import Calibration
    n = 0
    for r in rows:
        existing = (
            db.query(Calibration)
            .filter_by(key=r["key"], scope=r.get("scope", "account"))
            .one_or_none()
        )
        if existing is None:
            db.add(Calibration(key=r["key"], scope=r.get("scope", "account"),
                               value=r["value"], computed_at=datetime.utcnow()))
        else:
            existing.value = r["value"]
            existing.computed_at = datetime.utcnow()
        n += 1
    db.commit()
    return n
