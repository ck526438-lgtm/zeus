# ⚡ ZEUS HFT — moteur de scalping haute fréquence pour MetaTrader 5

Zeus HFT lit le **carnet d'ordres (DOM) et les ticks MT5** toutes les 2 ms. Il calcule
en continu le **déséquilibre du carnet (OBI)**, le **micro-momentum** et le **microprice**,
et laisse une **IA adaptative** apprendre sur le marché du moment. Il place, déplace et
annule ses ordres en quelques dizaines de microsecondes de calcul, sous un
**coupe-circuit strict**.

> ⚠️ **Honnêteté avant tout.** Aucun robot ne peut garantir des gains. Le backtest
> synthétique ne prouve rien sur le vrai marché : il vérifie seulement que la mécanique
> fonctionne. Valide toujours dans cet ordre : **backtest sur tes vrais ticks → DRY_RUN
> → compte démo pendant 2 à 4 semaines → réel en petit lot**. Le trading à effet de levier
> peut faire perdre plus que prévu.

---

## 🧠 Comment il décide (à chaque tick)

```
Tick + DOM MT5 (thread dédié, toutes les 2 ms)
      │
      ▼
Carnet L2 local ──► mid, spread, microprice, OBI pondéré sur 5 niveaux
      │              (sans DOM chez le broker : flux de ticks signé)
      ▼
Signal règle = 0.45·OBI + 0.35·momentum + 0.20·microprice
      │
      ▼
IA en ligne  ──► régression logistique apprise tick après tick :
      │          P(le prix monte dans 10 ticks)
      │          Son poids est NUL tant qu'elle ne bat pas 50 %,
      │          puis augmente avec sa précision mesurée (max 60 %)
      ▼
Filtres      ──► spread max, volatilité min/max, cooldown
      ▼
Position cible ──► OrderManager : nouvel ordre / modification de prix /
      │            annulation, au minimum d'actions
      ▼
Kill-switch  ──► vérifié AVANT CHAQUE ordre
```

**Sorties :** take-profit (passif), stop-loss (au marché), durée max, signal qui faiblit
ou se retourne.

## 🛡️ Coupe-circuit (kill-switch)

| Protection | Défaut | Effet |
|---|---|---|
| Drawdown journalier (depuis le plus haut du jour) | 300 | arrêt définitif + fermeture de tout |
| Inventaire net max | 0,05 lot | ordre refusé (les réductions restent permises) |
| Ordres par seconde | 8 | ordre refusé |
| Latence (ping MT5 ou aller-retour d'ordre) | 150 ms | ordres bloqués ; 3 fois de suite → arrêt d'urgence |
| Données trop anciennes | 5 s | ordre refusé |
| Compte réel | bloqué | sauf `--confirm-live` ou la case dans l'appli |

## 🚀 Installation (Windows)

**Option 1 — l'application `.exe` (sans Python)**
1. Télécharge `ZeusHFT-windows.zip` dans l'onglet **Actions** (artefact) ou **Releases** du dépôt.
2. Ouvre MetaTrader 5, connecte un **compte démo**, active *Outils → Options → Expert
   Advisors → Autoriser le trading algorithmique*.
3. Lance **`ZeusHFT-App.exe`** (interface) ou **`ZeusHFT.exe`** (ligne de commande).

**Option 2 — depuis le code**
```bat
install.bat
start_zeus_hft.bat
```

Identifiants MT5 : variables `ZEUS_MT5_LOGIN`, `ZEUS_MT5_PASSWORD`, `ZEUS_MT5_SERVER`
(comme Zeus v1), ou rien du tout pour utiliser le compte déjà ouvert dans MT5.

## ▶️ Utilisation

```bash
# 1. Simulation sans MT5 (n'importe quel PC)
python -m zeus_hft backtest --synthetic 200000
python -m zeus_hft run --synthetic             # paper trading en temps réel, flux synthétique

# 2. Tes vrais ticks (Windows + MT5)
python -m zeus_hft download --symbol XAUUSD --days 5
python -m zeus_hft backtest --csv data/XAUUSD_ticks.csv --export data/equity.csv

# 3. Paper trading sur le flux MT5 réel (aucun ordre envoyé)
python -m zeus_hft run

# 4. Ordres réels (compte démo d'abord !)
python -m zeus_hft run --mode LIVE
python -m zeus_hft run --mode LIVE --confirm-live   # compte réel

# Interface graphique
python -m zeus_hft gui
```

Options utiles : `--symbol`, `--lot`, `--max-dd`, `--max-inventory`, `--no-model`
(désactive l'IA), `--touch-fill 0.3` (backtest moins prudent sur les ordres passifs).
Tous les réglages : [`config/settings.py`](config/settings.py), surchargeables par
variables `ZEUS_HFT_*` (voir `.env.example`).

## 🧱 Version 2.4 : architecture HFT en 7 couches, adaptée à MT5

Le bot suit le pipeline classique d'un bot HFT (*Market Data → Order Book → Strategy → Order
Manager → Risk*). Voici, couche par couche, ce qui est en place et ce qui **ne peut pas** l'être
avec MetaTrader 5 en compte particulier.

| Couche | Dans Zeus HFT |
|---|---|
| 1. Infrastructure | VPS Windows proche du broker conseillé. Co-location, kernel bypass et FPGA : impossibles via un broker MT5 |
| 2. Feed handler | thread dédié 2 ms, reconnexion avec backoff, heartbeat, **chaque tick enregistré** (`data/ticks/*.csv.gz`, rejouable) |
| 3. Book builder | carnet L2 trié O(log n), détection de trous de séquence, OBI, microprice, spread pré-calculés |
| 4. Exécution | `client_order_id` déterministes (idempotence), états NEW→ACK→PARTIAL→FILLED/CANCELED/REJECTED, cancel/replace natif, **mode dégradé** si > 30 % de rejets |
| 5. Décision | momentum, mean-reversion, OBI + IA ; paramètres modifiables **à chaud** ; walk-forward |
| 6. Risque | contrôles **avant chaque ordre** : position, perte du jour, ordres/s, **notionnel (levier max)**, **drawdown sur fenêtre glissante**, kill-switch, **heartbeat → annulation de tous les ordres**, réduction seule automatique, **état sauvegardé toutes les 5 s** |
| 7. Supervision | **alertes Telegram**, **métriques Prometheus/Grafana**, latence par étape (tick→décision→envoi→ACK), **journal infalsifiable** (chaîne SHA-256) |

### Nouveautés v2.4
- **Reprise après crash** : l'état du risque (début de journée, arrêt journalier, série de pertes,
  pauses) est sauvegardé toutes les 5 s. Un redémarrage **ne lève pas** un arrêt pour perte du jour.
- **Heartbeat de données** : si le flux se coupe, tous les ordres en attente sont annulés et le bot passe en
  réduction seule. Les positions restent protégées par leur SL serveur.
- **Limite notionnelle** : exposition ≤ 20 × equity. Le calcul des lots en tient compte.
- **Drawdown glissant** : 1,5 % de l'equity en moins de 60 min → réduction seule pendant 1 h.
- **Mode dégradé** : plus de 30 % d'ordres rejetés sur 60 s → réduction seule pendant 5 min.
- **Alertes Telegram** (jeton et chat id dans l'appli, ou `ZEUS_TG_TOKEN` / `ZEUS_TG_CHAT`) :
  démarrage et arrêt, kill-switch, pauses, MT5 déconnecté, latence anormale, flux rétabli, bilan quotidien.
  L'envoi se fait dans un thread séparé et ne bloque jamais le trading.
- **Prometheus** : `ZeusHFT.exe run --metrics-port 9108`, puis `http://127.0.0.1:9108/metrics`.
- **Configuration à chaud** : crée ou édite `data/zeus_hft_hot.json`. Le bot l'applique en 5 s, sans redémarrage :
  ```json
  {"trading_enabled": false}
  {"max_spread_points": 30, "risk_per_trade_pct": 0.25, "velocity_entry_atr": 0.5}
  ```
- **Journal infalsifiable** : chaque trade contient l'empreinte de la ligne précédente ;
  `ZeusHFT.exe verify-journal` détecte toute ligne modifiée ou supprimée.
- **Backtest** : latence fixe + **gigue aléatoire** (10 ms), et rejeu direct des ticks enregistrés :
  `ZeusHFT.exe backtest --csv data/ticks/XAUUSD_20261127.csv.gz`.

### Pourquoi pas de C++, de FIX ou de co-location ?
- **Le goulot est le réseau, pas le code.** Une décision prend environ 40 µs en Python ; un ordre MT5 met
  20 à 150 ms pour atteindre le broker, soit environ 1 000 fois plus. Réécrire le moteur en C++ ne
  changerait pas le résultat.
- **MT5 n'offre ni FIX, ni ITCH/OUCH, ni co-location.** La seule voie plus rapide côté MT5 est un EA
  MQL5 exécuté dans le terminal, qui gagne moins d'une milliseconde.
- **Le market making n'est pas possible sur un CFD MT5.** Le broker fixe les prix : un ordre limite ne
  s'exécute que si son prix est traversé. Tu ne captures donc pas le spread, tu le paies.
- Tes documents disent la même chose : *« commence par du HFT léger (secondes) sur crypto ou CFD »* et
  *« la plupart des bots meurent de frais et de slippage mal modélisés, pas de manque de vitesse »*.

## 🏛️ Version 2.3 : filtres, gestion du capital, validation

### 1. Multi-filtres : la plupart des ticks sont ignorés
Une entrée n'est acceptée que si **tous** les filtres sont d'accord :

| Filtre | Règle |
|---|---|
| **Tendance H1** | EMA 50/200 : le momentum ne trade **que dans le sens H1** ; si H1 est neutre, dans le sens M15 |
| **Range M15** | mean-reversion seulement si **ADX M15 ≤ 22**, et jamais contre l'impulsion H1 |
| **Volatilité adaptative** | SL et TP en **multiples d'ATR M1** ; la mean-reversion est coupée si la volatilité dépasse le seuil |
| **Sessions** | momentum 08:00–11:30 et 13:30–17:00 UTC · **rollover 21:00–23:00 UTC bloqué** |
| **Spread** | refus si le spread dépasse **1,5 × sa moyenne mobile** (et le plafond absolu) |
| **News** | **15 min avant / 15 min après** chaque annonce USD à fort impact du **calendrier MT5** |
| **IA** | veto si elle est confiante dans le sens opposé |

L'historique M1, M15 et H1 est pré-chargé depuis MT5 : les filtres sont prêts dès le lancement.

### 2. Gestion du capital
- **Taille des lots dynamique** : `lots = capital × 0,5 % / (distance du SL × valeur du tick)`,
  à partir de `SYMBOL_TRADE_TICK_VALUE` et `SYMBOL_TRADE_TICK_SIZE`, arrondie au pas inférieur et plafonnée
  (`max_lots`). Si même le lot minimum risque plus de 1,5 × le budget, le trade est refusé.
  Mets **0 %** dans l'appli pour revenir au lot fixe.
- **Break-even** à +1R : le SL passe au prix d'entrée + commission.
- **Clôture partielle** : 50 % encaissés au premier objectif, puis **trailing stop à 1 ATR** sur le
  reste. Avec le lot minimum (0,01), la position ne peut pas être coupée en deux : elle est
  fermée en entier au premier objectif.
- Chaque stop resserré (break-even, trailing) est **reporté sur le SL serveur MT5** (`TRADE_ACTION_SLTP`).
- **Kill-switch multi-niveaux** : perte du jour 3 % (reprise le lendemain) · **4 pertes de suite →
  pause de 2 h** · 1 seule position ouverte · rejets répétés · flux figé.

### 3. Exécution
- Déviation maximale par ordre : `ZEUS_HFT_DEVIATION_POINTS` (10 par défaut).
- **Calendrier économique natif de MT5** : le paquet Python de MT5 n'y donne pas accès
  (`CalendarValueHistory` n'existe qu'en MQL5). Un petit EA fourni, `mql5/ZeusNewsExporter.mq5`, l'exporte
  pour le bot :
  1. Copie `ZeusNewsExporter.mq5` dans `MQL5\Experts\` (dans MT5 : *Fichier → Ouvrir le dossier des données*).
  2. Ouvre-le dans MetaEditor et compile-le (F7).
  3. Fais-le glisser sur n'importe quel graphique. Il ne passe aucun ordre : il met à jour
     `Common\Files\zeus_news.csv` toutes les 15 min (NFP, CPI, FOMC, discours de la Fed…).
  4. Le bot relit ce fichier chaque minute ; la prochaine annonce s'affiche en bas de l'appli.
- **Infrastructure** : un VPS Windows dans le datacenter du broker (LD4 / NY4, ping < 5 ms) et un compte
  **Raw Spread / ECN**. Sur l'or, c'est souvent ce qui fait la différence entre gagner et perdre.

### 4. Validation quantitative (contre le sur-apprentissage)
```bash
ZeusHFT.exe download --symbol XAUUSD --days 60          # ticks + historique M15/H1
ZeusHFT.exe walkforward --csv data/XAUUSD_ticks.csv --train-days 20 --test-days 5 --utc-offset 2
ZeusHFT.exe backtest --csv data/XAUUSD_ticks.csv --utc-offset 2 --montecarlo 5000 --mc-cost 0.5
ZeusHFT.exe montecarlo --trades data/zeus_hft_trades.csv --mode LIVE
```
- **Walk-forward** : optimisation sur 20 jours, test **sans modification** sur les 5 suivants, puis la
  fenêtre glisse. Seul le résultat hors échantillon compte. Une efficacité inférieure à environ 50 %
  signale une sur-optimisation.
- **Monte Carlo** : ordre des trades mélangé, 5 % de trades manqués, slippage ou spread aléatoire →
  PnL au 5e percentile, drawdown au 95e percentile, risque de ruine. Il refuse de conclure sous 30 trades.
- **Télémétrie par trade** (`data/zeus_hft_trades.csv`) : spread à l'entrée, **slippage réel** (prix
  demandé − prix exécuté) et **latence d'exécution** en ms.

## 🎯 Les stratégies (v2.2) et le mode automatique

En mode **Auto** (par défaut), le bot lit le **régime de marché** à chaque tick et choisit
lui-même la stratégie. Le régime courant est affiché dans l'appli.

| Régime | Quand | Stratégie autorisée |
|---|---|---|
| **TENDANCE / NORMAL** | sessions de Londres (07:00–11:00) et New York (13:30–17:00 UTC), volatilité 0,6 à 2,5 × la normale | **1. Momentum / cassure** |
| **CALME** | fin de séance US (19:30–21:30) et nuit (00:30–06:00 UTC), volatilité ≤ 1,1 × la normale | **2. Micro mean-reversion** |
| **OUVERTURE** | 5 premières minutes de Londres et de New York | aucune (slippage) |
| **NEWS** | 5 min avant à 15 min après chaque heure de news saisie | aucune |
| **CHOC** | le prix bouge de plus de 2 ATR en 2 s (news imprévue) | aucune pendant 2 min |
| **HORS-SESSION** | le reste du temps | aucune |

**1. Scalping momentum / cassure.** Le prix casse le plus haut (ou le plus bas) des 60
dernières secondes, et la vitesse sur 2 s dépasse 0,45 × ATR M1. Entrée **au marché**, TP court
(0,8 ATR), **SL serré** (0,5 ATR), 60 s maximum, sortie si la vitesse se retourne.

**2. Micro mean-reversion.** Le prix s'écarte d'au moins 1,2 ATR de la moyenne courte
(EMA ; le VWAP intrajournalier est disponible en option) et l'extension **ralentit**. Entrée en
**ordre limite**, ce qui évite de payer le spread à l'entrée. Sortie au retour vers la moyenne, avec un
**SL strict** (1,2 ATR) et une durée maximale. Le bot **sort au marché dès que la volatilité monte**, car sur
l'or une extension peut vite devenir une tendance de 200+ pips.

**3. Grille / martingale : volontairement absente.** Le bot n'ouvre qu'**une seule position, à
taille fixe** : l'inventaire maximum est plafonné au lot, et il ne renforce jamais une
position perdante.

**Filtre de coût :** aucune entrée si le gain visé est inférieur à **2,5 × le spread**. Avec un
spread élevé, le bot préfère ne pas trader.

**L'IA** (OBI + flux) continue d'apprendre à chaque tick et sert de **veto** : quand elle est
confiante dans le sens opposé, l'entrée est refusée.

### Sécurités MT5 (avant chaque ordre)
- **Spread en direct** comparé au plafond : entrée refusée au-delà (les sorties restent permises).
- **Stops level / freeze level** du broker lus au démarrage : prix des ordres limites et SL
  ajustés, pas de modification d'un ordre « gelé » (évite `INVALID_STOPS`).
- **SL de secours côté serveur** (1,5 × le SL de la stratégie) sur chaque entrée : la position
  est protégée même si le PC ou le bot s'arrête.
- **Comptes hedging** détectés : les sorties ferment la position **par son ticket**, au lieu
  d'ouvrir une position inverse.
- **Kill-switch** : perte du jour (montant **et** % du capital, reprise automatique le lendemain
  00:00 UTC), 5 rejets d'ordre de suite, latence > 150 ms, **flux figé** (aucun tick depuis 30 s :
  pause, puis reprise automatique).
- **Resynchronisation** : si une position est fermée hors du bot (SL serveur, clôture manuelle),
  le bot s'en aperçoit en 2 s et l'inscrit au journal.

### Fonctionnement 100 % automatique
1. Dans l'appli, coche **« Démarrage automatique »** : le bot démarre seul à l'ouverture et
   **se relance toutes les 30 s** si MT5 n'est pas encore prêt.
2. Clique sur **« Lancer avec Windows »** : l'appli s'ouvre à chaque démarrage du PC.
3. Saisis les heures de news de la semaine (UTC), par exemple `13:30` pour les chiffres
   américains, ou `2026-12-04 13:30` pour le NFP.
4. Réglages, mémoire de l'IA et journal sont conservés dans `data/`. Seule la case
   « Autoriser compte réel » n'est **jamais** mémorisée, par sécurité.

L'ATR M1 est pré-chargé depuis l'historique MT5 : le bot peut trader dès la première minute.

### Backtest réaliste
Le bot est en Python et non en MQL5 : il ne passe donc pas par le Strategy Tester de MT5.
L'équivalent du mode « Every tick based on real ticks » est le suivant :
```bash
ZeusHFT.exe download --symbol XAUUSD --days 10       # vrais ticks de TON broker
ZeusHFT.exe backtest --csv data/XAUUSD_ticks.csv --utc-offset 2 --strategy auto
```
Le backtest inclut la latence (40 ms), le slippage (2 points), la commission et l'exécution
prudente des ordres limites. Il donne le résultat **par stratégie** et le temps passé dans chaque régime.

## 🎓 Le laisser apprendre sur un compte démo MT5

C'est l'usage recommandé : de **vrais ordres**, dans les vraies conditions du broker
(spread, glissement, refus), **sans argent réel**.

1. Dans MT5, connecte-toi à un **compte démo** et active le trading algorithmique.
2. Ouvre `ZeusHFT-App.exe`, laisse **Mode = LIVE** et la case **« Autoriser compte réel » décochée**.
3. Clique sur **Démarrer**. La barre du bas affiche « compte DÉMO MT5 · LIVE ». Si MT5 est connecté
   à un compte réel, le bot refuse de démarrer.
4. Laisse-le tourner pendant les sessions actives (Londres 07h–11h, New York 13h–17h UTC).

**Ce qui est conservé d'une session à l'autre :**

| Fichier | Contenu |
|---|---|
| `data/zeus_hft_model_XAUUSD.json` | la mémoire de l'IA (sauvegardée chaque minute et à l'arrêt) |
| `data/zeus_hft_trades.csv` | chaque aller-retour : sens, prix, durée, PnL, frais, raison de sortie (s'ouvre dans Excel) |
| `data/zeus_hft.log` | le journal technique |

**Comment savoir s'il devient rentable ?** La barre du bas donne un verdict calculé sur les
100 derniers trades **LIVE** (le DRY_RUN est compté à part) :

- *en apprentissage (n/50 trades)* : pas assez de trades pour juger ;
- *perdant* / *à l'équilibre* : continue en démo et ajuste les réglages ;
- *RENTABLE (PF ≥ 1,2)* : profit factor d'au moins 1,2 et gain net sur les 100 derniers trades.
  Ce n'est pas une garantie pour la suite, mais c'est le minimum avant d'envisager un compte réel
  en lot minimum.

L'IA apprend du **marché** à chaque tick, même quand le bot ne trade pas. Ses poids ne changent
pas à cause des gains ou des pertes de ses propres trades. Le verdict, lui, mesure le résultat
réel des ordres. Si le spread de ton broker est élevé (plus de 30 points sur l'or), aucune IA
ne compensera ce coût : préfère un compte « raw spread » ou ECN.

## ⏱️ Performance et limites réelles de MT5

| Mesure | Valeur typique |
|---|---|
| Calcul tick → décision (backtest, Python) | p50 ≈ 40 µs, p99 ≈ 80 µs |
| Lecture tick + DOM MT5 | toutes les 2 ms (thread dédié, `time.sleep` haute résolution) |
| Aller-retour d'un ordre vers le broker | 20 à 150 ms selon le broker et le VPS |

Le goulot n'est **pas** le calcul, c'est le réseau entre MT5 et le serveur du broker.
Pour gagner vraiment en vitesse : un **VPS Windows dans le même datacenter que le serveur
du broker** (ping < 5 ms) et un broker qui fournit le **DOM** (sinon l'OBI est remplacé
par le flux de ticks, moins précis). Le vrai HFT de co-location à la microseconde n'est pas
accessible via MT5 retail ; Zeus HFT vise le **scalping sub-seconde**, ce que MT5 permet.

**Pourquoi pas de Rust/Cython ?** Le calcul prend environ 40 µs alors que le réseau en
prend 20 000 à 150 000 : optimiser le calcul ne changerait rien de mesurable. Le carnet
d'ordres (`core/orderbook.py`) est isolé, au cas où il faudrait le porter un jour.

## 📁 Structure

```
zeus_hft/
├── config/settings.py        # Pydantic, variables ZEUS_HFT_*, aucune clé en dur
├── core/orderbook.py         # carnet L2 : snapshots, deltas, séquences, OBI, microprice
├── core/engine.py            # boucle event-driven (conflation, heartbeat, réconciliation)
├── core/telemetry.py         # logs via QueueHandler, latences en ns (ring buffers numpy)
├── core/ringbuffer.py, types.py
├── feeds/mt5_gateway.py      # MT5 : flux, ordres, cancel/replace, fills partiels, backoff
├── feeds/synthetic.py        # marché synthétique avec DOM
├── strategies/router.py        # mode Auto : régime de marché -> stratégie
├── strategies/trend.py         # EMA 50/200 H1/M15, ADX M15
├── strategies/exits.py         # break-even, clôture partielle, trailing ATR
├── strategies/news_file.py     # calendrier exporté par mql5/ZeusNewsExporter.mq5
├── execution/sizing.py         # lots selon le % de risque et la valeur du tick
├── backtest/walkforward.py, montecarlo.py   # validation quantitative
├── ops/alerts.py, metrics.py   # Telegram, Prometheus
├── ops/recorder.py, hotconfig.py   # ticks bruts .csv.gz, réglages à chaud
├── strategies/features.py      # bougies M1, ATR, micro-range, vitesse, VWAP, sessions, news
├── strategies/momentum_breakout.py  # stratégie 1 : momentum / cassure
├── strategies/mean_reversion.py     # stratégie 2 : micro mean-reversion
├── strategies/hft_strategy.py  # OBI + micro-momentum + IA
├── strategies/online_model.py  # IA en ligne auto-évaluée
├── execution/order_manager.py  # cycle de vie, client_order_id déterministes, anti-doublons
├── execution/sim_exchange.py   # latence, slippage, frais maker/taker (paper + backtest)
├── execution/journal.py        # journal CSV des trades + verdict de rentabilité
├── risk/risk_manager.py      # kill-switch
├── backtest/simulator.py     # tick par tick, même code que le live
└── app/gui.py, factory.py    # interface Windows, assemblage
```

Tests : `pytest -q` (carnet, risque, ordres, IA, backtest, passerelle MT5 simulée).
Les `.exe` sont construits et testés automatiquement sur Windows par
`.github/workflows/windows-app.yml` ; pousser un tag `v2.0.0` publie une Release.

## Licence

MIT — utilisation à tes propres risques. Ceci n'est pas un conseil financier.
