"""
Schémas partagés du moteur d'optimisation (dataclasses, sans dépendance ORM).

`Candidate` est le schéma d'action canonique (v2 §11.1) — il se sérialise tel
quel vers la table `agent_actions` (Phase 1).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Optional


# --- vocabulaire (chaînes, alignées sur agent_actions / v2) -----------------

class Level:
    CAMPAIGN = "campaign"
    AD_GROUP = "ad_group"
    AD = "ad"
    ACCOUNT = "account"


class ActionType:
    RELAUNCH = "RELAUNCH"
    KILL = "KILL"
    BUDGET_UP = "BUDGET_UP"
    BUDGET_DOWN = "BUDGET_DOWN"
    CHANGE_BID_STRATEGY = "CHANGE_BID_STRATEGY"
    CHANGE_OPTIMIZATION = "CHANGE_OPTIMIZATION"
    EDIT_ADGROUP_CPA = "EDIT_ADGROUP_CPA"
    ADD_CREATIVES = "ADD_CREATIVES"
    EXPAND_AUDIENCE = "EXPAND_AUDIENCE"
    CREATE_CAMPAIGN = "CREATE_CAMPAIGN"
    CREATIVE_BRIEF = "CREATIVE_BRIEF"
    MERGE = "MERGE"
    REALLOCATE = "REALLOCATE"
    # Segmentation par targeting_types (SPEC v2 §4/§7/§8)
    SPLIT_SEGMENT = "SPLIT_SEGMENT"        # isoler un segment gagnant en AG/campagne
    EXCLUDE_SEGMENT = "EXCLUDE_SEGMENT"    # exclure un segment perdant du large


class Phase:
    TESTING = "TESTING"
    UNDERPOWERED = "UNDERPOWERED"
    SCALING = "SCALING"
    DECLINING = "DECLINING"


class LearningState:
    TOO_RECENT = "too_recent"
    UNDER_POWERED = "under_powered"
    STRUCTURALLY_STARVED = "structurally_starved"
    IN_LEARNING_API = "in_learning_api"
    READY = "ready"


class Status:
    PROPOSED = "PROPOSED"
    APPLIED = "APPLIED"
    REVIEWED_T1 = "REVIEWED_T1"
    REVIEWED_T2 = "REVIEWED_T2"
    DISMISSED = "DISMISSED"


class Outcome:
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class Hypothesis:
    """Prédiction falsifiable attachée à une action (v2 §11/§12)."""
    metric: str                      # ex: "cpa_web_click", "roas_web_click"
    window: str                      # ex: "7d_consolidated"
    baseline: Optional[float] = None
    expected: Optional[float] = None
    min_acceptable: Optional[float] = None
    horizon_t1: Optional[int] = None  # jours
    horizon_t2: Optional[int] = None  # jours

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Candidate:
    """Action proposée par le moteur de règles. Mappe 1:1 sur `agent_actions`."""
    type: str
    entity_level: str
    entity_id: Optional[str] = None
    entity_name: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)
    phase: Optional[str] = None
    rationale: str = ""
    account_check: str = ""
    hypothesis: Optional[Hypothesis] = None
    review_date_t1: Optional[date] = None
    review_date_t2: Optional[date] = None
    # DÉRIVÉE par le moteur — jamais posée par le LLM (INV-4).
    confidence: Optional[float] = None
    confidence_basis: str = ""
    cluster_key: Optional[str] = None  # pour l'arbitrage anti-cannibalisme

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.hypothesis is not None:
            d["hypothesis"] = self.hypothesis.to_dict()
        for k in ("review_date_t1", "review_date_t2"):
            if isinstance(d[k], date):
                d[k] = d[k].isoformat()
        return d


@dataclass
class EntityMetrics:
    """Métriques agrégées sur une fenêtre — toujours web-click pour la monnaie."""
    level: str
    entity_id: Optional[str] = None
    spend: float = 0.0
    impressions: float = 0.0
    clicks: float = 0.0
    saves: float = 0.0
    outbound_clicks: float = 0.0
    web_click_checkout: float = 0.0
    web_view_checkout: float = 0.0
    web_click_checkout_value: float = 0.0
    web_view_checkout_value: float = 0.0
    add_to_cart: float = 0.0
    video_3sec_views: float = 0.0
    video_p100_complete: float = 0.0
    impression_frequency: float = 0.0  # moyenne (non sommable) — dernière/representative
    n_days: int = 0
    date_min: Optional[str] = None
    date_max: Optional[str] = None

    # ---- métriques dérivées (INV-2 : ROAS = web_click uniquement) ----
    @property
    def roas_web_click(self) -> Optional[float]:
        return self.web_click_checkout_value / self.spend if self.spend > 0 else None

    @property
    def cpa_web_click(self) -> Optional[float]:
        return self.spend / self.web_click_checkout if self.web_click_checkout > 0 else None

    @property
    def ctr(self) -> Optional[float]:
        return self.clicks / self.impressions if self.impressions > 0 else None

    @property
    def outbound_ctr(self) -> Optional[float]:
        return self.outbound_clicks / self.impressions if self.impressions > 0 else None

    @property
    def save_rate(self) -> Optional[float]:
        return self.saves / self.impressions if self.impressions > 0 else None

    @property
    def hook_rate(self) -> Optional[float]:
        return self.video_3sec_views / self.impressions if self.impressions > 0 else None

    @property
    def retention(self) -> Optional[float]:
        return self.video_p100_complete / self.video_3sec_views if self.video_3sec_views > 0 else None

    @property
    def add_to_cart_cr(self) -> Optional[float]:
        return self.add_to_cart / self.clicks if self.clicks > 0 else None

    @property
    def checkout_cr(self) -> Optional[float]:
        return self.web_click_checkout / self.clicks if self.clicks > 0 else None

    @property
    def view_through_share(self) -> float:
        total = self.web_click_checkout + self.web_view_checkout
        return self.web_view_checkout / total if total > 0 else 0.0

    def to_dict(self) -> dict:
        base = asdict(self)
        base.update({
            "roas_web_click": self.roas_web_click,
            "cpa_web_click": self.cpa_web_click,
            "ctr": self.ctr,
            "outbound_ctr": self.outbound_ctr,
            "save_rate": self.save_rate,
            "hook_rate": self.hook_rate,
            "retention": self.retention,
        })
        return base
