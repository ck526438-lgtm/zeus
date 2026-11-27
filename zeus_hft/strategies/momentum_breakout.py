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
from zeus_hft.strategies.features import MarketFeatures


class MomentumBreakout:
    name = "momentum"

    def __init__(self, s, point: float) -> None:  # type: ignore[no-untyped-def]
        self.s = s
        self.point = point
        self.qty = s.order_qty
        self._max_hold_ns = int(s.mom_max_hold_s * 1e9)
        self.reset()

    def reset(self) -> None:
        self._entry_ns = 0
        self._entry_atr = 0.0
        self.last_reason = ""

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
            d = 1.0 if position > 0 else -1.0
            atr = self._entry_atr
            pnl = (f.mid - avg_price) * d
            if pnl <= -s.mom_sl_atr * atr:
                return self._d(0.0, Urgency.AGGRESSIVE, "SL momentum")
            if now_ns - self._entry_ns >= self._max_hold_ns:
                return self._d(0.0, Urgency.AGGRESSIVE, "durée max momentum")
            if f.velocity * d < -s.velocity_entry_atr * atr:
                return self._d(0.0, Urgency.AGGRESSIVE, "momentum inversé")
            if pnl >= s.mom_tp_atr * atr:
                return self._d(0.0, Urgency.PASSIVE, "TP momentum",
                               price=top.ask if d > 0 else top.bid)
            return None
        self._entry_ns = 0

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
        if f.mid > f.range_hi + buf and f.velocity >= v_min:
            return self._d(self.qty, Urgency.AGGRESSIVE, "cassure haussière", stop=stop)
        if f.mid < f.range_lo - buf and f.velocity <= -v_min:
            return self._d(-self.qty, Urgency.AGGRESSIVE, "cassure baissière", stop=stop)
        return None
