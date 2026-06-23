# Pinterest — Benchmarks (CALIBRÉS SUR TES DONNÉES)

> ⚠️ Ce ne sont **pas** des moyennes d'industrie. Les seuils sont les **percentiles
> de TES propres entités gagnantes**, recalculés depuis la table `calibration`
> (Phase 4). C'est l'avantage sur les benchmarks génériques.
>
> La version chiffrée à jour est rendue à la demande par
> `optimizer.refpack.render_benchmarks(calibration)` (lit `calibration` en DB).

## Métriques calibrées (P25 / P50 / P75 du compte)
- **CTR**, **outbound CTR**, **add-to-cart CR**, **checkout CR**, **save-rate** :
  P75 = « bon » · P25 = seuil kill.
- **CPA web-click** : `GOOD_CPA` = P25 · `MEDIAN_CPA` = P50 · `KILL_CPA` = P75.
- **Courbe de fatigue** : fréquence de déclin estimée sur l'historique.

## Économie unitaire (dérivée de MARGIN_CONTRIB)
- `break_even_CPA = AVG_BASKET × MARGIN_CONTRIB`
- `break_even_ROAS = 1 / MARGIN_CONTRIB`
- AVG_BASKET dérivé : Σ web_click_checkout_value / Σ web_click_checkout.

Voir `optimizer/calibration.py` pour le calcul et `calibration` (DB) pour les valeurs.
