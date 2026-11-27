"""Moteur de cotation : où placer le bid et l'ask, ou se retirer.

    prix juste = microprice + flow_alpha × flux × demi-spread
    décalage   = −(inventaire / inventaire max) × skew_ticks × tick
    demi-spread = max(min_half_spread_ticks × tick, vol_mult × σ(prix sur vol_horizon_s))
    bid = prix juste + décalage − demi-spread    (arrondi au tick inférieur)
    ask = prix juste + décalage + demi-spread    (arrondi au tick supérieur)

Garde-fous :
- post-only : bid < meilleur ask et ask > meilleur bid, sinon l'ordre serait taker ;
- inventaire au plafond -> on ne cote plus le côté qui l'augmente ;
- flux toxique (déséquilibre > toxic_flow, ou balayage de plusieurs niveaux) -> retrait
  du côté menacé pendant pull_ms ;
- sélection adverse mesurée (markout moyen trop négatif) -> retrait total.
Tout est O(1) par événement, sans indicateur technique : uniquement carnet et trades.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from zeus_hft.mm.config import MMSettings


@dataclass(slots=True)
class Quote:
    bid: float | None
    ask: float | None
    bid_size: float
    ask_size: float
    fair: float
    half_spread: float
    reason: str = ""


class FlowTracker:
    """Déséquilibre acheteurs/vendeurs agressifs sur une fenêtre glissante + balayages."""

    def __init__(self, window_ms: int) -> None:
        self._q: deque[tuple[int, float]] = deque()      # (ts, volume signé)
        self._win = window_ms
        self.buy = 0.0
        self.sell = 0.0
        self.last_sweep_ts = 0
        self.last_sweep_side = 0

    def add(self, ts: int, qty: float, side: int) -> None:
        self._q.append((ts, qty * side))
        if side > 0:
            self.buy += qty
        else:
            self.sell += qty
        self._expire(ts)

    def _expire(self, now: int) -> None:
        q = self._q
        while q and q[0][0] < now - self._win:
            _, v = q.popleft()
            if v > 0:
                self.buy -= v
            else:
                self.sell += v
        self.buy = max(self.buy, 0.0)
        self.sell = max(self.sell, 0.0)

    def imbalance(self, now: int) -> float:
        self._expire(now)
        tot = self.buy + self.sell
        return 0.0 if tot <= 0 else (self.buy - self.sell) / tot


class VolTracker:
    """Volatilité EWMA des variations du mid, ramenée en prix sur un horizon (√t)."""

    def __init__(self, halflife_s: float = 30.0) -> None:
        self._var_per_s = 0.0
        self._last_mid = 0.0
        self._last_ts = 0
        self._hl = halflife_s

    def update(self, ts: int, mid: float) -> None:
        if self._last_ts and ts > self._last_ts and self._last_mid > 0:
            dt = (ts - self._last_ts) / 1000.0
            r2 = (mid - self._last_mid) ** 2 / max(dt, 1e-3)
            a = 1.0 - 0.5 ** (dt / self._hl)
            self._var_per_s += a * (r2 - self._var_per_s)
        self._last_mid, self._last_ts = mid, ts

    def sigma(self, horizon_s: float) -> float:
        return math.sqrt(max(self._var_per_s, 0.0) * horizon_s)


class Markout:
    """Sélection adverse : où est le mid ``horizon`` après chacun de nos fills ?
    markout (bps) = (mid_après − prix_fill) × côté / prix. Négatif = on se fait « sniper »."""

    def __init__(self, horizon_ms: int, window: int) -> None:
        self._pending: deque[tuple[int, float, int]] = deque()     # (échéance, prix, côté)
        self.values: deque[float] = deque(maxlen=window)
        self._h = horizon_ms

    def on_fill(self, ts: int, price: float, side: int) -> None:
        self._pending.append((ts + self._h, price, side))

    def on_mid(self, ts: int, mid: float) -> None:
        p = self._pending
        while p and p[0][0] <= ts:
            _, price, side = p.popleft()
            self.values.append((mid - price) * side / price * 1e4)

    @property
    def mean_bps(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0.0


def floor_tick(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 10)


def ceil_tick(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 10)


class QuoteEngine:
    def __init__(self, s: MMSettings) -> None:
        self.s = s
        self.flow = FlowTracker(int(s.flow_window_s * 1000))
        self.vol = VolTracker()
        self.markout = Markout(int(s.markout_s * 1000), s.markout_window)
        self._pull_bid_until = 0
        self._pull_ask_until = 0
        self.last: Quote | None = None

    def on_trade(self, ts: int, price: float, qty: float, side: int, levels_crossed: int = 1) -> None:
        self.flow.add(ts, qty, side)
        if levels_crossed >= self.s.sweep_levels:
            # un acheteur agressif qui balaie l'ask : notre ask est en danger (et inversement)
            until = ts + int(self.s.pull_ms)
            if side > 0:
                self._pull_ask_until = until
            else:
                self._pull_bid_until = until

    def compute(self, ts: int, best_bid: float, best_ask: float, microprice: float,
                inventory: float) -> Quote:
        s, t = self.s, self.s.tick
        mid = (best_bid + best_ask) * 0.5
        self.vol.update(ts, mid)
        self.markout.on_mid(ts, mid)
        imb = self.flow.imbalance(ts)
        half = max(s.min_half_spread_ticks * t, s.vol_mult * self.vol.sigma(s.vol_horizon_s))
        fair = microprice + s.flow_alpha * imb * half
        skew = -(inventory / s.max_inventory) * s.skew_ticks * t
        bid = floor_tick(fair + skew - half, t)
        ask = ceil_tick(fair + skew + half, t)
        # post-only : jamais de prix qui traverserait le spread
        bid = min(bid, best_ask - t)
        ask = max(ask, best_bid + t)
        reasons = []
        qb: float | None = bid
        qa: float | None = ask

        if len(self.markout.values) >= 10 and self.markout.mean_bps < -s.max_adverse_bps:
            self.last = Quote(None, None, 0, 0, fair, half,
                              f"sélection adverse {self.markout.mean_bps:.1f} bps : retrait")
            return self.last
        if imb > s.toxic_flow or ts < self._pull_ask_until:
            qa = None
            reasons.append("acheteurs agressifs : ask retiré")
        if imb < -s.toxic_flow or ts < self._pull_bid_until:
            qb = None
            reasons.append("vendeurs agressifs : bid retiré")
        if inventory >= s.max_inventory - 1e-9:
            qb = None
            reasons.append("inventaire max long")
        if inventory <= -s.max_inventory + 1e-9:
            qa = None
            reasons.append("inventaire max short")
        bsize = min(s.quote_size, s.max_inventory - inventory) if qb is not None else 0.0
        asize = min(s.quote_size, s.max_inventory + inventory) if qa is not None else 0.0
        if qb is not None and bsize < s.min_vol - 1e-12:
            qb, bsize = None, 0.0
        if qa is not None and asize < s.min_vol - 1e-12:
            qa, asize = None, 0.0
        self.last = Quote(qb, qa, bsize, asize, fair, half, " · ".join(reasons))
        return self.last
