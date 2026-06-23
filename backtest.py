"""
Phase 14 — Backtest au niveau COMPTE (réf. spec §13.5).

On rejoue les recommandations sur l'historique en mesurant la trajectoire du
ROAS *du compte* (blended web-click, INV-2) vs une période témoin — PAS la
métrique de l'entité (sinon on reproduit le cannibalisme). Sortie : « le système
propose-t-il a posteriori les actions qui ont marché ? ».
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from optimizer.diagnostics_account import blended_test
from optimizer.metrics_access import as_date


def roas_web_click(rows: Iterable[dict]) -> Optional[float]:
    """ROAS web-click agrégé sur un ensemble de lignes (INV-2)."""
    spend = sum(float(r.get("spend") or 0) for r in rows)
    value = sum(float(r.get("web_click_checkout_value") or 0) for r in rows)
    return value / spend if spend > 0 else None


def split_series(daily: list[dict], split: date) -> tuple[list[dict], list[dict]]:
    """Coupe une série journalière en (avant, à partir de) `split`."""
    s = as_date(split)
    before = [r for r in daily if r.get("date") and as_date(r["date"]) < s]
    after = [r for r in daily if r.get("date") and as_date(r["date"]) >= s]
    return before, after


@dataclass
class BacktestResult:
    account_roas_before: Optional[float]
    account_roas_after: Optional[float]
    entity_roas_before: Optional[float]
    entity_roas_after: Optional[float]
    verdict: dict
    penalized: bool
    note: str


def backtest_recommendation(
    account_daily: list[dict], entity_daily: list[dict], split: date,
) -> BacktestResult:
    """
    Mesure une reco au niveau COMPTE. `penalized` = l'entité s'améliore mais le
    compte non (cannibalisme) → la reco est pénalisée même si l'entité « gagne ».
    """
    acc_b, acc_a = split_series(account_daily, split)
    ent_b, ent_a = split_series(entity_daily, split)
    a_before, a_after = roas_web_click(acc_b), roas_web_click(acc_a)
    e_before, e_after = roas_web_click(ent_b), roas_web_click(ent_a)

    verdict = blended_test(a_before or 0.0, a_after or 0.0, e_before or 0.0, e_after or 0.0)
    penalized = verdict["cannibalized"]
    note = ("PÉNALISÉE : entité positive mais compte négatif (cannibalisme)."
            if penalized else
            ("validée : le compte progresse." if verdict["account_improved"]
             else "neutre/négative au niveau compte."))
    return BacktestResult(a_before, a_after, e_before, e_after, verdict, penalized, note)


@dataclass
class BacktestReport:
    results: list[BacktestResult] = field(default_factory=list)

    @property
    def n_penalized(self) -> int:
        return sum(1 for r in self.results if r.penalized)

    @property
    def n_account_positive(self) -> int:
        return sum(1 for r in self.results if r.verdict["account_improved"])

    def summary(self) -> dict:
        return {
            "n_scenarios": len(self.results),
            "n_account_positive": self.n_account_positive,
            "n_penalized_cannibalism": self.n_penalized,
        }


def backtest_report(scenarios: list[dict]) -> BacktestReport:
    """
    `scenarios` : dicts {account_daily, entity_daily, split}. Génère le rapport
    de backtest account-level.
    """
    report = BacktestReport()
    for s in scenarios:
        report.results.append(
            backtest_recommendation(s["account_daily"], s["entity_daily"], s["split"]))
    return report
