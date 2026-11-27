"""Stratégie 1 — Scalping de momentum / cassure de micro-range (tick / M1).

Entrée : le mid casse le plus haut (ou plus bas) des ``range_window_s`` dernières
secondes d'au moins ``breakout_buffer_atr`` × ATR M1, ET la vitesse sur
``velocity_window_ms`` dépasse ``velocity_entry_atr`` × ATR dans le même sens.
Ordre au marché (le momentum n'attend pas).

Filtres (via le régime) : sessions de momentum uniquement, pas de choc de
vitesse (news non annoncée), pas de fenêtre de news, pas d'ouverture de session,
volatilité dans [mom_min_vol_ratio, mom_max_vol_ratio] × normale, et TP visé
>= ``min_tp_spread_mult`` × spread (sinon le coût mange le gain).

Sorties : TP court (mom_tp_atr × ATR), SL serré (mom_sl_atr × ATR), durée max,
ou retournement de la vitesse.
"""
from __future__ import annotations

from zeus_hft.core.orderbook import BookTop
from zeus_hft.core.types import Decision, Urgency
from zeus_hft.strategies.exits import ExitManager
from zeus_hft.strategies.features import MarketFeatures


class MomentumBreakout:
    name = "momentum"

    def __init__(self, s, point: float) -> None:  # type: ignore[no-untyped-def]
        self.s = s
        self.point = point
        self.qty = s.order_qty
        self._max_hold_ns = int(s.mom_max_hold_s * 1e9)
        self.exit = ExitManager(s, point, self.name)
        self.vol_step = s.volume_step
        self.vol_min = s.volume_min
        self.reset()

    def reset(self) -> None:
        self._entry_ns = 0
        self._entry_atr = 0.0
        self.last_reason = ""
        self.exit.reset()

    def _d(self, target: float, urg: Urgency, reason: str, price: float | None = None,
           stop: float | None = None) -> Decision:
        self.last_reason = reason
        return Decision(target, urg, price, reason, 0.0, stop, self.name)

    def evaluate(self, f: MarketFeatures, top: BookTop, position: float, avg_price: float,
                 now_ns: int) -> Decision | None:
        s = self.s
        if position != 0.0:
            if self._entry_ns == 0:
                self._entry_ns, self._entry_atr = now_ns, max(f.atr, self.point)
            atr = self._entry_atr
            if not self.exit.active:
                self.exit.start(avg_price, position, s.mom_sl_atr * atr, s.mom_tp_atr * atr)
            dec = self.exit.update(f.mid, f.atr, top, position, self.vol_step, self.vol_min)
            if dec is not None:
                self.last_reason = dec.reason
                return dec
            d = 1.0 if position > 0 else -1.0
            hold = self._max_hold_ns * (s.runner_hold_mult if self.exit.partial else 1.0)
            if now_ns - self._entry_ns >= hold:
                return self._d(0.0, Urgency.AGGRESSIVE, "durée max momentum")
            if not self.exit.partial and f.velocity * d < -s.velocity_entry_atr * atr:
                return self._d(0.0, Urgency.AGGRESSIVE, "momentum inversé")
            return None
        self._entry_ns = 0
        self.exit.reset()

        if not f.regime.momentum_allowed or f.range_hi is None or f.range_lo is None:
            return None
        atr = max(f.atr, self.point)
        tp_pts = s.mom_tp_atr * atr / self.point
        if tp_pts < s.min_tp_spread_mult * f.spread_pts:
            self.last_reason = "TP trop petit vs spread"
            return None
        buf = s.breakout_buffer_atr * atr
        v_min = s.velocity_entry_atr * atr
        stop = s.mom_sl_atr * atr / self.point
        direction = 0
        if f.mid > f.range_hi + buf and f.velocity >= v_min:
            direction = 1
        elif f.mid < f.range_lo - buf and f.velocity <= -v_min:
            direction = -1
        if direction == 0:
            return None
        ok, why = f.trend.allows_momentum(direction)
        if not ok:
            self.last_reason = f"cassure ignorée : {why}"
            return None
        label = "cassure haussière" if direction > 0 else "cassure baissière"
        return self._d(direction * self.qty, Urgency.AGGRESSIVE, label, stop=stop)
