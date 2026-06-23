"""
Phase 3 — Accès métriques + gate fraîcheur/consolidation (réf. spec §3).

INV-2 : ROAS = web_click_checkout_value / spend. Jamais `roas`/`revenue` bruts,
        jamais view-through. Ce module ne lit que les colonnes web-click.
INV-3 : les fenêtres de décision/review excluent J et J-1.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Union

from optimizer.config import CONSTANTS, Constants
from optimizer.schemas import EntityMetrics

DateLike = Union[date, datetime, str]

# Colonnes sommées telles quelles depuis metrics/ad_group_metrics/ad_metrics.
# INV-2 : on ne somme PAS web_view_checkout_value ni revenue/roas — la décision
# n'utilise que la valeur web-click. web_view_checkout (compte) sert uniquement
# au discount de fiabilité (view_through_share), jamais au ROAS.
_SUMMABLE = (
    "spend", "impressions", "clicks", "saves", "outbound_clicks",
    "web_click_checkout", "web_view_checkout",
    "web_click_checkout_value",
    "add_to_cart", "video_3sec_views", "video_p100_complete",
)


def as_date(value: DateLike) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"date non reconnue : {value!r}")


def consolidated_window(
    today: DateLike, days: Optional[int] = None, constants: Constants = CONSTANTS
) -> tuple[date, date]:
    """
    Fenêtre consolidée fermée [start, end], inclusive, qui **exclut J et J-1**
    (INV-3). Avec `today` = J et days=7 → [J-8 .. J-2].
    """
    d = as_date(today)
    n = days if days is not None else constants.CONSOLIDATED_WINDOW_DAYS
    end = d - timedelta(days=constants.DECISION_LAG_DAYS)   # J-2
    start = end - timedelta(days=n - 1)                     # J-8
    return start, end


def _num(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def aggregate(
    daily_rows: Iterable[dict],
    *,
    level: str,
    entity_id: Optional[str] = None,
    window: Optional[tuple[date, date]] = None,
) -> EntityMetrics:
    """
    Agrège des lignes journalières (dicts) sur une fenêtre optionnelle.
    Ne calcule le ROAS/CPA que via les colonnes web-click (propriétés de
    EntityMetrics) — aucune lecture de `revenue`/`roas`/view-through ici.
    """
    sums = {k: 0.0 for k in _SUMMABLE}
    dates: list[date] = []
    freq_weighted = 0.0
    freq_imps = 0.0
    for row in daily_rows:
        rd = row.get("date")
        if rd is not None:
            rdate = as_date(rd)
            if window is not None and not (window[0] <= rdate <= window[1]):
                continue
            dates.append(rdate)
        for k in _SUMMABLE:
            if k in row:
                sums[k] += _num(row.get(k))
        imps = _num(row.get("impressions"))
        if imps > 0 and row.get("impression_frequency") is not None:
            freq_weighted += _num(row.get("impression_frequency")) * imps
            freq_imps += imps

    em = EntityMetrics(level=level, entity_id=entity_id, **sums)
    em.n_days = len({*dates})
    em.date_min = min(dates).isoformat() if dates else None
    em.date_max = max(dates).isoformat() if dates else None
    em.impression_frequency = round(freq_weighted / freq_imps, 4) if freq_imps > 0 else 0.0
    return em


def entity_metrics(
    daily_rows: Iterable[dict],
    *,
    level: str,
    entity_id: Optional[str] = None,
    today: Optional[DateLike] = None,
    days: Optional[int] = None,
) -> EntityMetrics:
    """Raccourci : agrège sur la fenêtre consolidée se terminant à `today`."""
    window = consolidated_window(today, days) if today is not None else None
    return aggregate(daily_rows, level=level, entity_id=entity_id, window=window)


def freshness_ok(
    synced_at: Optional[DateLike],
    now: Optional[datetime] = None,
    stale_hours: int = CONSTANTS.SYNC_STALE_HOURS,
) -> bool:
    """False si la dernière synchro date de plus de `stale_hours` (défaut 36h)."""
    if synced_at is None:
        return False
    now = now or datetime.utcnow()
    if isinstance(synced_at, str):
        synced = datetime.fromisoformat(synced_at)
    elif isinstance(synced_at, date) and not isinstance(synced_at, datetime):
        synced = datetime(synced_at.year, synced_at.month, synced_at.day)
    else:
        synced = synced_at
    return (now - synced) <= timedelta(hours=stale_hours)
