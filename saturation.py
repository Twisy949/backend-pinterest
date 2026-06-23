"""
SPEC scaling §3 — Détecteur de SATURATION (le pivot de l'arbitrage).

Mesure si le vertical (hausses de budget) rend encore, depuis l'historique des
hausses (config_snapshots) + métriques. Construit la relation budget → CPA marginal
et détecte l'inflexion où le CPA s'envole. Proxy si pas d'historique de hausse.

Décision : pas saturé → VERTICAL (inventaire dispo) ; saturé → HORIZONTAL.
Cœur PUR. ROAS/CPA web-click (INV-2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from optimizer.config import CONSTANTS, Constants

VERTICAL = "VERTICAL"
HORIZONTAL = "HORIZONTAL"


def marginal_return_curve(steps: list[dict]) -> list[dict]:
    """
    `steps` : hausses de budget passées, chacune {budget_level, delta_spend, delta_conv}
    (Δ mesurés sur la fenêtre stable suivant la hausse). Retourne la courbe
    [{budget_level, marginal_cpa}] triée par budget, + un flag `rising` à l'inflexion
    où le CPA marginal dépasse le point précédent.
    """
    pts = []
    for s in steps:
        dc = float(s.get("delta_conv", 0) or 0)
        ds = float(s.get("delta_spend", 0) or 0)
        if dc > 0 and ds > 0:
            pts.append({"budget_level": float(s.get("budget_level", 0) or 0),
                        "marginal_cpa": round(ds / dc, 4)})
    pts.sort(key=lambda p: p["budget_level"])
    prev = None
    for p in pts:
        p["rising"] = bool(prev is not None and p["marginal_cpa"] > prev)
        prev = p["marginal_cpa"]
    return pts


def recent_marginal_cpa(curve: list[dict]) -> Optional[float]:
    """CPA marginal au plus haut palier de budget connu (None si courbe vide)."""
    return curve[-1]["marginal_cpa"] if curve else None


@dataclass
class SaturationSignals:
    avg_cpa: Optional[float] = None
    marginal_cpa: Optional[float] = None
    reach_flat: bool = False           # portée (impressions uniques) plate
    frequency_rising: bool = False     # fréquence ↑
    roas_declining: bool = False       # ROAS ↓ à créa stable (hors fatigue)
    has_history: bool = True           # une hausse de budget mesurable existe
    # proxy si pas d'historique de hausse :
    cpa_rising: bool = False
    conv_flat: bool = False
    spend_stable_or_up: bool = False


@dataclass
class SaturationResult:
    saturated: bool
    reason: str
    marginal_cpa: Optional[float] = None
    avg_cpa: Optional[float] = None


def near_saturation(sig: SaturationSignals, *, constants: Constants = CONSTANTS) -> SaturationResult:
    """Le vertical sature-t-il ? (§3). Combine CPA marginal, portée/fréquence, ROAS, proxy."""
    if sig.has_history and sig.marginal_cpa is not None and sig.avg_cpa:
        if sig.marginal_cpa > constants.SATURATION_CPA_MULT * sig.avg_cpa:
            return SaturationResult(True, (f"CPA marginal {sig.marginal_cpa} > "
                                           f"{constants.SATURATION_CPA_MULT}×CPA moyen ({sig.avg_cpa})"),
                                    sig.marginal_cpa, sig.avg_cpa)
    if sig.reach_flat and sig.frequency_rising:
        return SaturationResult(True, "portée plate + fréquence en hausse", sig.marginal_cpa, sig.avg_cpa)
    if sig.roas_declining:
        return SaturationResult(True, "ROAS en baisse à créa stable", sig.marginal_cpa, sig.avg_cpa)
    if not sig.has_history and sig.cpa_rising and sig.conv_flat and sig.spend_stable_or_up:
        return SaturationResult(True, "proxy : CPA ↑, conversions plates, dépense stable/↑",
                                sig.marginal_cpa, sig.avg_cpa)
    return SaturationResult(False, "inventaire encore disponible (vertical rentable)",
                            sig.marginal_cpa, sig.avg_cpa)


def arbitrate_scale(sat: SaturationResult) -> str:
    """Pas saturé → VERTICAL ; saturé → HORIZONTAL (§3)."""
    return HORIZONTAL if sat.saturated else VERTICAL


# --- §4 branche VERTICAL : plafond de saturation + rollback ------------------

def vertical_blocked_by_saturation(marginal_cpa: Optional[float], avg_cpa: Optional[float],
                                   *, constants: Constants = CONSTANTS) -> bool:
    """Ne pas scaler dans le mur : refuser le vertical si le CPA marginal dépasse déjà le seuil."""
    if marginal_cpa is None or not avg_cpa:
        return False
    return marginal_cpa > constants.SATURATION_CPA_MULT * avg_cpa


def should_rollback_vertical(
    baseline_cpa: Optional[float], current_cpa: Optional[float], hours_since: Optional[float],
    *, roas: Optional[float] = None, breakeven_roas: Optional[float] = None,
) -> tuple[bool, str]:
    """
    §4 — conditions de rollback après une hausse : CPA +25 % vs baseline pré-hausse
    sous 72 h, ou ROAS hors zone (sous le break-even) → réduire d'abord, diagnostiquer ensuite.
    """
    if (baseline_cpa and current_cpa and hours_since is not None
            and hours_since <= 72 and current_cpa > baseline_cpa * 1.25):
        return True, f"CPA {current_cpa} > +25 % vs baseline {baseline_cpa} sous 72 h — réduire d'abord."
    if roas is not None and breakeven_roas is not None and roas < breakeven_roas:
        return True, f"ROAS {roas} sous le break-even {breakeven_roas} (hors zone) — réduire d'abord."
    return False, ""
