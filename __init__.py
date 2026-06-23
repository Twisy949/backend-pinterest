"""
Moteur d'optimisation Pinterest Ads.

Coeur déterministe (règles + diagnostics + candidats) découplé de l'ORM : les
fonctions métier opèrent sur des structures Python simples (dicts / dataclasses)
pour être testables sans PostgreSQL. Des adaptateurs minces convertissent depuis
les modèles SQLAlchemy (`backend/models.py`).

Voir `CONTEXT.md` (racine) pour la cartographie phases ↔ modules.
"""
