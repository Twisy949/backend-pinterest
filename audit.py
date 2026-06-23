"""
Couche AUDIT Santé de compte (import de la méthodologie `claude-ads`, adaptée).

Catalogue de checks Pinterest **calculés en code depuis la DB** (jamais par le LLM,
INV-2/INV-4). Chaque check renvoie PASS | WARNING | FAIL | N/A, porte une gravité
et une catégorie. Score 0-100 pondéré (catégorie × gravité) + Quick Wins.

Bidirectionnel : chaque ID du catalogue a une implémentation enregistrée dans
`CHECKS` ; un test (eval harness) vérifie l'absence d'ID orphelin. 100 % déterministe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# --- barème (méthodologie claude-ads, calibré Pinterest e-commerce) ----------

SEVERITY_MULT = {"Critical": 5.0, "High": 3.0, "Medium": 1.5, "Low": 0.5}
CATEGORY_WEIGHT = {                       # somme = 1.0
    "tracking": 0.30, "structure": 0.25, "creative": 0.25,
    "audience": 0.12, "bid_budget": 0.08,
}
STATUS_SCORE = {"PASS": 1.0, "WARNING": 0.5, "FAIL": 0.0}   # N/A exclu du dénominateur

PASS, WARNING, FAIL, NA = "PASS", "WARNING", "FAIL", "N/A"


# --- contexte d'audit (assemblé depuis la DB ou des fixtures) ----------------

@dataclass
class AuditInput:
    campaigns: list[dict] = field(default_factory=list)      # {id,name,status,objective,daily_budget,agg}
    ad_groups: list[dict] = field(default_factory=list)      # {id,campaign_id,status,daily_budget,bid_strategy,
                                                             #  learning_mode,optimization_goal,targeting_spec,agg}
    ads: list[dict] = field(default_factory=list)            # {id,ad_group_id,pin_id,agg}
    clusters: list[dict] = field(default_factory=list)       # build_clusters(...)
    break_even_cpa: Optional[float] = None
    target_cpa: Optional[float] = None
    attribution_window: str = "7d_click"
    config_attribution_window: str = "7d_click"
    creative_attributes_count: int = 0
    total_budget_target: Optional[float] = None
    min_ads_per_adgroup: int = 10
    view_through_max: float = 1.0          # web_view/web_click toléré
    hook_floor: float = 0.20               # video_3sec/impr plancher
    diversity_min_concepts: int = 3
    learning_conv_week: int = 50

    # --- agrégats dérivés (web-click, INV-2) ---
    def _sum(self, rows, key):
        return sum(float((r.get("agg", {}) or {}).get(key, 0) or 0) for r in rows)

    def account_days(self) -> int:
        days = [int((c.get("agg", {}) or {}).get("n_days", 0) or 0) for c in self.campaigns]
        return max(days) if days else 0

    def conv_week_total(self) -> float:
        wcc = self._sum(self.campaigns, "web_click_checkout")
        weeks = max(self.account_days() / 7.0, 1.0)
        return wcc / weeks

    def paused_share(self) -> float:
        if not self.campaigns:
            return 0.0
        paused = sum(1 for c in self.campaigns if (c.get("status") or "").upper() != "ACTIVE")
        return paused / len(self.campaigns)

    def ads_per_adgroup(self) -> dict:
        counts: dict[str, int] = {}
        for a in self.ads:
            counts[a.get("ad_group_id")] = counts.get(a.get("ad_group_id"), 0) + 1
        return counts


# --- registre de checks (couverture bidirectionnelle) ------------------------

@dataclass
class CheckSpec:
    id: str
    severity: str
    category: str
    title: str
    fn: Callable[[AuditInput], tuple]


CHECKS: dict[str, CheckSpec] = {}


def check(cid: str, severity: str, category: str, title: str):
    def deco(fn):
        CHECKS[cid] = CheckSpec(cid, severity, category, title, fn)
        return fn
    return deco


@dataclass
class CheckResult:
    id: str
    category: str
    severity: str
    title: str
    status: str
    detail: str = ""
    est_minutes: Optional[int] = None
    impact: float = 0.0       # 0-1, pour trier les Quick Wins


def _r(status, detail="", est_minutes=None, impact=0.0):
    return (status, detail, est_minutes, impact)


# ============================ TRACKING / CAPI (30%) =========================

@check("P-TR1", "Critical", "tracking", "Tag de conversion présent & actif")
def p_tr1(ctx: AuditInput):
    has_tag = any(_conv_tag_id(g) for g in ctx.ad_groups)
    wcc = ctx._sum(ctx.campaigns, "web_click_checkout")
    if has_tag and wcc > 0:
        return _r(PASS, "Tag de conversion détecté et des achats web-click sont remontés.")
    if not has_tag:
        return _r(FAIL, "Aucun tag de conversion détecté sur les ad groups.", 15, 1.0)
    return _r(FAIL, "Tag présent mais aucun achat web-click remonté.", 30, 0.9)


@check("P-TR2", "High", "tracking", "Événement d'optimisation = CHECKOUT")
def p_tr2(ctx: AuditInput):
    goals = [(_conv_event(g)) for g in ctx.ad_groups if _conv_tag_id(g)]
    goals = [g for g in goals if g]
    if not goals:
        return _r(NA, "Aucun objectif de conversion configuré.")
    checkout = sum(1 for g in goals if g.upper() == "CHECKOUT")
    if checkout == len(goals):
        return _r(PASS, "Tous les ad groups optimisent sur CHECKOUT.")
    if checkout == 0:
        return _r(FAIL, "Aucun ad group n'optimise sur CHECKOUT.", 15, 0.7)
    return _r(WARNING, f"{checkout}/{len(goals)} ad groups optimisent sur CHECKOUT.", 15, 0.5)


@check("P-TR3", "High", "tracking", "Le view-through ne domine pas l'attribution")
def p_tr3(ctx: AuditInput):
    click = ctx._sum(ctx.campaigns, "web_click_checkout")
    view = ctx._sum(ctx.campaigns, "web_view_checkout")
    if click <= 0:
        return _r(NA, "Pas d'achats web-click.")
    ratio = view / click
    if ratio <= ctx.view_through_max:
        return _r(PASS, f"Ratio view/click = {ratio:.2f} (≤ {ctx.view_through_max}).")
    return _r(WARNING, f"View-through élevé (ratio {ratio:.2f}) — risque d'inflation d'attribution.", 20, 0.4)


@check("P-TR4", "Medium", "tracking", "Fenêtre d'attribution cohérente")
def p_tr4(ctx: AuditInput):
    if ctx.attribution_window == ctx.config_attribution_window:
        return _r(PASS, f"Fenêtre cohérente ({ctx.attribution_window}).")
    return _r(WARNING, "Fenêtre d'attribution incohérente avec la config des calculs.", 10, 0.3)


# ============================ STRUCTURE (25%) ==============================

@check("P-ST1", "Critical", "structure", "Sur-fragmentation du compte")
def p_st1(ctx: AuditInput):
    n = len(ctx.campaigns)
    expected = max(1.0, ctx.conv_week_total() / 50.0)
    if n <= expected * 1.5:
        return _r(PASS, f"{n} campagnes pour ~{expected:.0f} attendue(s).")
    return _r(FAIL, f"{n} campagnes pour ~{expected:.0f} justifiée(s) par le volume "
                    f"({ctx.conv_week_total():.0f} conv/sem) — consolider.", None, 1.0)


@check("P-ST2", "High", "structure", "Ad groups sous le plancher 5×CPA")
def p_st2(ctx: AuditInput):
    if ctx.target_cpa is None:
        return _r(NA, "MARGIN_CONTRIB non configurée — plancher 5×CPA indéterminé.")
    floor = 5 * ctx.target_cpa
    under = [g for g in ctx.ad_groups if float(g.get("daily_budget") or 0) < floor]
    if not ctx.ad_groups:
        return _r(NA, "Aucun ad group.")
    share = len(under) / len(ctx.ad_groups)
    if share == 0:
        return _r(PASS, "Tous les ad groups au-dessus du plancher 5×CPA.")
    status = FAIL if share > 0.5 else WARNING
    return _r(status, f"{len(under)}/{len(ctx.ad_groups)} ad groups sous {floor:.0f} € (5×CPA).", None, 0.5)


@check("P-ST3", "High", "structure", "Ad groups sous 50 conversions/semaine")
def p_st3(ctx: AuditInput):
    if not ctx.ad_groups:
        return _r(NA, "Aucun ad group.")
    weeks = max(ctx.account_days() / 7.0, 1.0)
    under = 0
    for g in ctx.ad_groups:
        conv_week = float((g.get("agg", {}) or {}).get("web_click_checkout", 0) or 0) / weeks
        if conv_week < ctx.learning_conv_week:
            under += 1
    share = under / len(ctx.ad_groups)
    status = FAIL if share > 0.5 else (WARNING if share > 0 else PASS)
    return _r(status, f"{under}/{len(ctx.ad_groups)} ad groups sous 50 conv/sem (learning bloqué).",
              None, 0.6)


@check("P-ST4", "Medium", "structure", "Édition pendant l'apprentissage")
def p_st4(ctx: AuditInput):
    learning = [g for g in ctx.ad_groups if (g.get("learning_mode") or "").upper() == "ACTIVE"]
    if not learning:
        return _r(NA, "Aucun ad group en apprentissage actif.")
    return _r(WARNING, f"{len(learning)} ad group(s) en apprentissage — éviter les éditions.", 5, 0.3)


@check("P-ST5", "High", "structure", "Compte à l'arrêt")
def p_st5(ctx: AuditInput):
    share = ctx.paused_share()
    if share >= 1.0:
        return _r(FAIL, "100 % des campagnes en pause — bascule en cold-start.", None, 0.9)
    if share >= 0.7:
        return _r(WARNING, f"{share*100:.0f} % des campagnes en pause.", None, 0.5)
    return _r(PASS, f"{(1-share)*100:.0f} % des campagnes actives.")


@check("P-ST6", "Medium", "structure", "Densité créative par ad group")
def p_st6(ctx: AuditInput):
    if not ctx.ad_groups:
        return _r(NA, "Aucun ad group.")
    counts = ctx.ads_per_adgroup()
    thin = [g for g in ctx.ad_groups if counts.get(g.get("id"), 0) < ctx.min_ads_per_adgroup]
    share = len(thin) / len(ctx.ad_groups)
    status = FAIL if share > 0.5 else (WARNING if share > 0 else PASS)
    return _r(status, f"{len(thin)}/{len(ctx.ad_groups)} ad groups sous {ctx.min_ads_per_adgroup} ads.",
              None, 0.3)


# ============================ CRÉATIF (25%) ================================

@check("P-CR1", "High", "creative", "Diversité créative par audience")
def p_cr1(ctx: AuditInput):
    if ctx.creative_attributes_count <= 0:
        return _r(NA, "creative_attributes non peuplée (couche Découverte M0 requise).")
    # heuristique : concepts distincts ~ attributs labellisés / audiences
    audiences = max(len(ctx.clusters), 1)
    per_aud = ctx.creative_attributes_count / audiences
    if per_aud >= ctx.diversity_min_concepts:
        return _r(PASS, f"~{per_aud:.0f} concepts distincts par audience.")
    return _r(WARNING, f"~{per_aud:.0f} concepts par audience (< {ctx.diversity_min_concepts}).", None, 0.5)


@check("P-CR2", "High", "creative", "Cadence de refresh créatif dépassée")
def p_cr2(ctx: AuditInput):
    active = [c for c in ctx.campaigns if (c.get("status") or "").upper() == "ACTIVE"]
    if not active:
        return _r(NA, "Aucune campagne active — fatigue non évaluable en direct.")
    return _r(PASS, "Fatigue évaluée par le module dédié sur les entités actives.")


@check("P-CR3", "Medium", "creative", "Hook vidéo faible")
def p_cr3(ctx: AuditInput):
    imp = ctx._sum(ctx.ads, "impressions")
    v3 = ctx._sum(ctx.ads, "video_3sec_views")
    if imp <= 0 or v3 <= 0:
        return _r(NA, "Pas de données vidéo.")
    hook = v3 / imp
    if hook >= ctx.hook_floor:
        return _r(PASS, f"Hook vidéo = {hook:.2f} (≥ {ctx.hook_floor}).")
    return _r(WARNING, f"Hook vidéo faible ({hook:.2f} < {ctx.hook_floor}).", None, 0.4)


@check("P-CR4", "Medium", "creative", "Décrochage de rétention vidéo")
def p_cr4(ctx: AuditInput):
    v3 = ctx._sum(ctx.ads, "video_3sec_views")
    v100 = ctx._sum(ctx.ads, "video_p100_complete")
    if v3 <= 0:
        return _r(NA, "Pas de données vidéo.")
    retention = v100 / v3
    if retention >= 0.10:
        return _r(PASS, f"Rétention 100%/3s = {retention:.2f}.")
    return _r(WARNING, f"Rétention faible ({retention:.2f}) — message milieu de vidéo à revoir.",
              None, 0.3)


@check("P-CR5", "High", "creative", "Concentration créative (point unique de fatigue)")
def p_cr5(ctx: AuditInput):
    spends = {}
    for a in ctx.ads:
        pin = a.get("pin_id")
        if pin is None:
            continue
        spends[pin] = spends.get(pin, 0.0) + float((a.get("agg", {}) or {}).get("spend", 0) or 0)
    total = sum(spends.values())
    if total <= 0 or len(spends) < 2:
        return _r(NA, "Données de dépense créative insuffisantes.")
    top2 = sum(sorted(spends.values(), reverse=True)[:2])
    share = top2 / total
    if share <= 0.6:
        return _r(PASS, f"Top 2 créas = {share*100:.0f} % de la dépense.")
    return _r(WARNING, f"Dépense concentrée : top 2 créas = {share*100:.0f} %.", None, 0.4)


# ============================ AUDIENCE (12%) ===============================

@check("P-AU1", "High", "audience", "Chevauchement / cannibalisme d'audiences")
def p_au1(ctx: AuditInput):
    overlapping = [c for c in ctx.clusters if c.get("size", 1) > 1 and (c.get("max_similarity") or 0) >= 0.5]
    if not ctx.clusters:
        return _r(NA, "Clusters non calculés.")
    if not overlapping:
        return _r(PASS, "Pas de chevauchement d'audiences notable.")
    biggest = max(overlapping, key=lambda c: c["size"])
    return _r(WARNING, f"{len(overlapping)} cluster(s) chevauchant(s) ; le plus large = "
                       f"{biggest['size']} entités.", None, 0.5)


@check("P-AU2", "Medium", "audience", "Aucun test d'élargissement d'audience")
def p_au2(ctx: AuditInput):
    if not ctx.ad_groups:
        return _r(NA, "Aucun ad group.")
    strategies = set()
    for g in ctx.ad_groups:
        for s in ((g.get("targeting_spec") or {}).get("TARGETING_STRATEGY") or []):
            strategies.add(str(s).upper())
    if strategies and strategies <= {"CHOOSE_YOUR_OWN"}:
        return _r(WARNING, "Tout est en ciblage manuel (CHOOSE_YOUR_OWN) — aucun test broad/expanded.",
                  None, 0.3)
    return _r(PASS, "Au moins une stratégie d'élargissement présente.")


@check("P-AU3", "Low", "audience", "Fragmentation d'audiences quasi-identiques")
def p_au3(ctx: AuditInput):
    if not ctx.clusters:
        return _r(NA, "Clusters non calculés.")
    near_dupes = sum(1 for c in ctx.clusters if c.get("size", 1) > 1)
    if near_dupes == 0:
        return _r(PASS, "Pas d'audiences quasi-identiques fragmentées.")
    return _r(WARNING, f"{near_dupes} groupe(s) d'audiences quasi-identiques fragmentées.", None, 0.2)


# ============================ ENCHÈRE & BUDGET (8%) =========================

@check("P-BB1", "High", "bid_budget", "Règle 3× Kill")
def p_bb1(ctx: AuditInput):
    if ctx.break_even_cpa is None:
        return _r(NA, "MARGIN_CONTRIB non configurée — seuil 3× Kill indéterminé.")
    kill = 3 * ctx.break_even_cpa
    bleeders = []
    for c in ctx.campaigns:
        if (c.get("status") or "").upper() != "ACTIVE":
            continue
        cpa = (c.get("agg", {}) or {}).get("cpa_web_click")
        if cpa is not None and cpa > kill:
            bleeders.append(c)
    if not any((c.get("status") or "").upper() == "ACTIVE" for c in ctx.campaigns):
        return _r(NA, "Aucune campagne active.")
    if not bleeders:
        return _r(PASS, f"Aucune entité active au-dessus de 3×break-even ({kill:.0f} €).")
    return _r(FAIL, f"{len(bleeders)} entité(s) active(s) à CPA > {kill:.0f} € — pauser.", 10, 0.9)


@check("P-BB2", "Medium", "bid_budget", "Suffisance budgétaire")
def p_bb2(ctx: AuditInput):
    if ctx.target_cpa is None:
        return _r(NA, "MARGIN_CONTRIB non configurée.")
    need = 5 * ctx.target_cpa
    budget = ctx.total_budget_target
    if budget is None:
        return _r(NA, "TOTAL_BUDGET_TARGET non configuré.")
    if budget >= need:
        return _r(PASS, f"Budget {budget:.0f} € couvre 1 ad group à 5×CPA ({need:.0f} €).")
    return _r(WARNING, f"Budget {budget:.0f} € < plancher 5×CPA ({need:.0f} €).", None, 0.4)


@check("P-BB3", "Low", "bid_budget", "Enchère manuelle trop serrée")
def p_bb3(ctx: AuditInput):
    manual = [g for g in ctx.ad_groups if (g.get("bid_strategy") or "").upper() not in
              ("AUTOMATIC_BID", "", "AUTOMATIC")]
    if not manual:
        return _r(PASS, "Enchère automatique partout.")
    return _r(WARNING, f"{len(manual)} ad group(s) en enchère manuelle — vérifier l'étranglement.",
              None, 0.2)


# --- exécution + scoring -----------------------------------------------------

def run_checks(ctx: AuditInput) -> list[CheckResult]:
    results = []
    for cid, spec in CHECKS.items():
        status, detail, est, impact = spec.fn(ctx)
        results.append(CheckResult(cid, spec.category, spec.severity, spec.title,
                                   status, detail, est, impact))
    return results


def compute_score(results: list[CheckResult]) -> dict:
    num = den = 0.0
    by_cat: dict[str, dict] = {}
    for r in results:
        if r.status == NA:
            continue
        w = SEVERITY_MULT[r.severity] * CATEGORY_WEIGHT[r.category]
        num += STATUS_SCORE[r.status] * w
        den += w
        cat = by_cat.setdefault(r.category, {"num": 0.0, "den": 0.0})
        cat["num"] += STATUS_SCORE[r.status] * w
        cat["den"] += w
    score = round(num / den * 100, 1) if den > 0 else None
    return {
        "score": score,
        "grade": _grade(score),
        "by_category": {k: round(v["num"] / v["den"] * 100, 1) if v["den"] else None
                        for k, v in by_cat.items()},
    }


def _grade(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    if score >= 90: return "A"
    if score >= 75: return "B"
    if score >= 60: return "C"
    if score >= 40: return "D"
    return "F"


def quick_wins(results: list[CheckResult]) -> list[CheckResult]:
    qw = [r for r in results
          if r.status in (FAIL, WARNING) and r.severity in ("Critical", "High")
          and r.est_minutes is not None and r.est_minutes < 15]
    sev_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    return sorted(qw, key=lambda r: (sev_rank[r.severity], -r.impact))


def run_audit(ctx: AuditInput) -> dict:
    """Audit complet déterministe : score, grade, findings, quick wins."""
    results = run_checks(ctx)

    # Cold-start : si le compte n'a AUCUNE campagne active, la santé ne concerne plus
    # que ce constat. Les autres findings portent sur des entités en pause (utiles aux
    # calculs internes de l'IA, mais hors périmètre « santé du compte »). On n'expose
    # donc que le déclencheur « Compte à l'arrêt » (P-ST5).
    cold_start = bool(ctx.campaigns) and not any(
        (c.get("status") or "").upper() == "ACTIVE" for c in ctx.campaigns)
    if cold_start:
        results = [r for r in results if r.id == "P-ST5"]

    score = compute_score(results)
    order = {FAIL: 0, WARNING: 1, PASS: 2, NA: 3}
    sev_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    findings = sorted(results, key=lambda r: (order[r.status], sev_rank[r.severity], r.id))
    return {
        **score,
        "cold_start": cold_start,
        "checks": [vars(r) for r in findings],
        "quick_wins": [vars(r) for r in quick_wins(results)],
        "summary": {"fail": sum(1 for r in results if r.status == FAIL),
                    "warning": sum(1 for r in results if r.status == WARNING),
                    "pass": sum(1 for r in results if r.status == PASS),
                    "na": sum(1 for r in results if r.status == NA)},
    }


# --- helpers d'extraction du goal de conversion ------------------------------

def _goal_meta(g: dict) -> dict:
    opt = g.get("optimization_goal")
    if isinstance(opt, str):                      # colonne String : JSON sérialisé en texte
        import json
        try:
            opt = json.loads(opt)
        except (json.JSONDecodeError, ValueError):
            return {}
    if isinstance(opt, dict):
        meta = opt.get("conversion_tag_v3_goal_metadata")
        if isinstance(meta, dict):
            return meta
    return {}


def _conv_tag_id(g: dict):
    return _goal_meta(g).get("conversion_tag_id")


def _conv_event(g: dict):
    return _goal_meta(g).get("conversion_event")
