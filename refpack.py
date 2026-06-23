"""
Pack de référence Pinterest (RAG, chargé à la demande) — import claude-ads §6.

3 références : specs créatives (sourcées doc Pinterest), arbre de décision enchère,
benchmarks **calibrés sur tes données** (rendus depuis la table `calibration`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

REF_DIR = Path(__file__).resolve().parent / "reference"

REFERENCES = {
    "creative-specs": "pinterest-creative-specs.md",
    "benchmarks": "pinterest-benchmarks.md",
    "bidding": "pinterest-bidding-decision-tree.md",
}


def list_references() -> list[str]:
    return sorted(REFERENCES.keys())


def load_reference(name: str) -> str:
    if name not in REFERENCES:
        raise KeyError(f"référence inconnue : {name} (dispo : {list_references()})")
    return (REF_DIR / REFERENCES[name]).read_text("utf-8")


def render_benchmarks(calibration: dict, economics=None) -> str:
    """Rend les benchmarks chiffrés à partir de la calibration (percentiles compte)."""
    lines = ["# Benchmarks calibrés (tes percentiles)", ""]
    cpa_keys = [("GOOD_CPA", "CPA excellent (P25)"), ("MEDIAN_CPA", "CPA médian (P50)"),
                ("KILL_CPA", "CPA kill (P75)")]
    for key, label in cpa_keys:
        v = (calibration.get(key) or {}).get("value")
        if v is not None:
            lines.append(f"- **{label}** : {v} €")
    for key in sorted(k for k in calibration if k.startswith("BENCH_")):
        b = calibration[key]
        if isinstance(b, dict) and "good" in b:
            lines.append(f"- **{key}** : bon (P75) = {b['good']} · kill (P25) = {b['kill']}")
    if economics is not None:
        lines += ["", "## Économie unitaire",
                  f"- break-even CPA : {economics.break_even_cpa} €",
                  f"- break-even ROAS : {economics.break_even_roas}",
                  f"- panier moyen : {economics.avg_basket} €"]
    return "\n".join(lines)
