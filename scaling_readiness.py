"""
SPEC scaling §2 — Gate 0 : READINESS (les 4 questions avant tout scale).

Avant de scaler (vertical OU horizontal) : tracking OK ? hors learning ? CPA stable
sur la fenêtre consolidée ? assez de conversions ? Sinon → FIX_FIRST (réparer d'abord,
ne rien scaler) en pointant le point faible.

Cœur PUR (testable sans DB). Tous les ratios sont web-click (INV-2).
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Optional

from optimizer.config import CONSTANTS, Constants

READY = "READY"
FIX_FIRST = "FIX_FIRST"


def daily_cpa_series(daily_rows: list[dict]) -> list[float]:
    """CPA web-click par jour (jours avec ≥1 conversion), depuis des lignes journalières."""
    out: list[float] = []
    for r in daily_rows:
        spend = float(r.get("spend", 0) or 0)
        conv = float(r.get("web_click_checkout", 0) or 0)
        if conv > 0 and spend > 0:
            out.append(spend / conv)
    return out


def stable(cpa_series: list[float], *, band: float = CONSTANTS.STABILITY_BAND,
           min_points: int = 3) -> bool:
    """
    CPA stable si le coefficient de variation (écart-type / moyenne) ≤ `band`
    sur la fenêtre. Demande au moins `min_points` jours avec conversions.
    """
    pts = [c for c in cpa_series if c and c > 0]
    if len(pts) < min_points:
        return False
    m = mean(pts)
    if m <= 0:
        return False
    return (pstdev(pts) / m) <= band


@dataclass
class Readiness:
    status: str            # READY | FIX_FIRST
    reason: str
    cv: Optional[float] = None

    @property
    def ready(self) -> bool:
        return self.status == READY


def assess_readiness(
    *, tracking_ok: bool, in_learning: bool, cpa_series: list[float], conv_count: float,
    constants: Constants = CONSTANTS,
) -> Readiness:
    """
    Gate 0 (§2). Ordre des contrôles = priorité du point faible à signaler.
    Renvoie FIX_FIRST dès le premier échec (le scale est interdit).
    """
    if not tracking_ok:
        return Readiness(FIX_FIRST, "tracking de conversion non vérifié (GATE-CAPI) — réparer d'abord.")
    if in_learning:
        return Readiness(FIX_FIRST, "campagne en learning — attendre la sortie avant de scaler.")
    if conv_count < constants.MIN_CONV_SCALE:
        return Readiness(FIX_FIRST,
                         f"{conv_count:.0f} conv < {constants.MIN_CONV_SCALE} — données insuffisantes pour scaler.")
    pts = [c for c in cpa_series if c and c > 0]
    cv = round(pstdev(pts) / mean(pts), 4) if len(pts) >= 3 and mean(pts) > 0 else None
    if not stable(cpa_series, band=constants.STABILITY_BAND):
        return Readiness(FIX_FIRST,
                         f"CPA instable (CV={cv if cv is not None else '—'} > {constants.STABILITY_BAND}) "
                         f"— stabiliser avant de scaler.", cv=cv)
    return Readiness(READY, "tracking OK, hors learning, CPA stable, volume suffisant.", cv=cv)
