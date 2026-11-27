"""Moteur de market making : événements de marché -> carnet -> cotation -> ordres.

Événements normalisés (identiques en direct, à l'enregistrement et au backtest) :
    ("snap",  ts_ms, bids, asks, version)
    ("depth", ts_ms, bids, asks, version)      # quantités absolues, 0 = niveau supprimé
    ("trade", ts_ms, price, qty, side)         # side = +1 acheteur agressif, −1 vendeur
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from zeus_hft.core.orderbook import DepthDelta, OrderBook, OrderBookError
from zeus_hft.mm.config import MMSettings
from zeus_hft.mm.quoting import Quote, QuoteEngine
from zeus_hft.mm.quotes import BUY, Fill, QuoteManager
from zeus_hft.risk.risk_manager import RiskManager

log = logging.getLogger("zeus_hft.mm")
FLAT = Quote(None, None, 0.0, 0.0, 0.0, 0.0, "retrait")


class MMEngine:
    def __init__(self, s: MMSettings, gateway: Any, risk: RiskManager, clock_ms: Callable[[], int],
                 sim: Any = None, session: int = 0, recorder: Any = None) -> None:
        self.s = s
        self.gw = gateway
        self.risk = risk
        self._clock = clock_ms
        self.sim = sim
        self.recorder = recorder
        self.book = OrderBook(s.mexc_symbol(), max_depth=500)
        self.qe = QuoteEngine(s)
        self.qm = QuoteManager(s, gateway, risk, clock_ms, session, on_fill=self._on_fill)
        if hasattr(gateway, "on_event"):
            gateway.on_event = self.qm.on_event
        if sim is not None:
            sim.set_level_fn(self._level)
        self.need_resync = False
        self.last_mid: float | None = None
        self.last_reason = ""
        self.events = 0
        self._last_ts = 0
        self.peak_equity = s.starting_equity
        self.max_dd = 0.0
        self.max_abs_inventory = 0.0
        self.quoted_both_ms = 0
        self.total_ms = 0

    def _level(self, side: int, price: float) -> float:
        d = self.book._bids if side == BUY else self.book._asks
        return float(d.get(price, 0.0))

    def _on_fill(self, f: Fill) -> None:
        self.qe.markout.on_fill(f.ts, f.price, f.side)
        self.max_abs_inventory = max(self.max_abs_inventory, abs(self.qm.inventory))

    async def on_event(self, ev: tuple) -> None:
        kind, ts = ev[0], ev[1]
        self.events += 1
        if self.recorder is not None:
            self.recorder.put(ev)
        if self._last_ts:
            dt = max(ts - self._last_ts, 0)
            self.total_ms += dt
            if self.qm.side_order[1] and self.qm.side_order[-1]:
                self.quoted_both_ms += dt
        self._last_ts = ts
        try:
            if kind == "snap":
                self.book.apply_snapshot(ev[2], ev[3], ev[4], ts, ts * 1_000_000)
                self.need_resync = False
            elif kind == "depth":
                self.book.apply_delta(DepthDelta(ev[4], ev[4], tuple(ev[2]), tuple(ev[3]), ts, ts * 1_000_000))
            elif kind == "trade":
                price, qty, side = ev[2], ev[3], ev[4]
                levels = 1
                top = self.book.top()
                if top is not None and self.s.tick > 0:
                    ref = top.ask if side > 0 else top.bid
                    levels = int(abs(price - ref) / self.s.tick + 1e-9) + 1
                self.qe.on_trade(ts, price, qty, side, levels)
                if self.sim is not None:
                    self.sim.on_trade(price, qty, side)
        except OrderBookError as exc:
            log.warning("carnet invalide (%s) : retrait des ordres et resynchronisation", exc)
            self.need_resync = True

        top = self.book.top() if self.book.is_ready else None
        if self.sim is not None and top is not None:
            self.sim.on_book(top.bid, top.ask)
        if top is None:
            if self.qm.orders:
                await self.qm.update(FLAT)
            return
        self.last_mid = top.mid
        eq = self.s.starting_equity + self.qm.pnl(top.mid)
        self.risk.update_equity(eq)
        self.peak_equity = max(self.peak_equity, eq)
        self.max_dd = max(self.max_dd, self.peak_equity - eq)
        if self.risk.killed or self.risk.halts:
            self.last_reason = self.risk.kill_reason or "; ".join(self.risk.halts.values())
            await self.qm.update(FLAT)
            return
        q = self.qe.compute(ts, top.bid, top.ask, top.microprice, self.qm.inventory)
        self.last_reason = q.reason
        await self.qm.update(q)

    def snapshot(self) -> dict[str, Any]:
        mid = self.last_mid
        return {"mid": mid, "inventory": self.qm.inventory, "pnl": round(self.qm.pnl(mid or 0), 4),
                "realized": round(self.qm.realized, 4), "fees": round(self.qm.fees, 4),
                "fills": len(self.qm.stats.fills), "orders": self.qm.stats.orders_sent,
                "cancels": self.qm.stats.cancels, "rejects": self.qm.stats.rejects,
                "markout_bps": round(self.qe.markout.mean_bps, 2),
                "uptime": round(self.quoted_both_ms / self.total_ms, 3) if self.total_ms else 0.0,
                "reason": self.last_reason, "risk": self.risk.status()}
