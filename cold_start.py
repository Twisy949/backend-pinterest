"""
Phase 13 — Cold-start = UNE seule campagne de redémarrage (PATCH_BRIEF #3).

Déclenché par l'audit (P-ST5 FAIL = 0 campagne active). On met en commun TOUTES
les créas du compte (dédup par pin), on applique le break-even comme **plancher de
survie** (pas critère de choix), puis on sélectionne le **top N absolu** via un
**score d'élite multi-signal** (ROAS + volume + save + hook + rétention).

Sortie = **1 prescription** (CREATE_CAMPAIGN ou RELAUNCH, jamais les deux), 1 ad
group, audience = **union des audiences rentables** (intérêts + geo + COUNTRY),
bid AUTOMATIC_BID, budget ancré sur le CPA médian des créas retenues. La carte est
traçable et passe par les **mêmes gates** que toute reco (GATE-CAPI bloque si le
tracking n'est pas vérifié).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from optimizer.calibration import percentile
from optimizer.config import (
    AUDIENCE_SCORE_WEIGHTS, CONSTANTS, CREATIVE_SCORE_WEIGHTS, Constants, Economics,
    OptimizerConfig, anchor_target_cpa, normalize, normalize_low, recommend_daily_budget,
)
from optimizer.diagnostics_account import audience_label, creative_benchmarks, pool_all_creatives
from optimizer.schemas import ActionType, Candidate, Hypothesis


def account_is_cold(campaigns: list[dict]) -> bool:
    """P-ST5 : aucune campagne ACTIVE."""
    return not any((c.get("status") or "").upper() == "ACTIVE" for c in campaigns)


@dataclass
class ColdStartPlan:
    is_cold: bool
    margin_configured: bool = False
    prescription: Optional[Candidate] = None
    blocked: bool = False
    alerts: list[str] = field(default_factory=list)
    pool_size: int = 0
    n_eligible: int = 0
    notes: str = ""

    @property
    def candidates(self) -> list[Candidate]:
        return [self.prescription] if self.prescription is not None else []


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def score_creative(ad: dict, benchmarks: dict, *, break_even_roas: Optional[float],
                   weights: dict = CREATIVE_SCORE_WEIGHTS) -> float:
    """
    Score d'élite composite déterministe dans [0,1] (Patch #3 Fix 3).
    Rentabilité dominante : ROAS (0.40) + CPA (0.20) = 0.60 du score.
    """
    s_roas = normalize(ad.get("roas_web_click"), break_even_roas or 0.0, benchmarks["p95_roas"])
    s_cpa = normalize_low(ad.get("cpa_web_click"),
                          benchmarks.get("good_cpa", 0.0), benchmarks.get("ceiling_cpa", 1.0))
    s_volume = normalize(ad.get("web_click_checkout"),
                         CONSTANTS.MIN_CONV_TRUST_COLDSTART, benchmarks["p75_conv"])
    s_save = normalize(ad.get("save_rate"), 0.0, benchmarks["p75_save_rate"])
    s_hook = normalize(ad.get("hook"), 0.0, benchmarks["p75_hook_rate"])
    s_ret = normalize(ad.get("video_p75"), 0.0, benchmarks["p75_video_p75"])
    return round(weights["roas_web_click"] * s_roas + weights["cpa_web_click"] * s_cpa
                 + weights["volume"] * s_volume + weights["save_rate"] * s_save
                 + weights["hook"] * s_hook + weights["retention"] * s_ret, 4)


def score_audience(conv: float, roas: float, potential: float, *, benchmarks: dict,
                   conv_ceiling: float, weights: dict = AUDIENCE_SCORE_WEIGHTS) -> float:
    """
    Score d'audience [0,1] au-delà du ROAS seul : volume + fiabilité (trust) +
    ROAS + potentiel (meilleur sous-ensemble). Une grosse audience à ROAS moyen
    (US) n'est PAS écartée au profit d'une micro-audience à ROAS élevé.
    """
    s_volume = normalize(conv, 0.0, conv_ceiling)
    s_trust = normalize(conv, 0.0, CONSTANTS.LEARNING_CONV_WEEK)   # 50 conv ⇒ pleine confiance
    s_roas = normalize(roas, 0.0, benchmarks["p95_roas"])
    s_potential = normalize(potential, 0.0, benchmarks["p95_roas"])
    return round(weights["volume"] * s_volume + weights["trust"] * s_trust
                 + weights["roas_web_click"] * s_roas + weights["potential"] * s_potential, 4)


def _pct_rank(value, values) -> Optional[int]:
    vals = [v for v in values if v is not None]
    if not vals or value is None:
        return None
    below = sum(1 for v in vals if v <= value)
    return round(100 * below / len(vals))


def _score_basis(ad: dict, eligible: list[dict]) -> str:
    sr = _pct_rank(ad.get("save_rate"), [c["save_rate"] for c in eligible])
    hk = _pct_rank(ad.get("hook"), [c["hook"] for c in eligible])
    rt = _pct_rank(ad.get("video_p75"), [c["video_p75"] for c in eligible])
    # Stats agrégées par pin sur TOUTES les campagnes (somme pondérée par le volume) :
    # on expose le spend total et le nb de campagnes pour rendre la pondération lisible.
    spend = float(ad.get("spend", 0) or 0)
    nc = int(ad.get("n_campaigns", 1) or 1)
    camp = f" · {nc} campagnes" if nc > 1 else ""
    return (f"ROAS {ad['roas_web_click']} · {ad['web_click_checkout']} conv · "
            f"{spend:.0f} € spend{camp} · save P{sr} · hook P{hk} · rétention P{rt}")


_GENDER_FR_TO_CODE = {"femmes": "female", "hommes": "male"}


def _label_loc(label: str) -> str:
    parts = label.split(" · ")
    return parts[-1] if parts else label


def _label_gender(label: str) -> str:
    parts = label.split(" · ")
    return parts[0] if parts else label


def _fold_gender_into_all(groups: dict) -> dict:
    """
    « tous · LOC » (sans restriction de genre) inclut déjà « femmes/hommes · LOC ».
    On absorbe donc les variantes genrées dans le large de la même localisation
    (métriques sommées) → la liste et l'union ne sont plus redondantes.
    """
    all_locs = {_label_loc(k) for k in groups if _label_gender(k) == "tous"}
    if not all_locs:
        return groups
    out: dict = {}
    for key, grp in groups.items():
        loc = _label_loc(key)
        target = f"tous · {loc}" if (_label_gender(key) != "tous" and loc in all_locs) else key
        m = out.setdefault(target, {"spend": 0.0, "wccv": 0.0, "conv": 0.0,
                                    "sub_roas": [], "specs": []})
        m["spend"] += grp["spend"]
        m["wccv"] += grp["wccv"]
        m["conv"] += grp["conv"]
        m["sub_roas"] += grp["sub_roas"]
        m["specs"] += grp["specs"]
    return out


def _union_gender(labels: list[str]) -> set:
    """
    Union des genres des audiences incluses. Si l'une est « tous », ou si plusieurs
    genres distincts coexistent, l'union ne restreint PAS le genre (le large inclut
    le restreint). Sinon, le genre unique.
    """
    any_all = False
    specific: set = set()
    for lbl in labels:
        g = _label_gender(lbl)
        if g == "tous":
            any_all = True
        else:
            specific.add(_GENDER_FR_TO_CODE.get(g, g))
    return set() if (any_all or len(specific) > 1) else specific


def build_union_audience(ad_groups: list[dict], *, break_even_roas: Optional[float],
                         benchmarks: dict, constants: Constants = CONSTANTS,
                         whole_audience: bool = True) -> dict:
    """
    Union des audiences (segmentées GENRE × COUNTRY pour le cold-start). On NOTE
    chaque audience via un score multi-signal (volume + trust + ROAS + potentiel),
    pas le ROAS seul — une grosse audience à ROAS moyen (US) reste incluse.

    `whole_audience=True` (cold-start) : on prend TOUTE l'audience (toutes celles
    avec un minimum de fiabilité) ; on n'exclut QUE les audiences sans volume
    crédible (< MIN_CONV_TRUST conversions). Ajoute le targeting_type COUNTRY.
    """
    groups: dict = {}
    for g in ad_groups:
        spec = g.get("targeting_spec") or {}
        key = audience_label([spec])                       # GENRE · COUNTRY
        grp = groups.setdefault(key, {"spend": 0.0, "wccv": 0.0, "conv": 0.0,
                                      "sub_roas": [], "specs": []})
        agg = g.get("agg", {}) or {}
        grp["spend"] += float(agg.get("spend", 0) or 0)
        grp["wccv"] += float(agg.get("web_click_checkout_value", 0) or 0)
        grp["conv"] += float(agg.get("web_click_checkout", 0) or 0)
        if agg.get("roas_web_click"):
            grp["sub_roas"].append(float(agg["roas_web_click"]))
        grp["specs"].append(spec)

    # Pliage : « tous · LOC » absorbe « femmes/hommes · LOC » (le large inclut le restreint).
    groups = _fold_gender_into_all(groups)

    conv_ceiling = percentile([g["conv"] for g in groups.values()], 90) or 1.0
    scored = []
    for key, grp in groups.items():
        roas = grp["wccv"] / grp["spend"] if grp["spend"] > 0 else 0.0
        potential = max(grp["sub_roas"]) if grp["sub_roas"] else roas
        score = score_audience(grp["conv"], roas, potential, benchmarks=benchmarks,
                               conv_ceiling=conv_ceiling)
        scored.append({"audience": key, "score": score, "roas_web_click": round(roas, 2),
                       "conv": int(grp["conv"]), "potential_roas": round(potential, 2),
                       "specs": grp["specs"]})
    scored.sort(key=lambda a: a["score"], reverse=True)

    interest, location = set(), set()
    included, excluded = [], []
    for a in scored:
        # cold-start : on prend toute l'audience crédible (trust mini), jamais filtrée par ROAS
        keep = a["conv"] >= constants.MIN_CONV_TRUST if whole_audience else a["score"] >= 0.3
        if keep:
            included.append({k: a[k] for k in ("audience", "score", "roas_web_click", "conv", "potential_roas")})
            for spec in a["specs"]:
                interest |= {str(x) for x in (spec.get("INTEREST") or [])}
                location |= {str(x) for x in (spec.get("LOCATION") or []) or (spec.get("COUNTRY") or [])}
        else:
            excluded.append({"audience": a["audience"], "roas_web_click": a["roas_web_click"],
                             "conv": a["conv"], "reason": "volume insuffisant (trust)"})

    # Union GENRE déduite des libellés inclus : « tous » l'emporte (le large inclut le
    # restreint) — évite de restreindre par erreur à « female » quand des audiences
    # tous-genres rentables existent.
    gender = _union_gender([a["audience"] for a in included])

    targeting = {
        "INTEREST": sorted(interest),
        "GENDER": sorted(gender),
        "LOCATION": sorted(location),
        "COUNTRY": sorted(location),       # targeting_type COUNTRY (demandé)
        "TARGETING_STRATEGY": ["CHOOSE_YOUR_OWN"],
    }
    return {"targeting_spec": targeting, "included": included, "excluded": excluded,
            "audience_scores": [{k: a[k] for k in ("audience", "score", "roas_web_click", "conv", "potential_roas")}
                                for a in scored],
            "n_sources": len(groups)}


def cold_start_plan(
    campaigns: list[dict],
    ad_groups: list[dict],
    ads: list[dict],
    config: OptimizerConfig,
    economics: Optional[Economics] = None,
    *,
    tr1_status: Optional[str] = None,         # statut audit P-TR1 (GATE-CAPI)
    today: Optional[date] = None,
    constants: Constants = CONSTANTS,
) -> ColdStartPlan:
    today = today or date.today()
    margin_ok = config.margin_contrib is not None and economics is not None
    n_camp = len(campaigns)

    if not account_is_cold(campaigns):
        return ColdStartPlan(is_cold=False, margin_configured=margin_ok,
                             notes="compte actif — hors cold-start.")

    trigger = f"P-ST5 FAIL : 0 campagne active sur {n_camp}. Prescription de redémarrage consolidé."

    # GATE-CAPI : tracking non vérifié → bloquer la prescription
    if tr1_status == "FAIL":
        return ColdStartPlan(is_cold=True, margin_configured=margin_ok, blocked=True,
                             alerts=["GATE-CAPI : tracking non vérifié (P-TR1 FAIL). Corriger le "
                                     "tag de conversion avant de relancer — optimiser sur un "
                                     "signal cassé invalide tous les résultats."],
                             notes=trigger)

    # 1) pool dédupliqué + 2) plancher de survie
    pool = pool_all_creatives(ads)
    bench = creative_benchmarks(pool)
    be_roas = economics.break_even_roas if margin_ok else None
    be_cpa = economics.break_even_cpa if margin_ok else None
    eligible = [
        c for c in pool
        if c["web_click_checkout"] >= constants.MIN_CONV_TRUST_COLDSTART
        and (be_roas is None or c["roas_web_click"] >= be_roas)
    ]
    if not eligible:
        return ColdStartPlan(
            is_cold=True, margin_configured=margin_ok, pool_size=len(pool), n_eligible=0,
            alerts=["Aucune créa rentable fiable (ROAS ≥ break-even & volume suffisant). "
                    "Produire de nouveaux créatifs (CREATIVE_BRIEF) avant de relancer."],
            prescription=Candidate(
                type=ActionType.CREATIVE_BRIEF, entity_level="account", entity_id=None,
                entity_name="Brief créatif — relance", phase="COLD_START",
                params={"trigger": trigger, "reason": "no_profitable_creative"},
                rationale="Aucune créa ne dépasse le plancher de survie : créer de nouveaux "
                          "concepts avant de redémarrer.",
                review_date_t1=today + timedelta(days=constants.REVIEW_HORIZON_T1),
                review_date_t2=today + timedelta(days=constants.REVIEW_HORIZON_T2)),
            notes=trigger + " Aucune créa rentable.")

    # 3) score d'élite + 4) sélection top N
    for c in eligible:
        c["score"] = score_creative(c, bench, break_even_roas=be_roas)
        c["score_basis"] = _score_basis(c, eligible)
    eligible.sort(key=lambda c: (c["score"], c["roas_web_click"]), reverse=True)
    top_n = max(5, min(8, constants.CREATIVES_PER_COLDSTART))
    selected = eligible[:top_n]

    # 4) budget ancré sur le CPA médian des créas retenues
    cpas = [c["cpa_web_click"] for c in selected if c["cpa_web_click"] is not None]
    target_cpa = anchor_target_cpa(cpas, break_even_cpa=be_cpa, margin=config.margin_contrib)
    daily_budget = recommend_daily_budget(target_cpa, constants=constants)
    median_cpa = round(percentile(cpas, 50), 2) if cpas else None
    median_roas = round(percentile([c["roas_web_click"] for c in selected], 50), 2)
    total_conv = int(sum(c["web_click_checkout"] for c in selected))

    # 2) union d'audiences — toute l'audience crédible (score multi-signal, + COUNTRY)
    aud = build_union_audience(ad_groups, break_even_roas=be_roas, benchmarks=bench,
                               constants=constants, whole_audience=True)

    # RELAUNCH si l'essentiel du top N vit déjà dans une campagne ; sinon CREATE
    from collections import Counter
    conc = Counter(cid for c in selected for cid in c["source_campaigns"])
    dom_campaign, dom_count = conc.most_common(1)[0] if conc else (None, 0)
    name_of = {str(c["id"]): c.get("name") for c in campaigns}
    relaunch = dom_count >= constants.MIN_CREATIVES_KEPT and dom_campaign in name_of

    conf = round(_clamp(total_conv / 300.0) * 0.7 + _clamp(len(selected) / 8.0) * 0.3, 3)
    conf_basis = f"{total_conv} conversions prouvées sur {len(selected)} créas retenues ; IC CPA resserré."
    # On expose TOUT le pool éligible (classé par score), pas seulement le top N : la
    # liste élite reste recommandée (flag `elite`), mais l'utilisateur peut piocher
    # parmi toutes les créatives rentables fiables. Stats agrégées par pin (cross-campagnes).
    elite_pins = {c["pin_id"] for c in selected}
    creatives = [{"pin_id": c["pin_id"], "name": c["name"], "score": c["score"],
                  "score_basis": c["score_basis"], "cpa_web_click": c["cpa_web_click"],
                  "roas_web_click": c["roas_web_click"], "spend": round(float(c.get("spend", 0) or 0), 2),
                  "web_click_checkout": int(c.get("web_click_checkout", 0) or 0),
                  "n_campaigns": int(c.get("n_campaigns", 1) or 1),
                  "elite": c["pin_id"] in elite_pins} for c in eligible]
    gates_passed = ["GATE-CAPI", "GATE-5XCPA", "GATE-KILL3X", "GATE-DIVERSITY", "GATE-CANNIBAL"]
    prov = " (provisoire — marge non configurée)" if not margin_ok else ""

    params = {
        "trigger": trigger, "objective": "WEB_CONVERSION",
        "bid_strategy": "AUTOMATIC_BID", "is_roas_optimized": True, "cpa_reference_internal": True,
        "target_cpa": target_cpa, "daily_budget": daily_budget,
        "budget_basis": f"médiane CPA créas retenues ({median_cpa} €) × 50/7 (plancher 5×CPA)",
        "n_adgroups": constants.AD_GROUPS_PER_COLDSTART,
        "audience": aud["targeting_spec"],
        "audience_label": ", ".join(a["audience"] for a in aud["included"]) or "—",
        "audience_scores": aud["audience_scores"], "audience_excluded": aud["excluded"],
        "n_audiences_sources": aud["n_sources"],
        "creatives": creatives, "n_creatives": len(selected), "n_creatives_pool": len(eligible),
        "reuse_pins": [c["pin_id"] for c in selected],
        "proven_cpa": median_cpa, "proven_roas": median_roas, "proven_conv": total_conv,
        "gates_passed": gates_passed, "provisional": not margin_ok, "dormant_until_relaunch": True,
    }
    hyp = Hypothesis("roas_web_click", "7d_consolidated", baseline=median_roas,
                     expected=economics.target_scale_roas if margin_ok else None,
                     min_acceptable=be_roas, horizon_t1=constants.REVIEW_HORIZON_T1,
                     horizon_t2=constants.REVIEW_HORIZON_T2)
    t1 = today + timedelta(days=constants.REVIEW_HORIZON_T1)
    t2 = today + timedelta(days=constants.REVIEW_HORIZON_T2)

    if relaunch:
        cid = str(dom_campaign)
        params["expected_name"] = name_of.get(cid)
        prescription = Candidate(
            type=ActionType.RELAUNCH, entity_level="campaign", entity_id=cid,
            entity_name=name_of.get(cid), params={**params, "from_status": "PAUSED", "to_status": "ACTIVE"},
            phase="COLD_START", confidence=conf, confidence_basis=conf_basis,
            rationale=(f"{trigger} Réactiver « {name_of.get(cid)} » : elle concentre {dom_count} des "
                       f"{len(selected)} créas d'élite (coût/achat médian {median_cpa} €, ROAS "
                       f"{median_roas}). Enchère automatique. Budget {daily_budget} €/jour{prov}."),
            account_check="Une seule campagne de redémarrage (pas de create+reactivate).",
            hypothesis=hyp, review_date_t1=t1, review_date_t2=t2)
    else:
        suggested = f"Redémarrage consolidé — {today.isoformat()}"
        prescription = Candidate(
            type=ActionType.CREATE_CAMPAIGN, entity_level="campaign", entity_id=None,
            entity_name=suggested, params={**params, "expected_name": suggested},
            phase="COLD_START", confidence=conf, confidence_basis=conf_basis,
            rationale=(f"{trigger} Créer 1 campagne / 1 ad group réutilisant les {len(selected)} "
                       f"meilleures créas du compte (score multi-signal), audience = union des "
                       f"audiences rentables. Coût/achat médian prouvé {median_cpa} €, ROAS "
                       f"{median_roas}. Enchère automatique (ROAS-optimisé). Budget {daily_budget} €/jour{prov}."),
            account_check="Une seule campagne pour concentrer le volume et clearer le learning.",
            hypothesis=hyp, review_date_t1=t1, review_date_t2=t2)

    # Fix 5 — la prescription passe par le MÊME validateur que toute reco (gates).
    from optimizer.validator import GateContext, validate_candidate
    entity_cpa = {}
    if relaunch:
        dom_agg = next((c.get("agg", {}) for c in campaigns if str(c["id"]) == str(dom_campaign)), {})
        if dom_agg.get("cpa_web_click") is not None:
            entity_cpa[str(dom_campaign)] = dom_agg["cpa_web_click"]
    gate = GateContext(break_even_cpa=be_cpa, entity_cpa=entity_cpa,
                       tracking_ok=(tr1_status != "FAIL"), diversity_min=constants.MIN_CREATIVES_KEPT)
    known = {str(dom_campaign)} if relaunch else set()
    res = validate_candidate(prescription, known_entity_ids=known, gate_ctx=gate)
    prescription.params["gates_passed"] = gates_passed if res.ok else []
    blocked = not res.ok
    alerts = [] if res.ok else res.errors

    notes = (f"{trigger} 1 campagne, 1 ad group, {len(selected)} créas d'élite "
             f"(pool {len(pool)} → {len(eligible)} éligibles). {len(aud['excluded'])} "
             f"audience(s) exclue(s)." + (" Budgets provisoires (marge non configurée)." if not margin_ok else ""))
    return ColdStartPlan(is_cold=True, margin_configured=margin_ok,
                         prescription=prescription, blocked=blocked, alerts=alerts,
                         pool_size=len(pool), n_eligible=len(eligible), notes=notes)
