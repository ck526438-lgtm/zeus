"""Stratégie 2 — Micro mean-reversion sur extension rapide (marché calme).

Entrée : le mid s'écarte de l'ancre (VWAP intrajournalier ou EMA courte) d'au
moins ``mr_entry_dev_atr`` × ATR M1 ET l'extension ralentit (vitesse <=
``mr_stall_velocity_atr`` × ATR). On joue le retour à l'ancre avec un ordre
limite (maker : on ne paie pas le spread à l'entrée).

Garde-fous (l'or peut transformer une « extension » en tendance de 200+ pips) :
- active seulement en régime CALME : sessions dédiées, ATR <= mr_max_atr_points,
  volatilité <= mr_max_vol_ratio × normale ;
- dès que la volatilité dépasse le seuil, un choc ou une news apparaît pendant
  qu'on est en position : sortie immédiate au marché ;
- SL strict (mr_sl_atr × ATR de l'entrée) et durée max.
"""
from __future__ import annotations

from zeus_hft.core.orderbook import BookTop
from zeus_hft.core.types import Decision, Urgency
from zeus_hft.strategies.exits import ExitManager
from zeus_hft.strategies.features import MarketFeatures


class MeanReversion:
    name = "meanrev"

    def __init__(self, s, point: float) -> None:  # type: ignore[no-untyped-def]
        self.s = s
        self.point = point
        self.qty = s.order_qty
        self._max_hold_ns = int(s.mr_max_hold_s * 1e9)
        self.exit = ExitManager(s, point, self.name)
        self.vol_step = s.volume_step
        self.vol_min = s.volume_min
        self.reset()

    def reset(self) -> None:
        self._entry_ns = 0
        self._entry_atr = 0.0
        self._entry_anchor = 0.0
        self.last_reason = ""
        self.exit.reset()

    def _d(self, target: float, urg: Urgency, reason: str, price: float | None = None,
           stop: float | None = None) -> Decision:
        self.last_reason = reason
        return Decision(target, urg, price, reason, 0.0, stop, self.name)

    def anchor(self, f: MarketFeatures) -> float:
        return f.vwap if self.s.mr_anchor == "vwap" else f.ema

    def evaluate(self, f: MarketFeatures, top: BookTop, position: float, avg_price: float,
                 now_ns: int) -> Decision | None:
        s = self.s
        atr_now = max(f.atr, self.point)
        dev = (f.mid - self.anchor(f)) / atr_now          # écart à l'ancre en ATR
        if position != 0.0:
            if self._entry_ns == 0:
                self._entry_ns, self._entry_atr = now_ns, atr_now
                self._entry_anchor = self.anchor(f)
            d = 1.0 if position > 0 else -1.0
            if not self.exit.active:
                # 1er objectif : le retour vers l'ancre (moins la marge mr_exit_dev_atr)
                tp1 = abs(self._entry_anchor - avg_price) - s.mr_exit_dev_atr * self._entry_atr
                self.exit.start(avg_price, position, s.mr_sl_atr * self._entry_atr, tp1)
            if f.regime.label in ("CHOC", "NEWS", "ROLLOVER") or f.vol_ratio > s.mr_max_vol_ratio * 1.3:
                return self._d(0.0, Urgency.AGGRESSIVE, "volatilité en hausse : sortie")
            dec = self.exit.update(f.mid, f.atr, top, position, self.vol_step, self.vol_min)
            if dec is not None:
                self.last_reason = dec.reason
                return dec
            hold = self._max_hold_ns * (s.runner_hold_mult if self.exit.partial else 1.0)
            if now_ns - self._entry_ns >= hold:
                return self._d(0.0, Urgency.AGGRESSIVE, "durée max mean-reversion")
            return None
        self._entry_ns = 0
        self.exit.reset()

        if not f.regime.meanrev_allowed:
            return None
        if abs(dev) < s.mr_entry_dev_atr:
            return None
        if abs(f.velocity) > s.mr_stall_velocity_atr * atr_now:
            self.last_reason = "extension encore rapide"
            return None
        target_pts = (abs(dev) - s.mr_exit_dev_atr) * atr_now / self.point
        if target_pts < s.min_tp_spread_mult * f.spread_pts:
            self.last_reason = "gain visé trop petit vs spread"
            return None
        stop = s.mr_sl_atr * atr_now / self.point
        ok, why = f.trend.allows_meanrev(-1 if dev > 0 else 1)
        if not ok:
            self.last_reason = f"mean-reversion ignorée : {why}"
            return None
        if dev > 0:
            return self._d(-self.qty, Urgency.PASSIVE, "extension haussière : vente", top.ask, stop)
        return self._d(self.qty, Urgency.PASSIVE, "extension baissière : achat", top.bid, stop)
