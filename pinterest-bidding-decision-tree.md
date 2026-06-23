# Pinterest — Arbre de décision enchère & budget (pack de référence)

> Règles internes de l'optimiseur (cohérentes avec v2 §5/§11 + PATCH_BRIEF
> consolidation). Le « CPA cible » en mode automatique est un **repère interne**
> (dimensionnement budget + jugement à la review), pas une valeur saisie dans Pinterest.

## Enchère : automatique par défaut
```
Nouvelle campagne / relance / cold-start
   → AUTOMATIC_BID, is_roas_optimized = true   (laisse l'optimiseur Pinterest chercher)

Passage en MANUEL (plafond de coût) — UNIQUEMENT si TOUT est vrai :
   • phase = SCALING
   • hors apprentissage (learning)
   • ≥ 50 conversions / semaine
   • CPA réalisé qui dérive AU-DESSUS du break-even
   → CHANGE_BID_STRATEGY = MANUAL, cost_cap = break_even_CPA
   ✗ Jamais au démarrage / 1re campagne.
```

## Objectif par volume
```
< 50 conversions / semaine  → consolider (Consideration / 1 ad group nourri)
≥ 50 conversions / semaine  → possibilité de scinder en plusieurs ad groups
```

## Plancher de budget
```
budget_quotidien ≥ 5 × CPA cible            (GATE-5XCPA)
budget conseillé = (50 / 7) × CPA cible      (viser 50 conv/sem)
CPA cible ancré sur la médiane des créas RETENUES du cluster, plafonné au
break-even — jamais le CPA blended du compte.
```

## Garde 3× Kill
```
CPA réalisé > 3 × break_even_CPA  → pauser l'entité (GATE-KILL3X), ne jamais scaler.
```
