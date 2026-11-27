"""Stratégie OBI + micro-momentum + modèle adaptatif en ligne.

Signal composite (dans [-1, 1]) :
    règle  = 0.45·OBI + 0.35·tanh(momentum / échelle) + 0.20·écart microprice
    score  = (1 − c)·règle + c·edge_IA      (c = confiance mesurée du modèle, 0 à 0.6)

- OBI : déséquilibre du DOM MT5 sur N niveaux (pondéré) ; si le broker ne
  fournit pas de profondeur, on utilise le flux de ticks signé (proportion
  d'upticks − downticks sur une fenêtre), qui en est le proxy classique.
- Micro-momentum : écart EMA rapide / EMA lente du mid, en bps.
- Filtres : spread max (points), volatilité EWMA min/max (bps par tick).
- Sorties : take-profit / stop-loss en points, durée max, retournement du signal.
Tout est O(1) par tick.
"""
from __future__ import annotations

import math

import numpy as np

from zeus_hft.config.settings import Settings
from zeus_hft.core.orderbook import OrderBook, depth_is_informative
from zeus_hft.core.ringbuffer import RingBuffer
from zeus_hft.core.types import Decision, Tick, Urgency
from zeus_hft.strategies.base import Strategy
from zeus_hft.strategies.online_model import OnlineLogit

W_OBI, W_MOM, W_MICRO = 0.45, 0.35, 0.20
MAX_MODEL_WEIGHT = 0.6


class OBIMomentumStrategy(Strategy):
    name = "obi_momentum_ai"

    def __init__(self, s: Settings, point: float | None = None, use_model: bool = True) -> None:
        self.s = s
        self.point = point if point is not None else s.point
        self.qty = s.order_qty
        self._a_fast = 2.0 / (s.momentum_fast + 1)
        self._a_slow = 2.0 / (s.momentum_slow + 1)
        self._a_vol = 1.0 - 0.5 ** (1.0 / s.vol_halflife)
        self._max_hold_ns = int(s.max_holding_s * 1e9)
        self._cooldown_ns = int(s.decision_cooldown_ms * 1e6)
        self.model = OnlineLogit(6) if use_model else None
        self.model_path: str | None = None
        self.model_loaded = False
        self._x = np.zeros(6)
        self.reset()

    def attach_model_file(self, path: str) -> None:
        """Charge l'IA déjà entraînée (si le fichier existe) et la sauvegardera au même endroit."""
        self.model_path = path
        if self.model is not None:
            self.model_loaded = self.model.load(path)

    def save_model(self) -> None:
        if self.model is not None and self.model_path:
            self.model.save(self.model_path)

    def reset(self) -> None:
        self._flow = RingBuffer(self.s.tick_flow_window)
        self._flow_sum = 0.0
        self._ema_fast = 0.0
        self._ema_slow = 0.0
        self._var = 0.0
        self._last_mid = 0.0
        self._n = 0
        self._entry_ns = 0
        self._last_change_ns = 0
        self._last_target = 0.0
        self.last: dict[str, float] = {}
        if self.model is not None:
            self.model.reset_pending()

    # ------------------------------------------------------------------ #
    def _update_features(self, mid: float) -> None:
        if self._n == 0:
            self._ema_fast = self._ema_slow = mid
        else:
            r = (mid / self._last_mid - 1.0) * 1e4
            self._var += self._a_vol * (r * r - self._var)
            sgn = 1.0 if mid > self._last_mid else (-1.0 if mid < self._last_mid else 0.0)
            if self._flow.full:
                self._flow_sum -= self._flow.last(self._flow._cap - 1)
            self._flow.append(sgn)
            self._flow_sum += sgn
            self._ema_fast += self._a_fast * (mid - self._ema_fast)
            self._ema_slow += self._a_slow * (mid - self._ema_slow)
        self._last_mid = mid
        self._n += 1

    def on_tick(self, book: OrderBook, tick: Tick, position: float, avg_price: float,
                now_ns: int) -> Decision | None:
        top = book.top()
        if top is None:
            return None
        s = self.s
        mid, spread = top.mid, top.spread
        self._update_features(mid)

        # --- Signaux ---------------------------------------------------- #
        n_flow = len(self._flow)
        tick_flow = self._flow_sum / n_flow if n_flow else 0.0
        if depth_is_informative(book):
            obi = book.imbalance(s.imbalance_levels, s.imbalance_decay)
            half = spread * 0.5
            micro = (top.microprice - mid) / half if half > 0 else 0.0
        else:
            obi, micro = tick_flow, 0.0
        mom_bps = (self._ema_fast - self._ema_slow) / mid * 1e4
        mom = math.tanh(mom_bps / s.momentum_scale_bps)
        vol = math.sqrt(self._var)
        spread_pts = spread / self.point

        rule = W_OBI * obi + W_MOM * mom + W_MICRO * micro
        score = rule
        conf = 0.0
        if self.model is not None:
            x = self._x
            x[0], x[1], x[2], x[3], x[4], x[5] = obi, tick_flow, mom, micro, spread_pts, vol
            self.model.step(x, mid)
            conf = self.model.confidence * MAX_MODEL_WEIGHT
            score = (1.0 - conf) * rule + conf * self.model.edge

        self.last = {"obi": obi, "flow": tick_flow, "mom_bps": mom_bps, "micro": micro,
                     "vol_bps": vol, "spread_pts": spread_pts, "score": score,
                     "model_conf": conf,
                     "model_acc": self.model.accuracy if self.model else 0.0,
                     "model_samples": float(self.model.samples) if self.model else 0.0}

        if self._n < s.warmup_ticks:
            return None

        # --- Gestion de la position ouverte ---------------------------- #
        if position != 0.0:
            if self._entry_ns == 0:
                self._entry_ns = now_ns
            d = 1.0 if position > 0 else -1.0
            pnl_pts = (mid - avg_price) / self.point * d
            if pnl_pts <= -s.stop_loss_points:
                return self._decide(0.0, Urgency.AGGRESSIVE, "stop-loss", score, now_ns, force=True)
            if now_ns - self._entry_ns >= self._max_hold_ns:
                return self._decide(0.0, Urgency.AGGRESSIVE, "durée max", score, now_ns, force=True)
            if pnl_pts >= s.take_profit_points:
                return self._decide(0.0, Urgency.PASSIVE, "take-profit", score, now_ns,
                                    price=top.ask if d > 0 else top.bid, force=True)
            if score * d <= -s.entry_threshold:
                urg = Urgency.AGGRESSIVE if abs(score) >= s.aggressive_threshold else Urgency.PASSIVE
                return self._decide(-d * self.qty, urg, "retournement", score, now_ns)
            if score * d <= -s.exit_threshold:
                return self._decide(0.0, Urgency.PASSIVE, "signal faiblit", score, now_ns,
                                    price=top.ask if d > 0 else top.bid)
            return self._decide(position, Urgency.PASSIVE, "tenir", score, now_ns)
        self._entry_ns = 0

        # --- Entrées ---------------------------------------------------- #
        if spread_pts > s.max_spread_points or not (s.min_vol_bps <= vol <= s.max_vol_bps):
            return self._decide(0.0, Urgency.PASSIVE, "filtre spread/vol", score, now_ns)
        if score >= s.entry_threshold:
            urg = Urgency.AGGRESSIVE if score >= s.aggressive_threshold else Urgency.PASSIVE
            return self._decide(self.qty, urg, "achat", score, now_ns, price=top.bid)
        if score <= -s.entry_threshold:
            urg = Urgency.AGGRESSIVE if score <= -s.aggressive_threshold else Urgency.PASSIVE
            return self._decide(-self.qty, urg, "vente", score, now_ns, price=top.ask)
        return self._decide(0.0, Urgency.PASSIVE, "neutre", score, now_ns)

    def _decide(self, target: float, urg: Urgency, reason: str, score: float, now_ns: int,
                price: float | None = None, force: bool = False) -> Decision:
        # Hystérésis temporelle : un CHANGEMENT de cible n'est accepté qu'après le cooldown.
        if (not force and target != self._last_target
                and now_ns - self._last_change_ns < self._cooldown_ns):
            target, urg, reason, price = self._last_target, Urgency.PASSIVE, "cooldown", None
        if target != self._last_target:
            self._last_target = target
            self._last_change_ns = now_ns
        return Decision(target, urg, price, reason, score)

    def state(self) -> dict[str, float]:
        return dict(self.last)
