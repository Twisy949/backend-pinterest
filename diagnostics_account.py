"""
Phase 6 — Diagnostic COMPTE / anti-cannibalisme (réf. spec §8).

  - targeting_similarity() : Jaccard sur les INTEREST (+ garde GENDER/LOCATION).
  - build_clusters()       : composantes connexes du graphe de similarité.
  - inverse_correlation()  : quand A scale, B perd-elle du volume ? (séries).
  - blended_test()         : le ROAS du COMPTE progresse-t-il, ou juste l'entité ?
"""
from __future__ import annotations

import hashlib
from collections import Counter
from statistics import median
from typing import Iterable, Optional


def _sets(spec: Optional[dict]) -> dict:
    spec = spec or {}
    def s(key):
        v = spec.get(key)
        return {str(x) for x in v} if isinstance(v, list) else set()
    return {"INTEREST": s("INTEREST"), "GENDER": s("GENDER"), "LOCATION": s("LOCATION")}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def targeting_similarity(spec_a: Optional[dict], spec_b: Optional[dict]) -> float:
    """
    Similarité de ciblage ∈ [0,1]. Audiences à GENDER/LOCATION disjoints → 0
    (pas de chevauchement réel). Sinon Jaccard des INTEREST ; à défaut d'intérêts,
    repli sur GENDER+LOCATION.
    """
    A, B = _sets(spec_a), _sets(spec_b)

    # garde géo/genre : si les deux côtés précisent et ne se recoupent pas → 0
    for key in ("LOCATION", "GENDER"):
        if A[key] and B[key] and not (A[key] & B[key]):
            return 0.0

    if A["INTEREST"] or B["INTEREST"]:
        return round(_jaccard(A["INTEREST"], B["INTEREST"]), 4)
    # repli : audiences larges sans intérêts → proximité géo/genre
    return round(_jaccard(A["GENDER"] | A["LOCATION"], B["GENDER"] | B["LOCATION"]), 4)


def _list_set(spec: Optional[dict], key: str) -> set:
    v = (spec or {}).get(key)
    return {str(x) for x in v} if isinstance(v, list) else set()


def _disjoint_geo_gender(spec_a: Optional[dict], spec_b: Optional[dict]) -> bool:
    """Vrai si deux campagnes précisent une géo/un genre et ne se recoupent pas."""
    for key in ("LOCATION", "GENDER", "COUNTRY", "REGION"):
        a, b = _list_set(spec_a, key), _list_set(spec_b, key)
        if a and b and not (a & b):
            return True
    return False


def audience_overlap(spec_a: Optional[dict], spec_b: Optional[dict]) -> float:
    """
    Chevauchement des listes d'audience incluses (SPEC §6). Deux campagnes incluant
    la MÊME liste de retargeting (ou actalikes de même source) se cannibalisent.
    Jaccard sur AUDIENCE_INCLUDE.
    """
    A, B = _list_set(spec_a, "AUDIENCE_INCLUDE"), _list_set(spec_b, "AUDIENCE_INCLUDE")
    if not A and not B:
        return 0.0
    return round(_jaccard(A, B), 4)


def demo_overlap(spec_a: Optional[dict], spec_b: Optional[dict]) -> float:
    """Chevauchement de la cellule démographique (genre/âge/géo) — Jaccard (SPEC §6)."""
    def cell(spec):
        out = set()
        for key in ("GENDER", "AGE_BUCKET", "LOCATION", "COUNTRY", "REGION"):
            out |= {f"{key}:{x}" for x in _list_set(spec, key)}
        return out
    A, B = cell(spec_a), cell(spec_b)
    if not A and not B:
        return 0.0
    return round(_jaccard(A, B), 4)


def overlap_score(spec_a: Optional[dict], spec_b: Optional[dict]) -> float:
    """
    Score de chevauchement inter-campagnes (SPEC §6) = max(intérêts, audience, démo).
    Géo/genre disjoints → 0 (marchés distincts, pas de cannibalisme réel).
    """
    if _disjoint_geo_gender(spec_a, spec_b):
        return 0.0
    return round(max(
        targeting_similarity(spec_a, spec_b),   # intérêts ciblés (Jaccard, garde géo incluse)
        audience_overlap(spec_a, spec_b),        # listes d'audience
        demo_overlap(spec_a, spec_b),            # cellule démo
    ), 4)


def campaign_targeting_union(specs: Iterable[Optional[dict]]) -> dict:
    """Fusionne les targeting_spec des ad groups d'une campagne (union par clé)."""
    out: dict[str, set] = {}
    for s in specs:
        if not isinstance(s, dict):
            continue
        for k, v in s.items():
            if isinstance(v, list):
                out.setdefault(k, set()).update(str(x) for x in v)
    return {k: sorted(v) for k, v in out.items()}


class _UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _cluster_key(members: list[str]) -> str:
    payload = "|".join(sorted(members))
    return "cl_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def build_clusters(
    entities: Iterable[dict],
    threshold: float = 0.3,
    level: str = "ad_group",
    similarity=targeting_similarity,
) -> list[dict]:
    """
    `entities` : itérable de dicts {id, targeting_spec, [similarity meta]}.
    `similarity` : fonction (spec_a, spec_b)→[0,1] ; défaut `targeting_similarity`
    (intérêts), ou `overlap_score` pour le cannibalisme inter-campagnes (§6).
    Renvoie une liste de clusters [{cluster_key, level, members:[id], size,
    max_similarity}]. Les singletons sont inclus (cluster de taille 1).
    """
    ents = [e for e in entities]
    ids = [str(e["id"]) for e in ents]
    spec_by_id = {str(e["id"]): e.get("targeting_spec") for e in ents}
    uf = _UnionFind(ids)
    max_sim: dict[str, float] = {i: 0.0 for i in ids}

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            sim = similarity(spec_by_id[ids[i]], spec_by_id[ids[j]])
            if sim >= threshold:
                uf.union(ids[i], ids[j])
                max_sim[ids[i]] = max(max_sim[ids[i]], sim)
                max_sim[ids[j]] = max(max_sim[ids[j]], sim)

    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(uf.find(i), []).append(i)

    clusters = []
    for members in groups.values():
        clusters.append({
            "cluster_key": _cluster_key(members),
            "level": level,
            "members": sorted(members),
            "size": len(members),
            "max_similarity": round(max(max_sim[m] for m in members), 4),
        })
    return sorted(clusters, key=lambda c: c["size"], reverse=True)


def cluster_rows(clusters: list[dict]) -> list[dict]:
    """Aplati les clusters → lignes prêtes pour la table `clusters`."""
    rows = []
    for c in clusters:
        for m in c["members"]:
            rows.append({
                "cluster_key": c["cluster_key"],
                "member_level": c["level"],
                "member_id": m,
                "similarity": c["max_similarity"],
            })
    return rows


def cluster_of(clusters: list[dict], entity_id: str) -> Optional[str]:
    eid = str(entity_id)
    for c in clusters:
        if eid in c["members"]:
            return c["cluster_key"]
    return None


def audience_label(specs: list[Optional[dict]]) -> str:
    """
    Étiquette d'audience lisible (genre · localisation dominante).

    Le genre n'est « restreint » que si l'audience EXCLUT un genre principal :
    présence simultanée de female ET male (ou aucune restriction) → « tous ».
    Ne PAS prendre le genre le plus fréquent : un ciblage `['female','male','unknown']`
    vise tout le monde et doit s'afficher « tous », pas « femmes ».
    """
    locations: Counter = Counter()
    has_female = has_male = False
    for s in specs:
        s = s or {}
        for g in (s.get("GENDER") or []):
            gv = str(g).lower()
            if gv == "female":
                has_female = True
            elif gv == "male":
                has_male = True
        for loc in (s.get("LOCATION") or []) or (s.get("COUNTRY") or []):
            locations[str(loc)] += 1
    if has_female and not has_male:
        g_fr = "femmes"
    elif has_male and not has_female:
        g_fr = "hommes"
    else:                                  # les deux, ou aucun (que 'unknown'/rien) → tous
        g_fr = "tous"
    loc = locations.most_common(1)[0][0] if locations else "WW"
    return f"{g_fr} · {loc}"


_POOL_SUM = ("spend", "web_click_checkout", "web_click_checkout_value",
            "impressions", "saves", "video_3sec_views", "video_p100_complete")


def pool_winning_creatives_by_cluster(
    ad_groups: Iterable[dict],
    ads: Iterable[dict],
    *,
    threshold: float = 0.3,
    min_conv_trust: int = 3,
    top_n: int = 6,
    cluster_level: str = "ad_group",
) -> list[dict]:
    """
    Patch A — sélectionne les créas gagnantes au niveau AD, mises en commun par
    cluster d'audience (toutes campagnes confondues), dédupliquées par `pin_id`.

    `ad_groups` : {id, campaign_id, targeting_spec}
    `ads`       : {id, ad_group_id, pin_id, name, campaign_id?, agg:{...}}
    Retourne, par cluster : top N créas fiables classées par CPA/ROAS + indicateurs
    avancés (save_rate, hook, rétention), + métadonnées (audience, campagnes fusionnées).
    """
    top_n = max(5, min(8, top_n))
    ad_groups = list(ad_groups)
    ents = [{"id": str(g["id"]), "targeting_spec": g.get("targeting_spec")} for g in ad_groups]
    clusters = build_clusters(ents, threshold=threshold, level=cluster_level)
    ag2cluster = {m: c["cluster_key"] for c in clusters for m in c["members"]}
    cluster_members = {c["cluster_key"]: c["members"] for c in clusters}
    campaign_of_ag = {str(g["id"]): g.get("campaign_id") for g in ad_groups}
    targeting_of_ag = {str(g["id"]): g.get("targeting_spec") for g in ad_groups}

    # pool ads par cluster → dédup par pin_id (métriques sommées)
    pools: dict[str, dict] = {}
    for a in ads:
        ag = str(a.get("ad_group_id"))
        cl = ag2cluster.get(ag)
        pin = a.get("pin_id")
        if cl is None or pin is None:
            continue
        acc = pools.setdefault(cl, {}).setdefault(pin, {
            **{k: 0.0 for k in _POOL_SUM}, "name": None,
            "campaigns": set(), "ad_groups": set(),
        })
        g = a.get("agg", {}) or {}
        for k in _POOL_SUM:
            acc[k] += float(g.get(k, 0) or 0)
        acc["name"] = acc["name"] or a.get("name")
        acc["campaigns"].add(a.get("campaign_id") or campaign_of_ag.get(ag))
        acc["ad_groups"].add(ag)

    results = []
    for cl, pinmap in pools.items():
        creatives = []
        for pin, acc in pinmap.items():
            spend, wcc = acc["spend"], acc["web_click_checkout"]
            if spend <= 0:
                continue
            imp, v3 = acc["impressions"], acc["video_3sec_views"]
            creatives.append({
                "pin_id": pin, "name": acc["name"],
                "cpa_web_click": round(spend / wcc, 2) if wcc > 0 else None,
                "roas_web_click": round(acc["web_click_checkout_value"] / spend, 4),
                "web_click_checkout": int(wcc), "spend": round(spend, 2),
                "save_rate": round(acc["saves"] / imp, 6) if imp > 0 else 0.0,
                "hook": round(v3 / imp, 6) if imp > 0 else 0.0,
                "retention": round(acc["video_p100_complete"] / v3, 6) if v3 > 0 else 0.0,
                "reliable": wcc >= min_conv_trust,
                "n_campaigns": len({c for c in acc["campaigns"] if c}),
                "source_ad_groups": sorted(g for g in acc["ad_groups"] if g),
            })
        reliable = sorted(
            (c for c in creatives if c["reliable"] and c["cpa_web_click"] is not None),
            key=lambda c: (c["cpa_web_click"], -c["roas_web_click"],
                           -c["save_rate"], -c["hook"], -c["retention"]),
        )
        selected = reliable[:top_n]
        all_camps = {c for acc in pinmap.values() for c in acc["campaigns"] if c}
        conc = Counter(ag for c in selected for ag in c["source_ad_groups"])
        dominant_ag, dominant_count = conc.most_common(1)[0] if conc else (None, 0)
        members = cluster_members.get(cl, [])
        results.append({
            "cluster_key": cl,
            "ad_group_members": members,
            "n_test_campaigns": len(all_camps),
            "n_reliable_creatives": len(reliable),
            "creatives": selected,
            "median_cpa": round(median([c["cpa_web_click"] for c in selected]), 2) if selected else None,
            "median_roas": round(median([c["roas_web_click"] for c in selected]), 2) if selected else None,
            "total_conv": int(sum(c["web_click_checkout"] for c in selected)),
            "dominant_ad_group": dominant_ag,
            "dominant_count": dominant_count,
            "dominant_campaign": campaign_of_ag.get(dominant_ag) if dominant_ag else None,
            "audience": audience_label([targeting_of_ag.get(m) for m in members]),
        })
    return sorted(results, key=lambda r: len(r["creatives"]), reverse=True)


_CREA_SUM = ("spend", "web_click_checkout", "web_click_checkout_value",
            "impressions", "saves", "video_3sec_views", "video_p75", "video_p100_complete")


def pool_all_creatives(ads: Iterable[dict]) -> list[dict]:
    """
    Patch #3 Fix 3 — pool de TOUTES les créas du compte, dédupliquées par pin_id,
    métriques agrégées sur tout l'historique (actives ET en pause).
    """
    acc: dict = {}
    for a in ads:
        pin = a.get("pin_id")
        if pin is None:
            continue
        c = acc.setdefault(pin, {**{k: 0.0 for k in _CREA_SUM}, "name": None,
                                 "campaigns": set(), "ad_groups": set()})
        g = a.get("agg", {}) or {}
        for k in _CREA_SUM:
            c[k] += float(g.get(k, 0) or 0)
        c["name"] = c["name"] or a.get("name")
        if a.get("campaign_id"):
            c["campaigns"].add(a["campaign_id"])
        if a.get("ad_group_id"):
            c["ad_groups"].add(a["ad_group_id"])

    out = []
    for pin, c in acc.items():
        spend, wcc, imp, v3 = c["spend"], c["web_click_checkout"], c["impressions"], c["video_3sec_views"]
        if spend <= 0:
            continue
        out.append({
            "pin_id": pin, "name": c["name"],
            "spend": round(spend, 2), "web_click_checkout": int(wcc),
            "roas_web_click": round(c["web_click_checkout_value"] / spend, 4),
            "cpa_web_click": round(spend / wcc, 2) if wcc > 0 else None,
            "save_rate": round(c["saves"] / imp, 6) if imp > 0 else 0.0,
            "hook": round(v3 / imp, 6) if imp > 0 else 0.0,
            "retention": round(c["video_p100_complete"] / v3, 6) if v3 > 0 else 0.0,
            "video_p75": int(c["video_p75"]),
            "n_campaigns": len(c["campaigns"]),
            "source_campaigns": sorted(str(x) for x in c["campaigns"]),
            "source_ad_groups": sorted(str(x) for x in c["ad_groups"]),
        })
    return out


def creative_benchmarks(pool: list[dict]) -> dict:
    """Plafonds de normalisation = percentiles calibrés sur les créas du compte."""
    from optimizer.calibration import percentile

    def p(key, q):
        vals = [c[key] for c in pool if c.get(key) is not None]
        return percentile(vals, q)
    return {
        "p95_roas": p("roas_web_click", 95) or 1.0,
        "p75_conv": p("web_click_checkout", 75) or 1.0,
        "p75_save_rate": p("save_rate", 75) or 1.0,
        "p75_hook_rate": p("hook", 75) or 1.0,
        "p75_video_p75": p("video_p75", 75) or 1.0,
        # CPA (bas = mieux) : best = P10, worst = P90
        "good_cpa": p("cpa_web_click", 10) or 1.0,
        "ceiling_cpa": p("cpa_web_click", 90) or 1.0,
    }


def inverse_correlation(
    daily_a: list[dict], daily_b: list[dict],
    key_a: str = "spend", key_b: str = "web_click_checkout",
) -> Optional[float]:
    """
    Corrélation (Pearson) entre `key_a` de A et `key_b` de B sur les dates
    communes. Négative ⇒ signal de cannibalisme (A monte, B baisse). None si
    moins de 3 points communs.
    """
    a_by_date = {(r.get("date") or "")[:10]: float(r.get(key_a) or 0) for r in daily_a}
    b_by_date = {(r.get("date") or "")[:10]: float(r.get(key_b) or 0) for r in daily_b}
    common = sorted(set(a_by_date) & set(b_by_date))
    if len(common) < 3:
        return None
    xs = [a_by_date[d] for d in common]
    ys = [b_by_date[d] for d in common]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return round(cov / (vx ** 0.5 * vy ** 0.5), 4)


def blended_test(
    account_before: float, account_after: float,
    entity_before: float, entity_after: float,
    eps: float = 1e-9,
) -> dict:
    """
    L'amélioration est-elle réelle au niveau COMPTE, ou seulement de l'entité
    (cannibalisme) ? Tous les ROAS sont web-click (INV-2).
    """
    entity_improved = entity_after > entity_before + eps
    account_improved = account_after > account_before + eps
    return {
        "entity_improved": entity_improved,
        "account_improved": account_improved,
        "cannibalized": entity_improved and not account_improved,
        "account_delta": round(account_after - account_before, 4),
        "entity_delta": round(entity_after - entity_before, 4),
    }
