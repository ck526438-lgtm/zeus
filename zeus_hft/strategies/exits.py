"""Gestion active des sorties, commune aux stratégies momentum et mean-reversion.

Pour chaque position :
1. **Stop initial** = distance de la stratégie (multiple d'ATR) : c'est le risque 1R.
2. **Break-even** : à +``breakeven_at_r`` R, le stop passe au prix d'entrée + frais
   (commission + 1 point) : le trade ne peut plus coûter d'argent.
3. **Premier objectif (TP1)** : clôture de ``partial_close_frac`` (50 %) de la
   position ; si la taille ne permet pas de la couper (lot minimum), clôture totale.
4. **Reliquat** : trailing stop à ``trail_atr`` × ATR M1 du meilleur prix atteint.

Le stop est vérifié à chaque tick par le bot. Chaque fois qu'il se resserre, il est
aussi reporté côté serveur MT5 (SL de secours), pour que la position reste protégée
même si le bot ou le PC s'arrête.
"""
from __future__ import annotations

import math

from zeus_hft.core.orderbook import BookTop
from zeus_hft.core.types import Decision, Urgency


class ExitManager:
    def __init__(self, s, point: float, owner: str) -> None:  # type: ignore[no-untyped-def]
        self.s = s
        self.point = point
        self.owner = owner
        self.reset()

    def reset(self) -> None:
        self.active = False
        self.dir = 0.0
        self.entry = 0.0
        self.R = 0.0
        self.stop = 0.0
        self.tp1 = 0.0
        self.best = 0.0
        self.partial = False
        self.be = False
        self.stop_reason = "SL"
        self.changed = False

    def start(self, entry: float, position: float, stop_dist: float, tp1_dist: float) -> None:
        d = 1.0 if position > 0 else -1.0
        self.active = True
        self.dir = d
        self.entry = entry
        self.R = max(stop_dist, self.point)
        self.stop = entry - d * self.R
        self.tp1 = entry + d * max(tp1_dist, self.point)
        self.best = entry
        self.partial = False
        self.be = False
        self.stop_reason = f"SL {self.owner}"
        self.changed = True

    def costs_price(self) -> float:
        """Commission aller-retour exprimée en prix, + 1 point de marge."""
        s = self.s
        per_price_unit = s.tick_value / s.tick_size          # devise par lot pour 1.0 de prix
        return s.commission_per_lot / per_price_unit + self.point

    def _tighten(self, new_stop: float, reason: str) -> None:
        if (new_stop - self.stop) * self.dir > 0:
            self.stop = new_stop
            self.stop_reason = reason
            self.changed = True

    def update(self, mid: float, atr: float, top: BookTop, position: float, vol_step: float,
               vol_min: float) -> Decision | None:
        s, d = self.s, self.dir
        exit_px = top.bid if d > 0 else top.ask
        self.best = max(self.best, mid) if d > 0 else min(self.best, mid)

        if (exit_px - self.stop) * d <= 0:                        # stop touché
            return Decision(0.0, Urgency.AGGRESSIVE, None, self.stop_reason, 0.0, None, self.owner)

        pnl = (mid - self.entry) * d
        if s.breakeven_at_r > 0 and not self.be and pnl >= s.breakeven_at_r * self.R:
            self.be = True
            self._tighten(self.entry + d * self.costs_price(), "break-even")

        if not self.partial and (mid - self.tp1) * d >= 0:
            self.partial = True
            if not self.be:                                       # au TP1, au minimum le break-even
                self.be = True
                self._tighten(self.entry + d * self.costs_price(), "break-even")
            size = abs(position)
            part = math.floor(size * s.partial_close_frac / vol_step + 1e-9) * vol_step
            if s.partial_close_frac > 0 and part >= vol_min - 1e-9 and size - part >= vol_min - 1e-9:
                target = round(position - d * part, 8)
                pct = int(round(part / size * 100))
                return Decision(target, Urgency.AGGRESSIVE, None,
                                f"TP1 : clôture partielle {pct} %", 0.0, None, self.owner)
            return Decision(0.0, Urgency.AGGRESSIVE, None, f"TP {self.owner}", 0.0, None,
                            self.owner)

        if self.partial and atr > 0:
            self._tighten(self.best - d * s.trail_atr * atr, "trailing stop")
        return None

    def take_changed_stop(self) -> float | None:
        if self.active and self.changed:
            self.changed = False
            return self.stop
        return None
