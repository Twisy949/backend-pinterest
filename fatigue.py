"""
Phase 7 — Fatigue créative (réf. spec §10).

  - fatigue_signature() : freq_rise & ctr_decline & save_decline & cpm_trend>=0.
  - discriminate()      : distingue fatigue CRÉATIVE vs AUDIENCE → action.
  - predict_fatigue()   : via la courbe de fatigue calibrée (Phase 4).

Garde clé : un CTR qui baisse alors que le SAVE tient = traîne d'attribution,
PAS une fatigue → aucune action (le save est un leading indicator).
"""
from __future__ import annotations

from typing import Optional

from optimizer.schemas import ActionType, EntityMetrics


def _cpm(m: EntityMetrics) -> Optional[float]:
    return (m.spend / m.impressions * 1000.0) if m.impressions > 0 else None


def fatigue_signature(
    now: EntityMetrics, prev: EntityMetrics, decline_ratio: float = 0.9
) -> dict:
    """
    Signature de fatigue sur deux fenêtres consolidées consécutives.
    `decline_ratio` : seuil de baisse (0.9 = -10 %).
    """
    ctr_now, ctr_prev = now.ctr, prev.ctr
    save_now, save_prev = now.save_rate, prev.save_rate
    cpm_now, cpm_prev = _cpm(now), _cpm(prev)

    freq_rise = now.impression_frequency > (prev.impression_frequency or 0)
    ctr_decline = bool(ctr_prev and ctr_now is not None and ctr_now < decline_ratio * ctr_prev)
    save_decline = bool(save_prev and save_now is not None and save_now < decline_ratio * save_prev)
    save_held = bool(save_prev and save_now is not None and save_now >= decline_ratio * save_prev)
    cpm_trend = (cpm_now - cpm_prev) if (cpm_now is not None and cpm_prev is not None) else 0.0
    cpm_rising = cpm_trend >= 0

    is_fatigued = freq_rise and ctr_decline and save_decline and cpm_rising
    return {
        "freq_rise": freq_rise,
        "ctr_decline": ctr_decline,
        "save_decline": save_decline,
        "save_held": save_held,
        "cpm_rising": cpm_rising,
        "cpm_trend": round(cpm_trend, 4),
        "is_fatigued": is_fatigued,
    }


def discriminate(
    ad_now: EntityMetrics, ad_prev: EntityMetrics,
    group_now: EntityMetrics, group_prev: EntityMetrics,
    *, decline_ratio: float = 0.9,
) -> dict:
    """
    Discrimine fatigue créative vs audience et renvoie l'action recommandée.

    Priorité :
      1. CTR↓ mais save tenu  → AUCUNE action (traîne d'attribution).
      2. tout l'ad group décline → fatigue AUDIENCE → EXPAND_AUDIENCE.
      3. seul le créatif décline → fatigue CRÉATIVE → CREATIVE_BRIEF / ADD_CREATIVES.
    """
    sig_ad = fatigue_signature(ad_now, ad_prev, decline_ratio)
    sig_grp = fatigue_signature(group_now, group_prev, decline_ratio)

    # (1) garde anti-refresh : CTR baisse mais le save tient
    if sig_ad["ctr_decline"] and sig_ad["save_held"]:
        return {
            "fatigue_type": "none",
            "action": None,
            "reason": "CTR↓ mais save tenu — traîne d'attribution, pas de refresh.",
            "signature": sig_ad,
        }

    # (2) fatigue d'AUDIENCE : tout l'ad group décline (CTR & save)
    group_declining = sig_grp["ctr_decline"] and sig_grp["save_decline"]
    if group_declining:
        return {
            "fatigue_type": "audience",
            "action": ActionType.EXPAND_AUDIENCE,
            "reason": "tout l'ad group décline (CTR & save) — saturation d'audience.",
            "signature": sig_grp,
        }

    # (3) fatigue CRÉATIVE : le créatif décline mais l'audience tient
    if sig_ad["is_fatigued"]:
        low_retention = (ad_now.retention or 1.0) < 0.15
        action = ActionType.CREATIVE_BRIEF if low_retention else ActionType.ADD_CREATIVES
        return {
            "fatigue_type": "creative",
            "action": action,
            "reason": ("rétention faible → nouveau hook (CREATIVE_BRIEF)"
                       if low_retention else "créatif usé → injecter de nouveaux créatifs"),
            "signature": sig_ad,
        }

    return {"fatigue_type": "none", "action": None,
            "reason": "pas de signature de fatigue.", "signature": sig_ad}


def predict_fatigue(current_freq: float, fatigue_curve: dict) -> dict:
    """
    Prédit la proximité de la fatigue à partir de la fréquence courante et de la
    courbe calibrée (Phase 4 : `decline_freq`).
    """
    decline_freq = float(fatigue_curve.get("decline_freq") or 2.5)
    headroom = round(decline_freq - current_freq, 3)
    return {
        "decline_freq": decline_freq,
        "current_freq": current_freq,
        "headroom": headroom,
        "will_fatigue_soon": current_freq >= decline_freq * 0.9,
        "basis": fatigue_curve.get("basis", "default"),
    }
