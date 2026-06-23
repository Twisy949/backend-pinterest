"""
Phase 12 — Review & Mémoire (boucle fermée, réf. spec §12).

  - review_t1() : 1re lecture (J+7) — provisoire, contrôle de la tendance compte.
  - review_t2() : 2e lecture (~J+18) — fige SUCCESS|FAILURE|INCONCLUSIVE
                  (capte la traîne d'attribution).
  - distill_heuristics() : agrège les outcomes par pattern_key → account_heuristics.

Contrôle de tendance : si TOUT le compte baisse, l'action n'est pas blâmée
(FAILURE → INCONCLUSIVE). À T2, atteindre le plancher `min_acceptable` = SUCCESS
(la traîne a validé l'action a posteriori).
"""
from __future__ import annotations

from typing import Iterable, Optional

from optimizer.schemas import Candidate, Hypothesis, Outcome, Status


def _higher_better(metric: str) -> bool:
    return "cpa" not in (metric or "").lower()   # roas → haut mieux ; cpa → bas mieux


def _meets(observed: float, threshold: Optional[float], higher_better: bool) -> bool:
    if threshold is None:
        return True
    return observed >= threshold if higher_better else observed <= threshold


def _account_declining(before: Optional[float], after: Optional[float], higher_better: bool) -> bool:
    if before is None or after is None:
        return False
    return after < before if higher_better else after > before


def _evaluate(
    hyp: Hypothesis, observed: float, *, final: bool,
    account_before: Optional[float], account_after: Optional[float],
) -> tuple[str, str]:
    hb = _higher_better(hyp.metric)
    meets_expected = _meets(observed, hyp.expected, hb)
    meets_min = _meets(observed, hyp.min_acceptable, hb)
    declining = _account_declining(account_before, account_after, hb)

    if final:
        if meets_min:
            return Outcome.SUCCESS, "T2 : plancher atteint (traîne d'attribution validée)."
        if declining:
            return Outcome.INCONCLUSIVE, "T2 : sous plancher mais tout le compte baisse — non imputable."
        return Outcome.FAILURE, "T2 : sous le plancher min_acceptable."
    # T1 (provisoire)
    if meets_expected:
        return Outcome.SUCCESS, "T1 : cible atteinte (provisoire)."
    if meets_min:
        return Outcome.INCONCLUSIVE, "T1 : entre plancher et cible (provisoire)."
    if declining:
        return Outcome.INCONCLUSIVE, "T1 : sous plancher mais le compte baisse — non imputable."
    return Outcome.FAILURE, "T1 : sous le plancher (provisoire)."


def review_t1(
    candidate: Candidate, observed: float, *,
    account_before: Optional[float] = None, account_after: Optional[float] = None,
) -> dict:
    outcome, notes = _evaluate(candidate.hypothesis, observed, final=False,
                               account_before=account_before, account_after=account_after)
    return {"status": Status.REVIEWED_T1, "outcome_provisional": outcome,
            "observed": observed, "notes": notes}


def review_t2(
    candidate: Candidate, observed: float, *,
    account_before: Optional[float] = None, account_after: Optional[float] = None,
    t1_outcome: Optional[str] = None,
) -> dict:
    outcome, notes = _evaluate(candidate.hypothesis, observed, final=True,
                               account_before=account_before, account_after=account_after)
    if t1_outcome == Outcome.FAILURE and outcome == Outcome.SUCCESS:
        notes = "Rattrapé par la traîne d'attribution entre T1 et T2. " + notes
    return {"status": Status.REVIEWED_T2, "outcome": outcome,
            "observed": observed, "notes": notes}


# --- distillation de la mémoire ---------------------------------------------

def pattern_key(action_type: str, phase: Optional[str]) -> str:
    return f"{action_type}_{phase or 'NA'}"


def distill_heuristics(actions: Iterable[dict]) -> list[dict]:
    """
    Agrège les outcomes par pattern_key → lignes account_heuristics.
    `actions` : dicts {type, phase, outcome}. Seul SUCCESS compte comme succès ;
    INCONCLUSIVE ne compte pas comme essai tranché (mais reste un trial).
    """
    agg: dict[str, dict] = {}
    for a in actions:
        if not a.get("outcome"):
            continue
        key = pattern_key(a.get("type"), a.get("phase"))
        row = agg.setdefault(key, {"pattern_key": key, "n_trials": 0, "n_success": 0,
                                   "n_failure": 0, "n_inconclusive": 0})
        row["n_trials"] += 1
        if a["outcome"] == Outcome.SUCCESS:
            row["n_success"] += 1
        elif a["outcome"] == Outcome.FAILURE:
            row["n_failure"] += 1
        else:
            row["n_inconclusive"] += 1

    rows = []
    for key, row in agg.items():
        rate = row["n_success"] / row["n_trials"] if row["n_trials"] else None
        rows.append({
            "pattern_key": key,
            "description": f"{row['n_success']}/{row['n_trials']} succès",
            "n_trials": row["n_trials"],
            "n_success": row["n_success"],
            "stats": {"success_rate": round(rate, 3) if rate is not None else None,
                      "n_failure": row["n_failure"], "n_inconclusive": row["n_inconclusive"]},
        })
    return rows


def write_heuristics(db, rows: list[dict]) -> int:
    """Upsert idempotent dans account_heuristics (par pattern_key)."""
    from datetime import datetime

    from models import AccountHeuristic
    n = 0
    for r in rows:
        existing = db.query(AccountHeuristic).filter_by(pattern_key=r["pattern_key"]).one_or_none()
        if existing is None:
            db.add(AccountHeuristic(updated_at=datetime.utcnow(), **r))
        else:
            existing.description = r["description"]
            existing.n_trials = r["n_trials"]
            existing.n_success = r["n_success"]
            existing.stats = r["stats"]
            existing.updated_at = datetime.utcnow()
        n += 1
    db.commit()
    return n


def memory_for_candidates(db) -> dict:
    """Charge account_heuristics → dict {pattern_key: {n_trials, n_success}} (derive_confidence)."""
    from models import AccountHeuristic
    out = {}
    for h in db.query(AccountHeuristic).all():
        out[h.pattern_key] = {"n_trials": h.n_trials, "n_success": h.n_success}
    return out
