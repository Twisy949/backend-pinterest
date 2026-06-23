"""
Phase 1 — réconciliation du schéma : backfill du ledger + job de snapshot config.

Les nouvelles TABLES sont créées par `Base.metadata.create_all` (modèles ajoutés
à `models.py`). Ce module fournit :
  - `targeting_hash()`        : empreinte stable d'un ciblage (diff rapide) ;
  - `build_config_snapshots()`: constructeur PUR (testable sans DB) ;
  - `snapshot_config(db)`     : job quotidien (upsert config_snapshots) ;
  - `backfill_agent_actions(db)` : copie idempotente actions(legacy) → agent_actions.

Décision brownfield (cf. CONTEXT.md) : on ne renomme PAS `actions`/`recommendations`
en `*_legacy` (la feature Actions live en dépend) ; on backfill sans perte vers le
ledger canonique `agent_actions`, qui reçoit désormais les écritures du moteur.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Clés de ciblage retenues pour l'empreinte (ordre stable).
_TARGETING_KEYS = ("INTEREST", "GENDER", "LOCATION", "AGE_BUCKET",
                   "TARGETING_STRATEGY", "APPTYPE")


def normalize_targeting(spec: Optional[dict]) -> dict:
    """Normalise un targeting_spec : ne garde que les clés signifiantes, triées."""
    if not isinstance(spec, dict):
        return {}
    out: dict[str, list] = {}
    for key in _TARGETING_KEYS:
        val = spec.get(key)
        if isinstance(val, list) and val:
            out[key] = sorted(str(v) for v in val)
    return out


def targeting_hash(spec: Optional[dict]) -> Optional[str]:
    """sha1 du ciblage normalisé (None si vide)."""
    norm = normalize_targeting(spec)
    if not norm:
        return None
    payload = json.dumps(norm, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _is_performance_plus(name: Optional[str], optimization_goal) -> bool:
    if name and "performance" in name.lower():
        return True
    if isinstance(optimization_goal, dict):
        meta = optimization_goal.get("conversion_tag_v3_goal_metadata", {})
        if isinstance(meta, dict) and meta.get("is_roas_optimized"):
            return True
    return False


def build_config_snapshots(
    campaigns: Iterable[dict],
    ad_groups: Iterable[dict],
    ads: Iterable[dict],
    snapshot_date: Optional[date] = None,
) -> list[dict]:
    """
    Constructeur PUR : produit une ligne config_snapshots par entité (active ET
    en pause). Accepte des dicts génériques (ORM-as-dict ou dump). Les champs
    JSON peuvent être des dicts déjà parsés ou des chaînes.
    """
    snap = snapshot_date or date.today()
    rows: list[dict] = []

    for c in campaigns:
        rows.append({
            "entity_level": "campaign",
            "entity_id": str(c.get("id")),
            "snapshot_date": snap,
            "status": c.get("status"),
            "objective": c.get("objective"),
            "daily_budget": _f(c.get("daily_budget")),
            "lifetime_budget": _f(c.get("lifetime_budget")),
            "bid": None,
            "bid_strategy": None,
            "optimization_goal": None,
            "targeting_spec": None,
            "targeting_hash": None,
            "is_performance_plus": _is_performance_plus(c.get("name"), None),
        })

    for g in ad_groups:
        opt = _as_json(g.get("optimization_goal"))
        tgt = _as_json(g.get("targeting_spec"))
        rows.append({
            "entity_level": "ad_group",
            "entity_id": str(g.get("id")),
            "snapshot_date": snap,
            "status": g.get("status"),
            "objective": g.get("objective"),
            "daily_budget": _f(g.get("daily_budget")),
            "lifetime_budget": _f(g.get("lifetime_budget")),
            "bid": _f(g.get("bid")),
            "bid_strategy": g.get("bid_strategy"),
            "optimization_goal": opt,
            "targeting_spec": tgt,
            "targeting_hash": targeting_hash(tgt),
            "is_performance_plus": _is_performance_plus(g.get("name"), opt),
        })

    for a in ads:
        rows.append({
            "entity_level": "ad",
            "entity_id": str(a.get("id")),
            "snapshot_date": snap,
            "status": a.get("status"),
            "objective": None,
            "daily_budget": None,
            "lifetime_budget": None,
            "bid": None,
            "bid_strategy": None,
            "optimization_goal": None,
            "targeting_spec": None,
            "targeting_hash": None,
            "is_performance_plus": False,
        })

    return rows


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_json(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return None
    return v


# --- jobs ORM (nécessitent une session DB) ----------------------------------

def snapshot_config(db, snapshot_date: Optional[date] = None) -> int:
    """
    Job quotidien : écrit/rafraîchit une ligne config_snapshots par entité
    (campagnes + ad groups + ads, actives ET en pause — lues depuis la DB, pas
    l'API qui ne rend que l'actif). Idempotent par (entity_level, entity_id,
    snapshot_date). Retourne le nombre de lignes upsertées.
    """
    from models import Ad, AdGroup, Campaign, ConfigSnapshot

    snap = snapshot_date or date.today()
    campaigns = [_orm_dict(c) for c in db.query(Campaign).all()]
    ad_groups = [_orm_dict(g) for g in db.query(AdGroup).all()]
    ads = [_orm_dict(a) for a in db.query(Ad).all()]
    rows = build_config_snapshots(campaigns, ad_groups, ads, snap)

    n = 0
    for r in rows:
        existing = (
            db.query(ConfigSnapshot)
            .filter_by(entity_level=r["entity_level"], entity_id=r["entity_id"],
                       snapshot_date=r["snapshot_date"])
            .one_or_none()
        )
        if existing is None:
            db.add(ConfigSnapshot(**r))
        else:
            for k, v in r.items():
                setattr(existing, k, v)
        n += 1
    db.commit()
    logger.info("snapshot_config: %d lignes upsertées pour %s", n, snap)
    return n


_STATUS_MAP = {"pending": "PROPOSED", "done": "APPLIED"}


def backfill_agent_actions(db) -> int:
    """
    Copie idempotente `actions` (legacy) → `agent_actions`. Ne supprime/renomme
    rien. Ré-exécutable : saute les lignes déjà importées (params.legacy_action_id).
    Retourne le nombre de lignes nouvellement insérées.
    """
    from sqlalchemy import inspect

    from models import Action, AgentAction

    insp = inspect(db.get_bind())
    if "actions" not in insp.get_table_names():
        return 0

    already = set()
    for aa in db.query(AgentAction).all():
        if isinstance(aa.params, dict) and "legacy_action_id" in aa.params:
            already.add(aa.params["legacy_action_id"])

    inserted = 0
    for a in db.query(Action).all():
        if a.id in already:
            continue
        rationale = a.title or ""
        if a.description:
            rationale = f"{rationale}\n{a.description}".strip()
        db.add(AgentAction(
            type=f"LEGACY_{(a.objective or 'NA').upper()}",
            entity_level="campaign",
            entity_id=a.campaign_id,
            entity_name=a.campaign_name,
            params={"legacy_action_id": a.id, "importance": a.importance},
            rationale=rationale,
            status=_STATUS_MAP.get(a.status, "PROPOSED"),
            result_notes=a.result_notes,
            confidence=None,
            created_at=a.created_at,
            applied_at=a.completed_at,
        ))
        inserted += 1
    db.commit()
    logger.info("backfill_agent_actions: %d lignes importées", inserted)
    return inserted


def _orm_dict(obj) -> dict:
    from sqlalchemy import inspect as sa_inspect
    return {c.key: getattr(obj, c.key) for c in sa_inspect(obj).mapper.column_attrs}
