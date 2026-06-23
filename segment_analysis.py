"""
SPEC v2 §3 — Analyse de performance par segment de ciblage.

Cœur PUR (`aggregate_segments`, `pinner_coverage`) testable sans DB, + un job ORM
`build_segment_performance` qui assemble la source :
  - historique > 90 j depuis `pinterest_targeting_daily` (déjà en base) ;
  - live < 90 j via `get_targeting_analytics` (réutilisé tel quel).

Tous les ratios sont web-click (INV-2) : `roas_web_click`, `cpa_web_click`.
Couvre les campagnes ACTIVES *et* en pause (la source les contient déjà).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Types analysés (mêmes que le backfill ciblage — SPEC §0).
SEGMENT_TARGETING_TYPES = [
    "KEYWORD", "GENDER", "COUNTRY",
    "TARGETED_INTEREST", "PINNER_INTEREST",
    "AGE_BUCKET", "REGION",
]
PINNER_TYPE = "PINNER_INTEREST"

# Clés sommables d'une ligne-segment canonique.
_SUM_KEYS = ("spend", "n_conv", "revenue", "impressions", "clicks")


# --- cœur pur ---------------------------------------------------------------

def aggregate_segments(rows: Iterable[dict]) -> list[dict]:
    """
    Agrège des lignes-segment brutes par (targeting_type, targeting_value, ad_group_id).

    Chaque ligne d'entrée : {targeting_type, targeting_value, ad_group_id, campaign_id?,
    source_status?, spend, n_conv, revenue, impressions, clicks}.
    Sortie : une ligne agrégée par clé, avec cpa/roas web-click dérivés.
    """
    agg: dict[tuple, dict] = {}
    for r in rows:
        ttype = r.get("targeting_type")
        tval  = r.get("targeting_value")
        ag    = r.get("ad_group_id")
        if ttype is None or tval in (None, ""):
            continue
        key = (ttype, str(tval), str(ag) if ag is not None else None)
        g = agg.get(key)
        if g is None:
            g = agg[key] = {
                "targeting_type": ttype, "targeting_value": str(tval),
                "ad_group_id": str(ag) if ag is not None else None,
                "campaign_id": r.get("campaign_id"),
                "source_status": r.get("source_status"),
                **{k: 0.0 for k in _SUM_KEYS},
            }
        if g["campaign_id"] is None and r.get("campaign_id") is not None:
            g["campaign_id"] = r.get("campaign_id")
        if g["source_status"] is None and r.get("source_status") is not None:
            g["source_status"] = r.get("source_status")
        for k in _SUM_KEYS:
            g[k] += float(r.get(k, 0) or 0)

    out = []
    for g in agg.values():
        spend  = g["spend"]
        n_conv = g["n_conv"]
        g["n_conv"]         = int(round(n_conv))
        g["impressions"]    = int(round(g["impressions"]))
        g["clicks"]         = int(round(g["clicks"]))
        g["spend"]          = round(spend, 4)
        g["revenue"]        = round(g["revenue"], 4)
        g["cpa_web_click"]  = round(spend / n_conv, 4) if n_conv > 0 else None
        g["roas_web_click"] = round(g["revenue"] / spend, 6) if spend > 0 else None
        out.append(g)
    return out


def pinner_coverage(rows: Iterable[dict], *, pinner_type: str = PINNER_TYPE) -> float:
    """
    Couverture PINNER_INTEREST (§3) : fraction des ad_groups (ayant des données de
    ciblage) qui possèdent au moins un segment PINNER peuplé (impressions > 0).
    PINNER étant souvent vide, ce ratio sert de garde `use_pinner`.
    """
    ag_all: set = set()
    ag_pinner: set = set()
    for r in rows:
        ag = r.get("ad_group_id")
        if ag is None:
            continue
        ag = str(ag)
        ag_all.add(ag)
        if r.get("targeting_type") == pinner_type and float(r.get("impressions", 0) or 0) > 0:
            ag_pinner.add(ag)
    if not ag_all:
        return 0.0
    return round(len(ag_pinner) / len(ag_all), 4)


def use_pinner(rows: Iterable[dict], *, min_coverage: float) -> bool:
    """PINNER_INTEREST n'entre dans les décisions que si suffisamment peuplé (§5.2)."""
    return pinner_coverage(rows) >= min_coverage


# --- adaptateurs source -----------------------------------------------------

def _hist_row(r, ag_map: dict) -> dict:
    """PinterestTargetingDaily → ligne-segment canonique."""
    camp_id, status = ag_map.get(str(r.ad_group_id), (None, None))
    return {
        "targeting_type":  r.targeting_type,
        "targeting_value": r.targeting_value,
        "ad_group_id":     str(r.ad_group_id),
        "campaign_id":     camp_id,
        "source_status":   status,
        "spend":           float(r.spend or 0),
        "n_conv":          int(r.web_click_checkout or 0),
        "revenue":         float(r.web_click_checkout_value_micro or 0) / 1_000_000,
        "impressions":     int(r.impressions or 0),
        "clicks":          int(r.clicks or 0),
    }


def _live_row(r: dict, ttype: str, ag_map: dict) -> Optional[dict]:
    """Ligne brute get_targeting_analytics → ligne-segment canonique (cf. campaign_targeting)."""
    m  = r.get("metrics") or r
    tv = r.get("targeting_value") or r.get("value") or r.get("id")
    ag = m.get("AD_GROUP_ID")
    if tv in (None, "") or not ag:
        return None
    camp_id, status = ag_map.get(str(ag), (None, None))
    return {
        "targeting_type":  ttype,
        "targeting_value": str(tv),
        "ad_group_id":     str(ag),
        "campaign_id":     camp_id,
        "source_status":   status,
        "spend":           float(m.get("SPEND_IN_DOLLAR") or 0),
        "n_conv":          int(m.get("TOTAL_WEB_CLICK_CHECKOUT") or 0),
        "revenue":         float(m.get("TOTAL_WEB_CLICK_CHECKOUT_VALUE_IN_MICRO_DOLLAR") or 0) / 1_000_000,
        "impressions":     int(m.get("TOTAL_IMPRESSION") or 0),
        "clicks":          int(m.get("TOTAL_CLICKTHROUGH") or 0),
    }


def collect_segment_rows(
    db, *, days: int = 180, today: Optional[date] = None, include_live: bool = True,
) -> tuple[list[dict], date, date]:
    """
    Assemble les lignes-segment brutes (historique + live) sur [today-days, today].
    Retourne (rows, period_start, period_end). N'écrit rien.
    """
    from models import AdGroup, Campaign, PinterestTargetingDaily

    today = today or date.today()
    start = today - timedelta(days=days)

    camp_status = {c.id: c.status for c in db.query(Campaign.id, Campaign.status).all()}
    ag_pairs = db.query(AdGroup.id, AdGroup.campaign_id).all()
    ag_map = {str(ag_id): (camp_id, camp_status.get(camp_id)) for ag_id, camp_id in ag_pairs}
    ag_ids = [str(ag_id) for ag_id, _ in ag_pairs]

    rows: list[dict] = []

    # Historique (> 90 j)
    hist = (
        db.query(PinterestTargetingDaily)
        .filter(PinterestTargetingDaily.stat_date >= start,
                PinterestTargetingDaily.stat_date <= today)
        .all()
    )
    rows.extend(_hist_row(r, ag_map) for r in hist)

    # Live (< 90 j)
    if include_live and ag_ids:
        from core.config import settings
        if settings.pinterest_ad_account_id and settings.pinterest_access_token:
            from api.pinterest import get_targeting_analytics
            live_start = max(start, today - timedelta(days=90))
            s, e = live_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
            for ttype in SEGMENT_TARGETING_TYPES:
                try:
                    raw = get_targeting_analytics(settings.pinterest_ad_account_id, ag_ids, ttype, s, e)
                except Exception as exc:
                    logger.warning("segment live %s: %s", ttype, exc)
                    continue
                for r in raw:
                    canon = _live_row(r, ttype, ag_map)
                    if canon:
                        rows.append(canon)

    return rows, start, today


# --- job ORM ----------------------------------------------------------------

def build_segment_performance(
    db, *, days: int = 180, today: Optional[date] = None, include_live: bool = True,
) -> int:
    """
    Construit/rafraîchit `segment_performance` pour la fenêtre [today-days, today].
    Idempotent : purge la fenêtre puis réinsère. Retourne le nombre de lignes écrites.
    """
    from models import SegmentPerformance

    rows, start, end = collect_segment_rows(db, days=days, today=today, include_live=include_live)
    aggregated = aggregate_segments(rows)

    db.query(SegmentPerformance).filter(
        SegmentPerformance.period_start == start,
        SegmentPerformance.period_end == end,
    ).delete(synchronize_session=False)

    now = datetime.utcnow()
    n = 0
    for g in aggregated:
        if g["spend"] <= 0 and g["impressions"] <= 0:
            continue
        db.add(SegmentPerformance(
            targeting_type=g["targeting_type"], targeting_value=g["targeting_value"],
            ad_group_id=g["ad_group_id"], campaign_id=g["campaign_id"],
            source_status=g["source_status"], spend=g["spend"], n_conv=g["n_conv"],
            revenue=g["revenue"], cpa_web_click=g["cpa_web_click"],
            roas_web_click=g["roas_web_click"], impressions=g["impressions"],
            clicks=g["clicks"], period_start=start, period_end=end, computed_at=now,
        ))
        n += 1
    db.commit()
    logger.info("build_segment_performance: %d lignes (%s → %s)", n, start, end)
    return n
