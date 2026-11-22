# ⚡ ZEUS — IA de trading automatique pour MetaTrader 5

Zeus analyse le marché en continu, **ouvre les trades quand son analyse est assez sûre**,
les **gère** (break-even, stop suiveur) et **les coupe** quand l'analyse se retourne,
quand le trade stagne ou quand le SL/TP est touché. Il est pensé pour le **scalping
haute fréquence en M1** (vérification chaque seconde) sur l'or (XAUUSD), l'EURUSD ou
tout symbole MT5.

> ⚠️ **À lire avant tout.** Aucun bot ne peut garantir que son analyse est « juste ».
> Zeus n'entre que quand plusieurs indicateurs **et** le modèle IA sont d'accord, mais il
> perdra des trades, et il peut perdre de l'argent sur la durée. Le trading à effet de
> levier est risqué. **Teste au moins 2 à 4 semaines sur un compte démo** avant le réel.
> Zeus refuse de trader sur un compte réel sans double confirmation.

---

## 🧠 Comment Zeus décide

```
Bougie M1 clôturée
      │
      ▼
1. Analyse technique ──► EMA 9/21/100, RSI, MACD, ADX, ATR, cassure 10 bougies
      │                  → score d'accord 0 à 1 (achat ou vente)
      ▼
2. Filtre tendance  ──► ADX < 18 → pas de trade (marché sans direction)
      ▼
3. Score ≥ 0.60 ?   ──► sinon on attend
      ▼
4. IA (gradient boosting) ──► probabilité que le TP soit touché avant le SL ≥ 58 % ?
      ▼
5. Gestion du risque ──► perte du jour, série de pertes, spread, horaires, cooldown
      ▼
6. ORDRE : lot calculé pour risquer 0.5 % du capital, SL = 1.5×ATR, TP = 2×ATR
      ▼
7. Chaque seconde : break-even à +1 ATR, puis stop suiveur
   Chaque bougie   : coupe si l'analyse s'inverse ou après 30 bougies
```

## 🛡️ Protections intégrées

| Protection | Défaut |
|---|---|
| Risque par trade | 0.5 % du capital |
| Perte max par jour | 3 % → Zeus s'arrête jusqu'au lendemain |
| Pertes consécutives | 5 → pause de 60 min |
| Trades max par jour | 60 |
| Spread max | 40 points |
| Positions max | 1 par symbole, 3 au total |
| Horaires | 07h–20h UTC (sessions Londres / New York) |
| Compte réel | bloqué sauf `mode: live` + `--confirm-live` |

## 🚀 Installation (Windows)

1. Installe **Python 3.10+** et **MetaTrader 5**, connecte-toi à un **compte démo**.
2. Dans MT5 : *Outils → Options → Expert Advisors* → coche **Autoriser le trading algorithmique**.
3. Double-clique sur `install.bat`.
4. Mets tes identifiants dans les variables d'environnement (voir `.env.example`)
   ou laisse vides : Zeus utilise alors le compte déjà ouvert dans MT5.

## ▶️ Utilisation

```bash
# 1. Télécharger l'historique (90 jours de M1)
python main.py download --symbol XAUUSD --days 90

# 2. Entraîner l'IA (70 % des données ; 30 % gardés pour le test)
python main.py train --csv data/XAUUSD_M1.csv

# 3. Backtest sur les données JAMAIS vues par l'IA
python main.py backtest --csv data/XAUUSD_M1.csv --export data/trades.csv

# 4. Lancer Zeus en direct (démo)
python main.py live
```

Raccourcis Windows : `train_zeus.bat` (étapes 1 à 3) et `start_zeus.bat` (étape 4).

Tester sans MT5 (Linux/Mac aussi) : `python main.py backtest --synthetic --no-model`

## ⚙️ Réglages

Tout est dans [`config.yaml`](config.yaml). Les plus importants :

- `trading.symbols` — symboles tradés (vérifie le nom exact chez ton broker : `XAUUSD`, `GOLD`, `XAUUSDm`…)
- `strategy.min_score` / `model_min_proba` — plus haut = moins de trades mais plus sélectifs
- `risk.risk_per_trade_pct` — ne dépasse pas 1 %
- `telegram` — recevoir chaque ouverture/fermeture sur Telegram

## 📊 Lire les résultats honnêtement

- Le backtest avec IA porte **uniquement sur la période que le modèle n'a pas vue**
  (`--in-sample` existe mais donne des résultats trompeurs).
- Au `train`, compare la **précision** à la **base** : si l'IA ne fait pas mieux que la
  base, elle n'apporte rien sur ce symbole.
- Un **profit factor > 1.3** et un **drawdown < 15 %** hors échantillon sont un bon début,
  mais seule la démo en conditions réelles (spread variable, glissement) confirme.
- Réentraîne l'IA chaque semaine ou chaque mois : le marché change.

## 📁 Structure

```
zeus/
├── main.py              # commandes : live, download, train, backtest
├── config.yaml          # tous les réglages
├── zeus/
│   ├── engine.py        # boucle temps réel : ouvre, gère, coupe
│   ├── strategy.py      # analyse multi-indicateurs + filtre IA
│   ├── model.py         # IA : labels triple barrière + walk-forward
│   ├── risk.py          # taille de lot, limites, break-even, stop suiveur
│   ├── mt5_client.py    # connexion et ordres MetaTrader 5
│   ├── backtest.py      # simulateur (spread, commission, pire cas)
│   ├── features.py / indicators.py / data.py / journal.py
└── tests/               # 13 tests automatiques
```

Journal des trades : `data/journal.csv` · Logs : `data/zeus.log`

## Licence

MIT — utilisation à tes propres risques. Ceci n'est pas un conseil financier.
