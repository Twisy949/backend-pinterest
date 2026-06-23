"""
Phase 5 — Diagnostic ENTITÉS (réf. spec §6, §7, §9).

  - learning_gating()  : robuste d'abord, API en confirmation.
  - detect_phase()     : TESTING | UNDERPOWERED | SCALING | DECLINING.
  - score_entity()     : délègue à scoring.compute_score (pinterest_performance_
                         score_spec) avec `save` en leading indicator + garde
                         anti-KILL si save-rate fort.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from optimizer.config import CONSTANTS, Constants, Economics
from optimizer.schemas import LearningState, Phase
from optimizer.schemas import EntityMetrics

# volume minimal pour juger une entité (conversions web-click sur la fenêtre)
MIN_CONV_TO_JUDGE = 5
# volume hebdo indicatif d'une entité en scaling prouvé
SCALING_CONV_WEEK = 25


@dataclass
class LearningGate:
    state: str
    structurally_starved: bool
    too_recent: bool
    under_powered: bool
    in_learning_api: bool
    reason: str

    @property
    def in_learning(self) -> bool:
        """Tout état non-`ready` interdit l'action (INV-5)."""
        return self.state != LearningState.READY


def _days_since(start, today: date) -> Optional[int]:
    if start is None:
        return None
    if isinstance(start, str):
        try:
            start = datetime.fromisoformat(start)
        except ValueError:
            return None
    if isinstance(start, datetime):
        start = start.date()
    if isinstance(start, date):
        return (today - start).days
    return None


def _api_in_learning(ad_group: dict) -> bool:
    if (ad_group.get("learning_mode") or "").upper() == "ACTIVE":
        return True
    opt = ad_group.get("optimization_goal")
    if isinstance(opt, dict):
        meta = opt.get("conversion_tag_v3_goal_metadata", {})
        if isinstance(meta, dict) and (meta.get("learning_mode_type") or "").upper() == "ACTIVE":
            return True
    return False


def learning_gating(
    ad_group: dict,
    metrics: EntityMetrics,
    target_cpa: float,
    *,
    today: Optional[date] = None,
    constants: Constants = CONSTANTS,
) -> LearningGate:
    """
    Classe l'état d'apprentissage. Signaux robustes (structure/volume/âge)
    d'abord ; le flag API ne fait que confirmer.
    """
    today = today or date.today()
    daily_budget = float(ad_group.get("daily_budget") or 0.0)
    starved = daily_budget > 0 and daily_budget < constants.BUDGET_FLOOR_X_CPA * target_cpa
    days = _days_since(ad_group.get("start_time") or ad_group.get("created_at"), today)
    too_recent = days is not None and days < constants.PATIENCE_POST_CHANGE
    under_powered = metrics.web_click_checkout < MIN_CONV_TO_JUDGE
    api_learning = _api_in_learning(ad_group)

    if starved:
        state, reason = LearningState.STRUCTURALLY_STARVED, (
            f"budget {daily_budget:.0f}€ < {constants.BUDGET_FLOOR_X_CPA}×CPA "
            f"({constants.BUDGET_FLOOR_X_CPA * target_cpa:.0f}€)")
    elif too_recent:
        state, reason = LearningState.TOO_RECENT, f"créé il y a {days} j (< patience)"
    elif api_learning:
        state, reason = LearningState.IN_LEARNING_API, "learning_mode API actif"
    elif under_powered:
        state, reason = LearningState.UNDER_POWERED, (
            f"{metrics.web_click_checkout:.0f} conv < {MIN_CONV_TO_JUDGE} (volume insuffisant)")
    else:
        state, reason = LearningState.READY, "prouvé : volume + budget + ancienneté OK"

    return LearningGate(state, starved, too_recent, under_powered, api_learning, reason)


def detect_phase(
    metrics: EntityMetrics,
    economics: Economics,
    daily_budget: float,
    target_cpa: float,
    *,
    prev: Optional[EntityMetrics] = None,
    constants: Constants = CONSTANTS,
) -> str:
    """
    Phase de cycle de vie. `prev` = fenêtre consolidée précédente (pour la
    tendance). Le ROAS est toujours web-click (INV-2).
    """
    conv = metrics.web_click_checkout
    roas = metrics.roas_web_click

    # DECLINING : tendance ROAS franchement à la baisse + fréquence qui monte.
    if prev is not None and prev.roas_web_click and roas is not None:
        delta = (roas - prev.roas_web_click) / prev.roas_web_click
        freq_rising = metrics.impression_frequency > (prev.impression_frequency or 0)
        if delta <= -0.20 and (freq_rising or metrics.impression_frequency >= 2.5):
            return Phase.DECLINING

    # sous-alimentation structurelle
    starved = daily_budget > 0 and daily_budget < constants.BUDGET_FLOOR_X_CPA * target_cpa

    # TESTING : trop peu de signal / trop jeune
    if conv < MIN_CONV_TO_JUDGE or metrics.n_days < 7:
        return Phase.TESTING

    # SCALING : rentable (>= break-even) avec un volume crédible
    weeks = max(metrics.n_days / 7.0, 1.0)
    conv_week = conv / weeks
    if roas is not None and roas >= economics.break_even_roas and conv_week >= SCALING_CONV_WEEK:
        return Phase.SCALING

    if starved:
        return Phase.UNDERPOWERED

    return Phase.TESTING


def _score_cfg(economics: Economics, calibration: Optional[dict] = None) -> dict:
    """Construit la config de scoring depuis l'économie dérivée + calibration."""
    from scoring import load_config
    cfg = load_config()
    cfg["breakeven_roas"] = economics.break_even_roas
    cfg["roas_target"] = economics.target_scale_roas
    if calibration:
        if calibration.get("GOOD_CPA", {}).get("value"):
            cfg["good_cpa"] = calibration["GOOD_CPA"]["value"]
        if calibration.get("KILL_CPA", {}).get("value"):
            cfg["kill_cpa"] = calibration["KILL_CPA"]["value"]
        if calibration.get("MEDIAN_CPA", {}).get("value"):
            cfg["target_cpa"] = calibration["MEDIAN_CPA"]["value"]
        if calibration.get("BENCH_SAVE_RATE", {}).get("good"):
            cfg["bench_save"] = calibration["BENCH_SAVE_RATE"]["good"]
    return cfg


def score_entity(
    metrics: EntityMetrics,
    economics: Economics,
    *,
    level: Optional[str] = None,
    in_learning: bool = False,
    prev_roas: Optional[float] = None,
    calibration: Optional[dict] = None,
) -> dict:
    """
    Score 0-100 (P×F×C×A×R×T) via scoring.compute_score + garde anti-KILL :
    si le verdict est KILL mais le save-rate est fort (≥ benchmark « bon »), on
    déclasse en FIX (le save est un leading indicator — l'attribution traîne).
    """
    from scoring import compute_score
    cfg = _score_cfg(economics, calibration)
    lvl = level or metrics.level

    result = compute_score(
        level=lvl,
        roas_web_click=metrics.roas_web_click or 0.0,
        spend=metrics.spend,
        impressions=int(metrics.impressions),
        clicks=int(metrics.clicks),
        web_click_checkout=int(metrics.web_click_checkout),
        add_to_cart=int(metrics.add_to_cart),
        saves=int(metrics.saves),
        outbound_clicks=int(metrics.outbound_clicks),
        video_3s=int(metrics.video_3sec_views),
        video_100=int(metrics.video_p100_complete),
        frequency=metrics.impression_frequency,
        web_view_checkout=int(metrics.web_view_checkout),
        roas_7d=metrics.roas_web_click,
        roas_prev_7d=prev_roas,
        in_learning=in_learning,
        cfg=cfg,
    )

    save_rate = metrics.save_rate or 0.0
    bench_save = cfg.get("bench_save") or 0.006
    if result["verdict"] == "KILL" and save_rate >= bench_save:
        result["verdict"] = "FIX"
        result["save_guard"] = True
        result["reason"] = (
            f"anti-KILL : save-rate fort ({save_rate:.4f} ≥ {bench_save:.4f}) — "
            "leading indicator, ne pas tuer (traîne d'attribution)."
        )
    else:
        result["save_guard"] = False
    return result
