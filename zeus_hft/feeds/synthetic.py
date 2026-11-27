"""Générateur de marché synthétique avec DOM (backtest et démo sans MT5).

Une « pression » latente AR(1) influence à la fois le déséquilibre des
quantités du carnet et la dérive des prix suivants : le signal OBI y a donc un
pouvoir prédictif faible mais réel, comme sur un vrai marché. Ce n'est PAS une
preuve de rentabilité : seul l'historique réel (ticks MT5) l'est.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from typing import Iterator

from zeus_hft.core.types import Tick
from zeus_hft.feeds.base import MarketUpdate


def synthetic_updates(
    n: int,
    *,
    symbol: str = "XAUUSD",
    start_price: float = 2650.0,
    point: float = 0.01,
    levels: int = 5,
    dt_ms: int = 400,
    start_ts_ms: int = 1_767_571_200_000,   # lundi 2026-01-05 00:00 heure serveur (dim. 22:00 UTC)
    seed: int = 7,
    predictiveness: float = 0.6,
    regimes: bool = True,
) -> Iterator[MarketUpdate]:
    """Marché synthétique avec régimes : CALME (retour à la moyenne), NORMAL, POUSSÉE
    (tendance brève et rapide, comme une cassure sur l'or)."""
    rng = random.Random(seed)
    mid = start_price
    anchor = start_price
    pressure = 0.0
    ts = start_ts_ms
    regime, left, drift_dir = "normal", 3000, 0.0
    for _ in range(n):
        if regimes:
            left -= 1
            if left <= 0:
                regime = rng.choices(["calme", "normal", "poussee"], [0.4, 0.4, 0.2])[0]
                left = (int(rng.uniform(30, 120)) if regime == "poussee"
                        else int(rng.expovariate(1 / 1500)) + 200)
                drift_dir = rng.choice([-1.0, 1.0])
                anchor = mid
        vol = {"calme": 1.5, "normal": 3.0, "poussee": 4.0}[regime] if regimes else 3.0
        pressure = 0.97 * pressure + rng.gauss(0.0, 0.25)
        p = math.tanh(pressure)
        drift = predictiveness * p * 2.0 * point
        if regime == "poussee":
            drift += drift_dir * 8.0 * point
        elif regime == "calme":
            drift += 0.02 * (anchor - mid)
        mid += drift + rng.gauss(0.0, vol * point)
        half_spread = max(1, round(abs(rng.gauss(8, 3)))) * point
        bid = round((mid - half_spread) / point) * point
        ask = round((mid + half_spread) / point) * point
        if ask <= bid:
            ask = bid + point
        bids, asks = [], []
        for i in range(levels):
            base = rng.uniform(0.5, 3.0) * (1.0 + 0.3 * i)
            bids.append((round(bid - i * 5 * point, 2), round(base * (1.0 + 0.8 * p), 3)))
            asks.append((round(ask + i * 5 * point, 2), round(base * (1.0 - 0.8 * p), 3)))
        ts += max(1, int(rng.expovariate(1.0 / dt_ms)))
        tick = Tick(symbol, bid, ask, bid_qty=bids[0][1], ask_qty=asks[0][1],
                    exchange_ts_ms=ts, recv_ts_ns=ts * 1_000_000)
        yield MarketUpdate(tick, bids, asks)


class SyntheticFeed:
    """Rejoue le générateur en temps réel (DRY_RUN sans MT5, interface de démo)."""

    def __init__(self, symbol: str = "XAUUSD", speed: float = 1.0, seed: int | None = None) -> None:
        self.symbol = symbol
        self.speed = speed
        self.seed = seed if seed is not None else int(time.time())
        self.connected = True

    async def run(self, out: asyncio.Queue[MarketUpdate], stop: asyncio.Event) -> None:
        last_ts: int | None = None
        for upd in synthetic_updates(10**9, symbol=self.symbol, seed=self.seed):
            if stop.is_set():
                return
            if last_ts is not None:
                await asyncio.sleep((upd.tick.exchange_ts_ms - last_ts) / 1e3 / self.speed)
            last_ts = upd.tick.exchange_ts_ms
            upd.tick.recv_ts_ns = time.perf_counter_ns()
            if out.full():
                out.get_nowait()
            out.put_nowait(upd)
