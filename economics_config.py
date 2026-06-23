"""
Config économique runtime — MARGIN_CONTRIB / TOTAL_BUDGET_TARGET (spec §17).

Source de vérité : `.env` (via core.config.settings) fournit les valeurs initiales.
L'UI peut les surcharger à chaud → persistées dans `economics_config.json` (même
pattern que `score_config.json`). Sans fichier, on retombe sur `.env`.

INV-1 : `margin_contrib` invalide/absente ⇒ mode décision bloqué (validé en amont
par optimizer.config.require_margin).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from optimizer.config import OptimizerConfig

CONFIG_PATH = Path(__file__).resolve().parent.parent / "economics_config.json"

_KEYS = ("margin_contrib", "total_budget_target", "scale_safety")


def _parse_float(raw) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _env_defaults() -> dict:
    """Valeurs initiales depuis .env (core.config.settings)."""
    from core.config import settings
    return {
        "margin_contrib": _parse_float(getattr(settings, "margin_contrib", "")),
        "total_budget_target": _parse_float(getattr(settings, "total_budget_target", "")),
        "scale_safety": 0.8,
    }


def load_economics_config() -> dict:
    """Merge .env (défauts) + fichier persisté (surcharge UI)."""
    cfg = _env_defaults()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text("utf-8"))
            for k in _KEYS:
                if k in saved:
                    cfg[k] = _parse_float(saved[k]) if k != "scale_safety" else (
                        _parse_float(saved[k]) or 0.8)
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    return cfg


def _validate(cfg: dict) -> None:
    m = cfg.get("margin_contrib")
    if m is not None and not (0.0 < m <= 1.0):
        raise ValueError("margin_contrib doit être dans ]0, 1] (ou vide pour bloquer).")
    b = cfg.get("total_budget_target")
    if b is not None and b < 0:
        raise ValueError("total_budget_target doit être ≥ 0.")
    s = cfg.get("scale_safety")
    if s is not None and not (0.0 < s <= 1.0):
        raise ValueError("scale_safety doit être dans ]0, 1].")


def save_economics_config(updates: dict) -> dict:
    """Valide puis persiste les surcharges. Lève ValueError si invalide."""
    cfg = load_economics_config()
    for k in _KEYS:
        if k in updates:
            cfg[k] = _parse_float(updates[k])
    if cfg.get("scale_safety") is None:
        cfg["scale_safety"] = 0.8
    _validate(cfg)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    return cfg


def to_optimizer_config(overrides: Optional[dict] = None) -> OptimizerConfig:
    """Construit l'OptimizerConfig effectif (persisté + surcharges ponctuelles)."""
    cfg = load_economics_config()
    if overrides:
        for k in _KEYS:
            if overrides.get(k) is not None:
                cfg[k] = _parse_float(overrides[k])
    return OptimizerConfig(
        margin_contrib=cfg.get("margin_contrib"),
        total_budget_target=cfg.get("total_budget_target"),
        scale_safety=cfg.get("scale_safety") or 0.8,
    )
