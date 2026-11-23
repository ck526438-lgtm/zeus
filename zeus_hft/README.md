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
├── strategies/hft_strategy.py  # OBI + micro-momentum + filtres + sorties
├── strategies/online_model.py  # IA en ligne auto-évaluée
├── execution/order_manager.py  # cycle de vie, client_order_id déterministes, anti-doublons
├── execution/sim_exchange.py   # latence, slippage, frais maker/taker (paper + backtest)
├── risk/risk_manager.py      # kill-switch
├── backtest/simulator.py     # tick par tick, même code que le live
└── app/gui.py, factory.py    # interface Windows, assemblage
```

Tests : `pytest -q` (carnet, risque, ordres, IA, backtest, passerelle MT5 simulée).
Les `.exe` sont construits et testés automatiquement sur Windows par
`.github/workflows/windows-app.yml` ; pousser un tag `v2.0.0` publie une Release.

## Licence

MIT — utilisation à tes propres risques. Ceci n'est pas un conseil financier.
