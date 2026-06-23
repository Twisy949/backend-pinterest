"""
Suivi des actions de l'optimiseur (boucle fermée, réf. spec §12).

Cycle de vie d'une recommandation :
  PROPOSED (adoptée) → APPLIED (tu l'as faite dans Pinterest) → REVIEWED_T1 (J+7)
  → REVIEWED_T2 (J+18, outcome figé) | DISMISSED.

Reconnaissance de la campagne créée : auto par nom (`expected_name` ↔ campaigns.name)
+ secours manuel (`link_action`). Les reviews comparent le ROAS web-click réel à
l'objectif, en contrôlant la tendance du compte.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from optimizer.metrics_access import aggregate, consolidated_window
from optimizer.review import review_t1, review_t2
from optimizer.schemas import Outcome, Status

# statuts "vivants" (pour l'idempotence de l'adoption)
_LIVE = (Status.PROPOSED, Status.APPLIED, Status.REVIEWED_T1, Status.REVIEWED_T2)
_METRIC_FIELDS = (
    "spend", "impressions", "clicks", "saves", "outbound_clicks",
    "web_click_checkout", "web_view_checkout", "web_click_checkout_value",
    "add_to_cart", "video_3sec_views", "video_p100_complete", "impression_frequency",
)


def _to_date(v) -> Optional[date]:
    if v is None or isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def serialize(a) -> dict:
    def iso(x):
        return x.isoformat() if isinstance(x, (date, datetime)) else x
    return {
        "id": a.id, "type": a.type, "entity_level": a.entity_level,
        "entity_id": a.entity_id, "entity_name": a.entity_name, "params": a.params or {},
        "phase": a.phase, "rationale": a.rationale, "account_check": a.account_check,
        "hypothesis": a.hypothesis, "review_date_t1": iso(a.review_date_t1),
        "review_date_t2": iso(a.review_date_t2), "confidence": a.confidence,
        "confidence_basis": a.confidence_basis, "status": a.status, "outcome": a.outcome,
        "result_notes": a.result_notes, "applied_at": iso(a.applied_at),
        "reviewed_at": iso(a.reviewed_at), "created_at": iso(a.created_at),
        "linked": bool(a.entity_id),
    }


def adopt_candidate(db, candidate: dict) -> dict:
    """Persiste une reco dans le ledger `agent_actions` (idempotent)."""
    from models import AgentAction

    key_id = candidate.get("entity_id")
    key_name = candidate.get("entity_name")
    existing = (
        db.query(AgentAction)
        .filter(AgentAction.type == candidate.get("type"),
                AgentAction.status.in_(_LIVE))
        .all()
    )
    for e in existing:
        if (key_id and e.entity_id == key_id) or (not key_id and e.entity_name == key_name):
            return serialize(e)

    aa = AgentAction(
        type=candidate.get("type"), entity_level=candidate.get("entity_level"),
        entity_id=key_id, entity_name=key_name, params=candidate.get("params") or {},
        phase=candidate.get("phase"), rationale=candidate.get("rationale"),
        account_check=candidate.get("account_check"), hypothesis=candidate.get("hypothesis"),
        review_date_t1=_to_date(candidate.get("review_date_t1")),
        review_date_t2=_to_date(candidate.get("review_date_t2")),
        confidence=candidate.get("confidence"), confidence_basis=candidate.get("confidence_basis"),
        status=Status.PROPOSED, created_at=datetime.utcnow(),
    )
    db.add(aa)
    db.commit()
    db.refresh(aa)
    return serialize(aa)


def list_actions(db) -> list[dict]:
    from models import AgentAction
    rows = db.query(AgentAction).order_by(AgentAction.created_at.desc()).all()
    return [serialize(a) for a in rows]


def apply_action(db, action_id: int, today: Optional[date] = None) -> dict:
    """Marque APPLIED + recale les dates de review depuis aujourd'hui."""
    from models import AgentAction
    from optimizer.config import CONSTANTS
    today = today or date.today()
    a = db.get(AgentAction, action_id)
    if a is None:
        raise ValueError("action introuvable")
    a.status = Status.APPLIED
    a.applied_at = datetime.utcnow()
    a.review_date_t1 = today + timedelta(days=CONSTANTS.REVIEW_HORIZON_T1)
    a.review_date_t2 = today + timedelta(days=CONSTANTS.REVIEW_HORIZON_T2)
    db.commit()
    auto_link_by_name(db)            # une RELAUNCH a déjà son entity_id ; lie les créations
    db.refresh(a)
    return serialize(a)


def dismiss_action(db, action_id: int) -> dict:
    from models import AgentAction
    a = db.get(AgentAction, action_id)
    if a is None:
        raise ValueError("action introuvable")
    a.status = Status.DISMISSED
    db.commit()
    db.refresh(a)
    return serialize(a)


def create_manual_action(db, payload: dict, today: Optional[date] = None) -> dict:
    """
    Crée une action MANUELLE dans le même journal que les recos → l'IA connaît
    tous les changements et pourra l'évaluer (si reliée à une campagne).
    payload : {title, objective?, importance?, campaign_id?, note?, already_done?, completed_at?}
    """
    from models import AgentAction, Campaign
    from optimizer.config import CONSTANTS
    today = today or date.today()

    campaign_id = payload.get("campaign_id") or None
    name, level = None, "account"
    if campaign_id:
        camp = db.get(Campaign, str(campaign_id))
        name = camp.name if camp else None
        level = "campaign"

    title = (payload.get("title") or "").strip()
    note = (payload.get("note") or "").strip()
    if not title:
        raise ValueError("titre requis")
    rationale = title if not note else f"{title}\n{note}"

    aa = AgentAction(
        type="MANUAL", entity_level=level,
        entity_id=str(campaign_id) if campaign_id else None, entity_name=name,
        params={"manual": True, "objective": payload.get("objective"),
                "importance": payload.get("importance") or "normale", "note": note or None},
        rationale=rationale, status=Status.PROPOSED, created_at=datetime.utcnow(),
    )
    if payload.get("already_done"):
        applied = _to_date(payload.get("completed_at")) or today
        aa.status = Status.APPLIED
        aa.applied_at = datetime(applied.year, applied.month, applied.day)
        aa.review_date_t1 = applied + timedelta(days=CONSTANTS.REVIEW_HORIZON_T1)
        aa.review_date_t2 = applied + timedelta(days=CONSTANTS.REVIEW_HORIZON_T2)
    db.add(aa)
    db.commit()
    db.refresh(aa)
    return serialize(aa)


def delete_action(db, action_id: int) -> dict:
    from models import AgentAction
    a = db.get(AgentAction, action_id)
    if a is None:
        raise ValueError("action introuvable")
    db.delete(a)
    db.commit()
    return {"deleted": action_id}


def link_action(db, action_id: int, campaign_id: str) -> dict:
    """Secours manuel : relier une action à une campagne existante."""
    from models import AgentAction, Campaign
    a = db.get(AgentAction, action_id)
    if a is None:
        raise ValueError("action introuvable")
    camp = db.get(Campaign, campaign_id)
    if camp is None:
        raise ValueError("campagne introuvable")
    a.entity_id = camp.id
    if not a.entity_name:
        a.entity_name = camp.name
    db.commit()
    db.refresh(a)
    return serialize(a)


def auto_link_by_name(db) -> int:
    """Lie les CREATE_CAMPAIGN non reliées à la campagne dont le nom correspond."""
    from models import AgentAction, Campaign
    pending = (
        db.query(AgentAction)
        .filter(AgentAction.type == "CREATE_CAMPAIGN",
                AgentAction.entity_id.is_(None),
                AgentAction.status.in_(_LIVE))
        .all()
    )
    if not pending:
        return 0
    campaigns = db.query(Campaign).all()
    by_name = {(c.name or "").strip().lower(): c for c in campaigns}
    linked = 0
    for a in pending:
        expected = ((a.params or {}).get("expected_name") or a.entity_name or "").strip().lower()
        camp = by_name.get(expected)
        if camp is not None:
            a.entity_id = camp.id
            linked += 1
    if linked:
        db.commit()
    return linked


# --- reviews (boucle fermée) ------------------------------------------------

def _metric_rows(rows) -> list[dict]:
    out = []
    for m in rows:
        d = {"date": getattr(m, "date", None)}
        for f in _METRIC_FIELDS:
            d[f] = getattr(m, f, 0) or 0
        out.append(d)
    return out


def _entity_roas(db, level: str, entity_id: str, window) -> Optional[float]:
    from models import AdGroupMetric, Metric
    if level == "ad_group":
        rows = db.query(AdGroupMetric).filter(AdGroupMetric.ad_group_id == entity_id).all()
    else:
        rows = db.query(Metric).filter(Metric.campaign_id == entity_id).all()
    return aggregate(_metric_rows(rows), level=level, window=window).roas_web_click


def _account_roas(db, window) -> Optional[float]:
    from models import Metric
    rows = db.query(Metric).all()
    return aggregate(_metric_rows(rows), level="account", window=window).roas_web_click


def _usable_hypothesis(a) -> bool:
    h = a.hypothesis or {}
    return h.get("min_acceptable") is not None


def _manual_review(before, after, acc_before, acc_after, *, final: bool):
    """Évalue une action manuelle par delta avant/après, en contrôlant le compte."""
    if before is None or after is None:
        return ((Outcome.INCONCLUSIVE if final else None),
                "Pas assez d'historique avant/après pour conclure.")
    delta = f"ROAS {round(before, 2)} → {round(after, 2)}"
    improved = after > before
    acc_known = acc_before is not None and acc_after is not None
    acc_improved = acc_known and acc_after > acc_before
    if not final:
        return (None, f"{delta} (provisoire).")
    if improved and (acc_improved or not acc_known):
        return (Outcome.SUCCESS, f"{delta} : nette amélioration — bonne décision.")
    if improved and not acc_improved:
        return (Outcome.INCONCLUSIVE,
                f"{delta} pour l'entité mais le compte ne progresse pas (cannibalisme possible).")
    if not improved and acc_known and acc_after < acc_before:
        return (Outcome.INCONCLUSIVE, f"{delta} mais tout le compte baisse — non imputable.")
    return (Outcome.FAILURE, f"{delta} : pas d'amélioration.")


def run_due_reviews(db, today: Optional[date] = None) -> dict:
    """
    Évalue les actions (recos ET manuelles) arrivées à échéance (T1 puis T2).
    Reconnecte d'abord les campagnes créées par nom. Retourne un récap.
    """
    from models import AgentAction

    today = today or date.today()
    auto_link_by_name(db)

    win_now = consolidated_window(today)
    acc_after = _account_roas(db, win_now)

    reviewed_t1 = reviewed_t2 = skipped = 0
    actions = (
        db.query(AgentAction)
        .filter(AgentAction.status.in_((Status.APPLIED, Status.REVIEWED_T1)))
        .all()
    )
    for a in actions:
        due_t1 = a.status == Status.APPLIED and a.review_date_t1 and today >= a.review_date_t1
        due_t2 = a.status == Status.REVIEWED_T1 and a.review_date_t2 and today >= a.review_date_t2
        if not (due_t1 or due_t2):
            continue

        # action non reliée à une entité → on l'enregistre comme changement non mesurable
        if not a.entity_id:
            if due_t2:
                a.status, a.outcome = Status.REVIEWED_T2, Outcome.INCONCLUSIVE
                a.result_notes = "Changement enregistré (non reliée à une campagne → non mesurable automatiquement)."
                a.reviewed_at = datetime.utcnow()
                reviewed_t2 += 1
            else:
                a.status = Status.REVIEWED_T1
                a.result_notes = "Enregistrée — non reliée à une campagne, pas de mesure automatique."
                a.reviewed_at = datetime.utcnow()
                reviewed_t1 += 1
            continue

        observed = _entity_roas(db, a.entity_level, a.entity_id, win_now)
        if observed is None:
            skipped += 1
            continue
        acc_before = (_account_roas(db, consolidated_window(a.applied_at.date()))
                      if a.applied_at else None)

        if _usable_hypothesis(a):                       # recommandation chiffrée
            cand = _as_candidate(a)
            if due_t1:
                r = review_t1(cand, observed, account_before=acc_before, account_after=acc_after)
                a.params = {**(a.params or {}), "t1_outcome": r["outcome_provisional"]}
                notes, status, outcome = r["notes"], Status.REVIEWED_T1, None
            else:
                r = review_t2(cand, observed, account_before=acc_before, account_after=acc_after,
                              t1_outcome=(a.params or {}).get("t1_outcome"))
                notes, status, outcome = r["notes"], Status.REVIEWED_T2, r["outcome"]
        else:                                           # action manuelle : delta avant/après
            before = _entity_roas(db, a.entity_level, a.entity_id,
                                  consolidated_window(a.applied_at.date()) if a.applied_at else win_now)
            outcome, notes = _manual_review(before, observed, acc_before, acc_after, final=due_t2)
            status = Status.REVIEWED_T2 if due_t2 else Status.REVIEWED_T1

        tag = "T2" if due_t2 else "T1"
        a.status = status
        if outcome is not None:
            a.outcome = outcome
        a.result_notes = f"[{tag} {today}] {notes} (ROAS={round(observed, 2)})"
        a.reviewed_at = datetime.utcnow()
        reviewed_t2 += 1 if due_t2 else 0
        reviewed_t1 += 1 if due_t1 else 0

    db.commit()
    return {"reviewed_t1": reviewed_t1, "reviewed_t2": reviewed_t2,
            "skipped": skipped, "outcome_success": _count_outcome(db, Outcome.SUCCESS)}


def _count_outcome(db, outcome: str) -> int:
    from models import AgentAction
    return db.query(AgentAction).filter(AgentAction.outcome == outcome).count()


def _as_candidate(a):
    """Reconstruit un objet porteur de `.hypothesis` (Hypothesis) pour review.py."""
    from optimizer.schemas import Candidate, Hypothesis
    h = a.hypothesis or {}
    hyp = Hypothesis(
        metric=h.get("metric", "roas_web_click"), window=h.get("window", "7d_consolidated"),
        baseline=h.get("baseline"), expected=h.get("expected"),
        min_acceptable=h.get("min_acceptable"), horizon_t1=h.get("horizon_t1"),
        horizon_t2=h.get("horizon_t2"),
    )
    return Candidate(type=a.type, entity_level=a.entity_level, entity_id=a.entity_id,
                     entity_name=a.entity_name, hypothesis=hyp, phase=a.phase)
