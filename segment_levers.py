"""
SPEC v2 §6/§7/§8 — Leviers de segmentation (cœur PUR) :
  - §7 levier de SCALE horizontal : proposer une campagne KEYWORD dédiée ;
  - §4/§9 traduction des décisions de segment en candidats SPLIT_SEGMENT / EXCLUDE_SEGMENT
    (plafonnés par l'anti-fragmentation P-ST1) ;
  - §8 tests de nouveaux segments (PINNER peuplé corroborant un intérêt non ciblé).

Réutilise les briques existantes : `Candidate`/`Hypothesis` (schemas), `_review_dates`
+ `derive_confidence` (candidates), `recommend_daily_budget` (config), `overlap_score`
(diagnostics_account, GATE-CANNIBAL §6).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from optimizer.config import CONSTANTS, Constants, Economics, recommend_daily_budget
from optimizer.candidates import _review_dates, derive_confidence
from optimizer.schemas import ActionType, Candidate, EntityMetrics, Hypothesis, Level, Phase

# Valeurs « targeting » automatiques Pinterest : pas des keywords/segments isolables.
AUTO_BUCKETS = {"performance+ targeting", "performance + targeting", "(automatic)", "automatic", "—"}

# targeting_type → clé de targeting_spec (pour le calcul de chevauchement §6).
_TYPE_TO_SPEC_KEY = {
    "TARGETED_INTEREST": "INTEREST", "PINNER_INTEREST": "INTEREST",
    "KEYWORD": "KEYWORD", "GENDER": "GENDER", "AGE_BUCKET": "AGE_BUCKET",
    "COUNTRY": "LOCATION", "REGION": "LOCATION",
}


def _is_real_value(value) -> bool:
    v = str(value or "").strip()
    return bool(v) and v.lower() not in AUTO_BUCKETS


def segment_to_spec(targeting_type: str, value: str) -> dict:
    """Mini targeting_spec d'un segment isolé (pour overlap_score §6)."""
    key = _TYPE_TO_SPEC_KEY.get(targeting_type, targeting_type)
    return {key: [str(value)]}


def _seg_metrics(seg: dict, period_days: int) -> EntityMetrics:
    spend = float(seg.get("spend", 0) or 0)
    roas  = seg.get("roas_web_click")
    revenue = float(seg.get("revenue", roas * spend if roas else 0) or 0)
    return EntityMetrics(level="ad_group", spend=spend,
                         web_click_checkout=float(seg.get("n_conv", 0) or 0),
                         web_click_checkout_value=revenue, n_days=period_days)


def _mk(action: str, *, level: str, params: dict, rationale: str, hypothesis: Hypothesis,
        metrics: EntityMetrics, today: date, constants: Constants, account_check: str = "",
        cluster_key: Optional[str] = None, entity_name: Optional[str] = None,
        entity_id: Optional[str] = None) -> Candidate:
    t1, t2 = _review_dates(today, constants)
    c = Candidate(
        type=action, entity_level=level, entity_id=entity_id, entity_name=entity_name,
        params=params, phase=None, rationale=rationale, account_check=account_check,
        hypothesis=hypothesis, review_date_t1=t1, review_date_t2=t2, cluster_key=cluster_key,
    )
    c.confidence, c.confidence_basis = derive_confidence(c, metrics)
    return c


# --- §7 levier campagne KEYWORD au scale ------------------------------------

def rising_marginal_cpa(curr_cpa: Optional[float], prev_cpa: Optional[float],
                        *, ratio: float = 1.05) -> bool:
    """Le CPA marginal grimpe (le vertical rend moins) — si fenêtre précédente connue."""
    if curr_cpa is None or prev_cpa is None or prev_cpa <= 0:
        return False
    return curr_cpa > prev_cpa * ratio


def near_saturation(cpa: Optional[float], economics: Economics,
                    *, freq: Optional[float] = None, freq_ceiling: float = 2.5) -> bool:
    """Proxy de saturation : CPA proche du break-even, ou fréquence élevée (fatigue)."""
    if freq is not None and freq >= freq_ceiling:
        return True
    return cpa is not None and cpa >= economics.break_even_cpa * 0.9


def harvest_keywords(segments: list[dict], *, use_pinner: bool,
                     min_keywords: int = CONSTANTS.KEYWORD_MIN) -> tuple[list[str], bool]:
    """
    Récolte des keywords (§7.2) : keywords déjà convertis + intérêts ciblés gagnants
    (graines) + PINNER si peuplé. Déduplique, ignore les buckets automatiques.
    Retourne (keywords, enough≥min_keywords).
    """
    kws: list[str] = []
    seen: set[str] = set()

    def add(v):
        if not _is_real_value(v):
            return
        v = str(v).strip()
        k = v.lower()
        if k not in seen:
            seen.add(k)
            kws.append(v)

    for s in segments:
        if s.get("targeting_type") == "KEYWORD" and (s.get("n_conv") or 0) > 0:
            add(s.get("targeting_value"))
    winners = sorted(
        (s for s in segments
         if s.get("targeting_type") == "TARGETED_INTEREST" and (s.get("n_conv") or 0) > 0),
        key=lambda s: -(s.get("roas_web_click") or 0),
    )
    for s in winners:
        add(s.get("targeting_value"))
    if use_pinner:
        for s in segments:
            if s.get("targeting_type") == "PINNER_INTEREST" and (s.get("n_conv") or 0) > 0:
                add(s.get("targeting_value"))

    return kws, len(kws) >= min_keywords


def propose_keyword_campaign(
    core: dict, account: dict, segments: list[dict], economics: Economics,
    *, n_elite_creatives: int, use_pinner: bool, period_days: int,
    today: Optional[date] = None, constants: Constants = CONSTANTS,
) -> Optional[Candidate]:
    """
    §7 — propose une campagne KEYWORD au scale si TOUTES les conditions tiennent :
    cœur en SCALING, gros budget/jour, le compte soutient 2 campagnes à 50 conv/sem,
    et le vertical sature. Sinon None.
    """
    today = today or date.today()
    if core.get("phase") != Phase.SCALING:
        return None
    if float(core.get("daily_budget") or 0) < constants.KEYWORD_SCALE_BUDGET:
        return None
    if float(account.get("conv_week_total") or 0) < 2 * constants.LEARNING_CONV_WEEK:
        return None
    saturating = rising_marginal_cpa(core.get("cpa_web_click"), core.get("prev_cpa")) or \
        near_saturation(core.get("cpa_web_click"), economics, freq=core.get("impression_frequency"))
    if not saturating:
        return None

    keywords, enough = harvest_keywords(segments, use_pinner=use_pinner,
                                        min_keywords=constants.KEYWORD_MIN)
    if not enough:
        return None  # Pinterest recommande ≥ 25 keywords/AG — sinon on ne propose pas

    target_cpa = economics.break_even_cpa
    budget = recommend_daily_budget(target_cpa, constants=constants)
    metrics = _seg_metrics({"spend": core.get("daily_budget", 0),
                            "n_conv": core.get("n_conv", 0),
                            "roas_web_click": core.get("roas_web_click")}, period_days)
    hyp = Hypothesis("roas_web_click", "7d_consolidated", baseline=core.get("roas_web_click"),
                     expected=economics.target_scale_roas, min_acceptable=economics.break_even_roas,
                     horizon_t1=constants.REVIEW_HORIZON_T1, horizon_t2=constants.REVIEW_HORIZON_T2)
    params = {
        "lever": "HORIZONTAL_SCALE", "targeting": {"KEYWORD": keywords},
        "n_keywords": len(keywords), "keyword_min": constants.KEYWORD_MIN,
        "bid": "AUTOMATIC_BID", "daily_budget": budget, "target_cpa": target_cpa,
        "n_creatives": n_elite_creatives, "source_campaign": core.get("id"),
    }
    return _mk(
        ActionType.CREATE_CAMPAIGN, level=Level.CAMPAIGN, params=params,
        rationale=(f"SCALING horizontal : le vertical sature (CPA {core.get('cpa_web_click')} ≈ "
                   f"break-even {economics.break_even_cpa}) — ouvrir une campagne KEYWORD "
                   f"({len(keywords)} keywords) sur une poche d'intention fraîche."),
        account_check="pool d'intention distinct du large/intérêt — vérifier le non-chevauchement (GATE-CANNIBAL).",
        hypothesis=hyp, metrics=metrics, today=today, constants=constants,
        entity_name="[KEYWORD] scale horizontal",
    )


# --- §4/§9 décisions de segment → candidats ---------------------------------

def antifragmentation_cap(conv_week_total: float, *, constants: Constants = CONSTANTS) -> int:
    """P-ST1 — nombre de structures séparées que le compte peut nourrir (~conv/sem ÷ 50)."""
    return max(1, int(float(conv_week_total or 0) // constants.LEARNING_CONV_WEEK))


def segment_candidates(
    decisions: list[dict], economics: Economics, *, conv_week_total: float,
    n_elite_creatives: int, period_days: int, today: Optional[date] = None,
    constants: Constants = CONSTANTS,
) -> list[Candidate]:
    """
    Traduit les décisions SPLIT/DROP en candidats. SPLIT plafonnés par P-ST1
    (les plus gros budgets d'abord). DROP → EXCLUDE_SEGMENT (correctif).
    """
    today = today or date.today()
    out: list[Candidate] = []
    # Les buckets automatiques Pinterest (Performance+, Automatic) ne sont pas des
    # segments isolables/excluables : on ne propose ni SPLIT ni EXCLUDE dessus.
    decisions = [d for d in decisions if _is_real_value(d.get("targeting_value"))]

    splits = sorted((d for d in decisions if d.get("decision") == "SPLIT"),
                    key=lambda d: -(d.get("spend") or 0))
    cap = antifragmentation_cap(conv_week_total, constants=constants)
    for d in splits[:cap]:
        target_cpa = d.get("cpa_web_click") or economics.break_even_cpa
        budget = recommend_daily_budget(target_cpa, constants=constants)
        metrics = _seg_metrics(d, period_days)
        hyp = Hypothesis("roas_web_click", "7d_consolidated", baseline=d.get("roas_web_click"),
                         expected=economics.target_scale_roas, min_acceptable=economics.break_even_roas,
                         horizon_t1=constants.REVIEW_HORIZON_T1, horizon_t2=constants.REVIEW_HORIZON_T2)
        out.append(_mk(
            ActionType.SPLIT_SEGMENT, level=Level.AD_GROUP,
            params={"targeting_type": d["targeting_type"], "targeting_value": d["targeting_value"],
                    "target_cpa": target_cpa, "daily_budget": budget, "n_creatives": n_elite_creatives},
            rationale=(f"SPLIT : {d['targeting_type']} « {d['targeting_value']} » sur-performe "
                       f"(ROAS {d.get('roas_web_click')} vs {d.get('account_baseline_roas')}) avec "
                       f"le volume requis — isoler en AG/campagne dédié."),
            account_check="vérifier le non-chevauchement avec une campagne active (GATE-CANNIBAL).",
            hypothesis=hyp, metrics=metrics, today=today, constants=constants,
            entity_name=f"[SPLIT] {d['targeting_value']}",
        ))

    for d in (x for x in decisions if x.get("decision") == "DROP"):
        metrics = _seg_metrics(d, period_days)
        hyp = Hypothesis("account_roas_web_click", "7d_consolidated", baseline=d.get("roas_web_click"),
                         horizon_t1=constants.REVIEW_HORIZON_T1, horizon_t2=constants.REVIEW_HORIZON_T2)
        out.append(_mk(
            ActionType.EXCLUDE_SEGMENT, level=Level.AD_GROUP,
            params={"targeting_type": d["targeting_type"], "targeting_value": d["targeting_value"],
                    "reason": "perdant net fiable"},
            rationale=(f"EXCLUDE : {d['targeting_type']} « {d['targeting_value']} » sous-performe "
                       f"nettement (ROAS {d.get('roas_web_click')} < {d.get('account_baseline_roas')}, "
                       f"n={d.get('n_conv')}) — exclure du large."),
            account_check="exclusion libère du budget vers le cœur.",
            hypothesis=hyp, metrics=metrics, today=today, constants=constants,
            entity_name=f"[EXCLUDE] {d['targeting_value']}",
        ))
    return out


# --- §8 tests de nouveaux segments ------------------------------------------

def propose_segment_tests(
    segments: list[dict], decisions: list[dict], economics: Economics, *,
    use_pinner: bool, period_days: int, n_elite_creatives: int,
    today: Optional[date] = None, constants: Constants = CONSTANTS,
) -> list[tuple[Candidate, dict]]:
    """
    §8 — propose des TESTS de segments hypothétiques : un PINNER_INTEREST peuplé qui
    convertit mais n'est pas déjà ciblé en TARGETED_INTEREST → candidat de test isolé.
    Retourne [(Candidate, segment_test_record)].
    """
    today = today or date.today()
    if not use_pinner:
        return []

    targeted = {str(s.get("targeting_value")).lower() for s in segments
                if s.get("targeting_type") == "TARGETED_INTEREST"}
    baseline = next((d.get("account_baseline_roas") for d in decisions
                     if d.get("account_baseline_roas") is not None), None)

    out: list[tuple[Candidate, dict]] = []
    pinners = sorted(
        (s for s in segments if s.get("targeting_type") == "PINNER_INTEREST"
         and (s.get("n_conv") or 0) >= constants.MIN_CONV_SEGMENT and _is_real_value(s.get("targeting_value"))),
        key=lambda s: -(s.get("roas_web_click") or 0),
    )
    for s in pinners:
        val = str(s.get("targeting_value"))
        if val.lower() in targeted:
            continue  # déjà ciblé explicitement
        if baseline is not None and (s.get("roas_web_click") or 0) <= baseline:
            continue  # ne corrobore pas un gagnant
        target_cpa = s.get("cpa_web_click") or economics.break_even_cpa
        budget = recommend_daily_budget(target_cpa, constants=constants)
        metrics = _seg_metrics(s, period_days)
        hyp = Hypothesis("roas_web_click", "7d_consolidated", baseline=s.get("roas_web_click"),
                         expected=economics.target_scale_roas, min_acceptable=economics.break_even_roas,
                         horizon_t1=constants.REVIEW_HORIZON_T1, horizon_t2=constants.REVIEW_HORIZON_T2)
        cand = _mk(
            ActionType.CREATE_CAMPAIGN, level=Level.CAMPAIGN,
            params={"lever": "TEST", "targeting": {"INTEREST": [val]},
                    "daily_budget": budget, "target_cpa": target_cpa,
                    "n_creatives": n_elite_creatives, "source": "PINNER"},
            rationale=(f"TEST : intérêt « {val} » convertit côté PINNER (ROAS {s.get('roas_web_click')}) "
                       f"mais n'est pas ciblé en TARGETED — tester isolé contre le large."),
            account_check="un seul segment isolé ; ne pas affamer le cœur ; GATE-CANNIBAL.",
            hypothesis=hyp, metrics=metrics, today=today, constants=constants,
            entity_name=f"[TEST] {val}",
        )
        out.append((cand, {
            "targeting_type": "TARGETED_INTEREST", "targeting_value": val,
            "source": "PINNER", "status": "PROPOSED",
            "rationale": cand.rationale,
        }))
    return out


# --- GATE-CANNIBAL sur propositions (§6/§9) ---------------------------------

def cannibal_conflict(spec: dict, active_specs: list[dict], threshold: float) -> Optional[float]:
    """
    Retourne le score de chevauchement max avec une campagne active si ≥ seuil
    (proposition cannibale à bloquer), sinon None.
    """
    from optimizer.diagnostics_account import overlap_score
    best = 0.0
    for a in active_specs:
        best = max(best, overlap_score(spec, a))
    return round(best, 4) if best >= threshold else None


def _proposed_spec(c: Candidate) -> dict:
    """Targeting d'une proposition pour le calcul de chevauchement (§6)."""
    p = c.params or {}
    if p.get("targeting"):                       # CREATE_CAMPAIGN (keyword / test)
        return p["targeting"]
    if p.get("targeting_type"):                  # SPLIT_SEGMENT
        return segment_to_spec(p["targeting_type"], p.get("targeting_value"))
    return {}


# --- job ORM : orchestrateur des recommandations de segmentation ------------

def build_segment_recommendations(db, *, config=None, today: Optional[date] = None) -> dict:
    """
    Assemble les candidats issus de la segmentation (§4/§7/§8), applique
    GATE-CANNIBAL (§6) sur les propositions budgétées et la validation standard,
    persiste les tests proposés. Lecture seule hormis l'écriture des `segment_tests`.
    """
    from datetime import date as _date
    from sqlalchemy import func

    from optimizer.config import derive_economics, load_config
    from optimizer.diagnostics_entity import detect_phase
    from optimizer.pipeline import load_account, load_ads
    from optimizer.diagnostics_account import campaign_targeting_union, pool_all_creatives
    from optimizer.segment_decision import aggregate_account_segments
    from optimizer.segment_analysis import pinner_coverage
    from optimizer.validator import GateContext, validate_batch
    from models import SegmentDecision, SegmentPerformance, SegmentTest

    today = today or _date.today()
    config = config or load_config()

    campaigns, ad_groups = load_account(db)
    history = {
        "web_click_checkout_value": sum(c["agg"]["web_click_checkout_value"] for c in campaigns),
        "web_click_checkout": sum(c["agg"]["web_click_checkout"] for c in campaigns),
    }
    try:
        economics = derive_economics(config.margin_contrib, history)
    except Exception as e:
        return {"ok": False, "decision_ready": False, "reason": str(e)}

    # --- segments + décisions (dernière fenêtre) ---
    latest = db.query(func.max(SegmentDecision.period_end)).scalar()
    if latest is None:
        return {"ok": False, "reason": "Aucune décision de segment : lancer /segments/build d'abord."}
    perf = db.query(SegmentPerformance).filter(SegmentPerformance.period_end == latest).all()
    period_start = min((p.period_start for p in perf), default=latest)
    period_days = max((latest - period_start).days, 1)
    perf_rows = [{"targeting_type": p.targeting_type, "targeting_value": p.targeting_value,
                  "ad_group_id": p.ad_group_id, "source_status": p.source_status,
                  "spend": float(p.spend or 0), "n_conv": int(p.n_conv or 0),
                  "revenue": float(p.revenue or 0), "impressions": int(p.impressions or 0),
                  "clicks": int(p.clicks or 0)} for p in perf]
    segments = aggregate_account_segments(perf_rows)
    use_pinner_flag = pinner_coverage(perf_rows) >= CONSTANTS.PINNER_MIN_COVERAGE

    decisions = [{
        "targeting_type": d.targeting_type, "targeting_value": d.targeting_value,
        "decision": d.decision, "roas_web_click": d.roas_web_click,
        "cpa_web_click": d.cpa_web_click, "spend": float(d.spend or 0),
        "n_conv": int(d.n_conv or 0), "conv_week": d.conv_week,
        "account_baseline_roas": d.account_baseline_roas,
    } for d in db.query(SegmentDecision).filter(SegmentDecision.period_end == latest).all()]

    # --- contexte compte ---
    days = max((c["agg"]["n_days"] for c in campaigns), default=0)
    weeks = max(days / 7.0, 1.0)
    conv_week_total = history["web_click_checkout"] / weeks
    pool = pool_all_creatives(load_ads(db))
    n_elite = max(1, min(CONSTANTS.CREATIVES_PER_COLDSTART,
                         sum(1 for c in pool if (c.get("web_click_checkout") or 0) >= CONSTANTS.MIN_CONV_TRUST)))

    # specs des campagnes actives (union des ad groups) pour GATE-CANNIBAL
    ag_specs_by_camp: dict[str, list] = {}
    for g in ad_groups:
        ag_specs_by_camp.setdefault(str(g["campaign_id"]), []).append(g.get("targeting_spec"))
    active_specs = [campaign_targeting_union(ag_specs_by_camp.get(str(c["id"]), []))
                    for c in campaigns if (c["status"] or "").upper() == "ACTIVE"]

    # --- candidats ---
    cands = segment_candidates(decisions, economics, conv_week_total=conv_week_total,
                               n_elite_creatives=n_elite, period_days=period_days, today=today)

    for c in campaigns:
        if (c["status"] or "").upper() != "ACTIVE":
            continue
        agg = c["agg"]
        em = EntityMetrics(level="campaign", entity_id=c["id"], spend=agg["spend"],
                           web_click_checkout=agg["web_click_checkout"],
                           web_click_checkout_value=agg["web_click_checkout_value"],
                           n_days=agg["n_days"])
        phase = detect_phase(em, economics, c["daily_budget"], economics.break_even_cpa)
        core = {"id": c["id"], "name": c["name"], "phase": phase, "daily_budget": c["daily_budget"],
                "roas_web_click": em.roas_web_click, "cpa_web_click": em.cpa_web_click,
                "n_conv": em.web_click_checkout, "impression_frequency": None, "prev_cpa": None}
        kw = propose_keyword_campaign(core, {"conv_week_total": conv_week_total}, segments,
                                      economics, n_elite_creatives=n_elite, use_pinner=use_pinner_flag,
                                      period_days=period_days, today=today)
        if kw:
            cands.append(kw)

    tests = propose_segment_tests(segments, decisions, economics, use_pinner=use_pinner_flag,
                                  period_days=period_days, n_elite_creatives=n_elite, today=today)
    test_rec_by_value = {r["targeting_value"]: r for _, r in tests}
    cands.extend(c for c, _ in tests)

    # --- GATE-CANNIBAL sur propositions budgétées (§6/§9) ---
    blocked: list[dict] = []
    surviving: list[Candidate] = []
    for c in cands:
        if c.type in (ActionType.CREATE_CAMPAIGN, ActionType.SPLIT_SEGMENT):
            score = cannibal_conflict(_proposed_spec(c), active_specs, CONSTANTS.CANNIBAL_THRESHOLD)
            if score is not None:
                blocked.append({"candidate": c.to_dict(),
                                "reason": f"GATE-CANNIBAL : chevauchement {score} ≥ "
                                          f"{CONSTANTS.CANNIBAL_THRESHOLD} avec une campagne active."})
                continue
        surviving.append(c)

    # --- validation standard ---
    known = {str(c["id"]) for c in campaigns} | {str(g["id"]) for g in ad_groups}
    from optimizer.audit import _conv_tag_id
    tracking_ok = (history["web_click_checkout"] > 0
                   and any(_conv_tag_id(g) for g in ad_groups))
    gate_ctx = GateContext(break_even_cpa=economics.break_even_cpa, tracking_ok=tracking_ok)
    batch = validate_batch(surviving, known_entity_ids=known, gate_ctx=gate_ctx)

    # --- persiste les tests acceptés (idempotent) ---
    n_tests = 0
    for c in batch.accepted:
        if (c.params or {}).get("lever") != "TEST":
            continue
        val = (c.params.get("targeting") or {}).get("INTEREST", [None])[0]
        rec = test_rec_by_value.get(val)
        if not rec:
            continue
        exists = (db.query(SegmentTest)
                  .filter(SegmentTest.targeting_value == rec["targeting_value"],
                          SegmentTest.status == "PROPOSED").first())
        if not exists:
            db.add(SegmentTest(targeting_type=rec["targeting_type"], targeting_value=rec["targeting_value"],
                               source=rec["source"], status="PROPOSED", rationale=rec["rationale"]))
            n_tests += 1
    db.commit()

    by_type: dict[str, int] = {}
    for c in batch.accepted:
        by_type[c.type] = by_type.get(c.type, 0) + 1

    return {
        "ok": True, "period_end": latest.isoformat(), "economics": economics.__dict__,
        "use_pinner": use_pinner_flag, "conv_week_total": round(conv_week_total, 1),
        "antifragmentation_cap": antifragmentation_cap(conv_week_total),
        "counts_by_type": by_type, "n_tests_persisted": n_tests,
        "candidates": [c.to_dict() for c in batch.accepted],
        "blocked_cannibal": blocked,
        "rejected": [{"candidate": r.candidate.to_dict(), "errors": r.errors} for r in batch.rejected],
    }
