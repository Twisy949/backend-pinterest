"""
Phase 10 — Couche LLM bornée (réf. spec §11.4). Réutilise la boucle agent
existante (`ai/claude_client.py`, table `agent_logs`).

Contrat strict : le LLM peut PRIORISER, détailler le « quoi faire exactement »,
rédiger les briefs créatifs, écrire `confidence_basis`. Il ne peut PAS : calculer
une métrique, poser `confidence` (INV-4), inventer une entité, modifier les
`params`/`hypothesis` chiffrés. `apply_llm_output()` fait respecter tout cela.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from optimizer.schemas import Candidate

HARD_RULES = [
    "ROAS = web_click_checkout_value / spend (jamais blended ni view-through).",
    "Aucune action pendant l'apprentissage (in_learning).",
    "Hausse de budget ≤ 25 % par palier ; cooldown ≥ patience.",
    "L'objectif est lu sur l'entité (WEB_CONVERSION ici), jamais hardcodé SALES.",
    "Tu ne calcules aucune métrique et ne poses jamais `confidence` (dérivée).",
    "Tu ne proposes que des entités présentes dans la liste de candidats.",
]

OUTPUT_CONTRACT = (
    "Réponds UNIQUEMENT en JSON : "
    '{"decisions":[{"ref": "<ref candidat>", "priority": <int>, '
    '"detail": "<quoi faire exactement>", "creative_brief": "<optionnel>", '
    '"confidence_basis": "<justification qualitative>"}]}. '
    "N'invente pas de `ref`. N'inclus ni `confidence`, ni `params`, ni `hypothesis`."
)


def candidate_ref(c: Candidate) -> str:
    return f"{c.type}:{c.entity_level}:{c.entity_id}"


def build_llm_payload(candidates: list[Candidate], memory: Optional[dict] = None) -> dict:
    """Contexte chiffré + mémoire pertinente + règles dures pour le LLM."""
    return {
        "hard_rules": HARD_RULES,
        "output_contract": OUTPUT_CONTRACT,
        "candidates": [
            {**c.to_dict(), "ref": candidate_ref(c)} for c in candidates
        ],
        "memory": memory or {},
    }


@dataclass
class ApplyResult:
    candidates: list[Candidate]
    rejected_refs: list[str] = field(default_factory=list)
    stripped: list[str] = field(default_factory=list)   # champs interdits retirés


# champs que le LLM N'A PAS le droit de fixer
_FORBIDDEN = ("confidence", "params", "hypothesis", "review_date_t1", "review_date_t2")


def apply_llm_output(candidates: list[Candidate], llm_output) -> ApplyResult:
    """
    Fusionne la sortie LLM dans les candidats déterministes.

    - applique : priority (ordre), detail (→ rationale), creative_brief,
      confidence_basis ;
    - INV-4 : ignore tout `confidence` LLM — la valeur dérivée est conservée ;
    - rejette toute `ref` absente des candidats (entité inventée) ;
    - retire silencieusement les champs chiffrés interdits.
    """
    if isinstance(llm_output, str):
        try:
            llm_output = json.loads(llm_output)
        except (json.JSONDecodeError, ValueError):
            return ApplyResult(candidates=candidates, rejected_refs=["<json invalide>"])

    decisions = (llm_output or {}).get("decisions", []) if isinstance(llm_output, dict) else []
    by_ref = {candidate_ref(c): c for c in candidates}
    rejected: list[str] = []
    stripped: list[str] = []
    priority: dict[str, int] = {}

    for d in decisions:
        if not isinstance(d, dict):
            continue
        ref = d.get("ref")
        c = by_ref.get(ref)
        if c is None:
            rejected.append(str(ref))               # entité/candidat inventé → rejet
            continue
        for f in _FORBIDDEN:
            if f in d:
                stripped.append(f"{ref}.{f}")        # INV-4 & params figés
        if d.get("detail"):
            c.rationale = f"{c.rationale}\n→ {d['detail']}".strip()
        if d.get("creative_brief"):
            c.params = {**(c.params or {}), "creative_brief": d["creative_brief"]}
        if d.get("confidence_basis"):
            # on AJOUTE la justification qualitative, sans toucher au nombre dérivé
            c.confidence_basis = f"{c.confidence_basis} | LLM: {d['confidence_basis']}".strip(" |")
        if isinstance(d.get("priority"), int):
            priority[ref] = d["priority"]

    ordered = sorted(candidates, key=lambda c: priority.get(candidate_ref(c), 1_000_000))
    return ApplyResult(candidates=ordered, rejected_refs=rejected, stripped=stripped)


def prioritize_with_llm(
    db, candidates: list[Candidate], memory: Optional[dict] = None, *, client=None,
) -> ApplyResult:
    """
    Appelle Claude pour prioriser/détailler, puis applique sous contrat. Dégrade
    proprement (retourne les candidats inchangés) si aucune clé API. Logge dans
    `agent_logs`.
    """
    from core.config import settings

    if not settings.anthropic_api_key:
        return ApplyResult(candidates=candidates)

    payload = build_llm_payload(candidates, memory)
    try:
        import anthropic
        client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=settings.claude_model,
            max_tokens=1500,
            system=("Tu PRIORISES et DÉTAILLES des candidats d'optimisation Pinterest "
                    "déjà chiffrés. " + OUTPUT_CONTRACT),
            messages=[{"role": "user", "content": json.dumps(payload, default=str, ensure_ascii=False)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        result = apply_llm_output(candidates, text)
    except Exception as exc:  # ne jamais casser le pipeline déterministe
        return ApplyResult(candidates=candidates, rejected_refs=[f"<llm error: {exc}>"])

    try:
        from models import AgentLog
        db.add(AgentLog(role="optimizer_llm", content=text,
                        tool_name="prioritize_with_llm",
                        tool_input={"n_candidates": len(candidates)},
                        tool_output={"rejected": result.rejected_refs, "stripped": result.stripped}))
        db.commit()
    except Exception:
        db.rollback()
    return result
