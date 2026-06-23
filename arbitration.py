"""
Phase 9 — Arbitrage account-level + budget global (réf. spec §8, sortie allocation).

  - ordonne les candidats par rendement marginal décroissant ;
  - refuse un scale qui empiète sur un cluster déjà couvert → propose MERGE ;
  - respecte TOTAL_BUDGET_TARGET (le delta budget cumulé ne le dépasse pas).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from optimizer.schemas import ActionType, Candidate

# actions correctrices : priorité maximale (libèrent du budget / stoppent la perte)
_CORRECTIVE = {ActionType.KILL, ActionType.BUDGET_DOWN}
_SCALE = {ActionType.BUDGET_UP}


def incremental_budget(c: Candidate) -> float:
    """Delta budget demandé par le candidat (>0 = consomme du budget)."""
    p = c.params or {}
    if c.type == ActionType.BUDGET_UP:
        return float(p.get("to", 0) or 0) - float(p.get("from", 0) or 0)
    if c.type == ActionType.BUDGET_DOWN:
        return float(p.get("to", 0) or 0) - float(p.get("from", 0) or 0)  # négatif
    return 0.0


def marginal_return(c: Candidate) -> float:
    """
    Proxy de rendement marginal. Correctif > scaling efficace > reste.
    Pour BUDGET_UP : ROAS web-click (baseline de l'hypothèse) × confiance.
    """
    if c.type in _CORRECTIVE:
        return 1e6                                  # toujours retenu en tête
    conf = c.confidence if c.confidence is not None else 0.5
    if c.type == ActionType.BUDGET_UP:
        roas = (c.hypothesis.baseline if c.hypothesis else None) or 0.0
        return roas * conf
    return conf


@dataclass
class ArbitrationResult:
    retained: list[Candidate]
    merged: list[Candidate]
    deferred: list[Candidate]

    def all(self) -> list[Candidate]:
        return self.retained + self.merged + self.deferred


def _merge_candidate(loser: Candidate, winner: Candidate) -> Candidate:
    return Candidate(
        type=ActionType.MERGE,
        entity_level=loser.entity_level,
        entity_id=loser.entity_id,
        entity_name=loser.entity_name,
        params={"into": winner.entity_id, "into_name": winner.entity_name,
                "cluster_key": loser.cluster_key, "reason": "cluster déjà couvert"},
        phase=loser.phase,
        rationale=(f"Cluster {loser.cluster_key} déjà couvert par "
                   f"{winner.entity_name} (meilleur rendement) — fusionner/réallouer "
                   f"plutôt que scaler en parallèle (anti-cannibalisme)."),
        account_check="éviter deux scales concurrents sur la même audience.",
        hypothesis=loser.hypothesis,
        review_date_t1=loser.review_date_t1, review_date_t2=loser.review_date_t2,
        confidence=loser.confidence, confidence_basis=loser.confidence_basis,
        cluster_key=loser.cluster_key,
    )


def arbitrate(
    candidates: list[Candidate],
    *,
    total_budget_target: Optional[float] = None,
) -> ArbitrationResult:
    """
    Filtre/ordonne les candidats. Deux BUDGET_UP sur un même cluster → le moins
    rentable devient un MERGE. Au-delà de TOTAL_BUDGET_TARGET → deferred.
    """
    ordered = sorted(candidates, key=marginal_return, reverse=True)

    merged: list[Candidate] = []
    cluster_winner: dict[str, Candidate] = {}
    survivors: list[Candidate] = []

    for c in ordered:
        if c.type in _SCALE and c.cluster_key:
            if c.cluster_key in cluster_winner:
                merged.append(_merge_candidate(c, cluster_winner[c.cluster_key]))
                continue
            cluster_winner[c.cluster_key] = c
        survivors.append(c)

    if total_budget_target is None:
        return ArbitrationResult(retained=survivors, merged=merged, deferred=[])

    retained: list[Candidate] = []
    deferred: list[Candidate] = []
    budget_used = 0.0
    for c in survivors:
        inc = incremental_budget(c)
        if inc <= 0:                                # correctif / libère du budget
            retained.append(c)
            budget_used += inc
            continue
        if budget_used + inc <= total_budget_target:
            retained.append(c)
            budget_used += inc
        else:
            deferred.append(c)
    return ArbitrationResult(retained=retained, merged=merged, deferred=deferred)
