"""Gestion des deux ordres de cotation (bid et ask) : placement, cancel/replace, fills, PnL.

- Un seul ordre actif par côté ; une seule action en vol par côté (pas de doublon).
- On ne bouge un ordre que si le nouveau prix diffère d'au moins ``requote_ticks`` :
  chaque annulation fait perdre la place dans la file d'attente.
- Ordres « purs » : un ordre ferme OU ouvre, jamais les deux (compatible avec le mode
  hedge de MEXC). Si l'inventaire change de signe, les ordres dont l'intention ne
  correspond plus sont annulés puis replacés.
- ``client id`` déterministe (``externalOid``) : <préfixe><session>-<n>.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from zeus_hft.mm.config import MMSettings
from zeus_hft.mm.quoting import Quote
from zeus_hft.risk.risk_manager import RiskManager

log = logging.getLogger("zeus_hft.mm")
BUY, SELL = 1, -1


@dataclass(slots=True)
class MMOrder:
    cid: str
    side: int
    price: float
    qty: float
    closing: bool
    status: str = "pending"          # pending -> live -> (filled | canceled | rejected) ; canceling
    filled: float = 0.0
    sent_ms: int = 0
    updated_ms: int = 0


@dataclass(slots=True)
class Fill:
    ts: int
    side: int
    qty: float
    price: float
    fee: float


@dataclass
class MMStats:
    fills: list[Fill] = field(default_factory=list)
    orders_sent: int = 0
    cancels: int = 0
    rejects: int = 0
    quoted_ms: int = 0
    total_ms: int = 0


class QuoteManager:
    def __init__(self, s: MMSettings, gateway: Any, risk: RiskManager,
                 clock_ms: Callable[[], int], session: int = 0, on_fill: Callable[[Fill], None] | None = None) -> None:
        self.s = s
        self.gw = gateway
        self.risk = risk
        self._clock = clock_ms
        self._root = f"ZM{session:x}-"
        self._seq = 0
        self.orders: dict[str, MMOrder] = {}
        self.side_order: dict[int, str | None] = {BUY: None, SELL: None}
        self.inventory = 0.0
        self.avg_price = 0.0
        self.realized = 0.0
        self.fees = 0.0
        self.volume = 0.0
        self.stats = MMStats()
        self._on_fill = on_fill

    # --- comptabilité ----------------------------------------------------- #
    def unrealized(self, mark: float) -> float:
        return self.inventory * (mark - self.avg_price) * self.s.contract_size if self.inventory else 0.0

    def pnl(self, mark: float) -> float:
        return self.realized + self.unrealized(mark) - self.fees

    def _apply_fill(self, side: int, qty: float, price: float) -> None:
        inv, cs = self.inventory, self.s.contract_size
        signed = side * qty
        if inv == 0 or (inv > 0) == (signed > 0):
            new = inv + signed
            self.avg_price = (self.avg_price * abs(inv) + price * qty) / abs(new)
            self.inventory = round(new, 10)
            return
        closing = min(qty, abs(inv))
        self.realized += closing * (price - self.avg_price) * cs * (1 if inv > 0 else -1)
        new = round(inv + signed, 10)
        if abs(new) < 1e-12:
            self.inventory, self.avg_price = 0.0, 0.0
        elif (new > 0) != (inv > 0):
            self.inventory, self.avg_price = new, price
        else:
            self.inventory = new

    # --- événements de la passerelle -------------------------------------- #
    def on_event(self, kind: str, cid: str, qty: float = 0.0, price: float = 0.0, fee: float = 0.0) -> None:
        o = self.orders.get(cid)
        if o is None:
            return
        o.updated_ms = self._clock()
        if kind == "ack":
            self.risk.record_execution_ok()
            if o.status == "pending":
                o.status = "live"
        elif kind == "fill":
            qty = min(qty, o.qty - o.filled)
            if qty <= 0:
                return
            o.filled += qty
            self.fees += fee
            self.volume += qty * self.s.contract_size * price
            self._apply_fill(o.side, qty, price)
            f = Fill(self._clock(), o.side, qty, price, fee)
            self.stats.fills.append(f)
            if self._on_fill is not None:
                self._on_fill(f)
            if o.qty - o.filled <= 1e-12:
                self._close(o, "filled")
        elif kind == "canceled":
            self._close(o, "canceled")
        elif kind == "cancel_rejected":
            if o.status == "canceling":
                o.status = "live"            # sera clos par son fill
        elif kind == "rejected":
            self.stats.rejects += 1
            self.risk.record_execution_error("post-only/rejet")
            self._close(o, "rejected")

    def _close(self, o: MMOrder, status: str) -> None:
        o.status = status
        self.orders.pop(o.cid, None)
        if self.side_order.get(o.side) == o.cid:
            self.side_order[o.side] = None

    # --- cotation ---------------------------------------------------------- #
    def _next_cid(self) -> str:
        self._seq += 1
        return f"{self._root}{self._seq}"

    async def update(self, q: Quote) -> int:
        actions = 0
        now = self._clock()
        for side, price, size in ((BUY, q.bid, q.bid_size), (SELL, q.ask, q.ask_size)):
            cid = self.side_order[side]
            cur = self.orders.get(cid) if cid else None
            closing = (self.inventory < 0) if side == BUY else (self.inventory > 0)
            if closing and price is not None:
                size = min(size, abs(self.inventory))      # un ordre de fermeture ne retourne pas
            if cur is not None and cur.status in ("pending", "canceling"):
                continue                                    # une seule action en vol par côté
            if price is None or size <= 0:
                if cur is not None:
                    actions += await self._cancel(cur)
                continue
            if cur is None:
                actions += await self._place(side, price, size, closing, now)
                continue
            stale = cur.closing != closing
            moved = abs(cur.price - price) >= self.s.requote_ticks * self.s.tick - 1e-12
            resized = abs((cur.qty - cur.filled) - size) >= self.s.vol_step - 1e-12 and not closing
            if (stale or moved or resized) and now - cur.updated_ms >= self.s.min_requote_ms:
                actions += await self._cancel(cur)          # cancel puis new au tick suivant
        return actions

    async def _place(self, side: int, price: float, size: float, closing: bool, now: int) -> int:
        verdict = (self.risk.check_action() if closing
                   else self.risk.check_order(side * size, self.inventory, 0.0))
        if not verdict.allowed:
            return 0
        cid = self._next_cid()
        o = MMOrder(cid, side, price, size, closing, sent_ms=now, updated_ms=now)
        self.orders[cid] = o
        self.side_order[side] = cid
        self.stats.orders_sent += 1
        try:
            await self.gw.place(cid, side, price, size, closing)
        except Exception as exc:
            log.warning("ordre %s refusé : %s", cid, exc)
            self.on_event("rejected", cid)
        return 1

    async def _cancel(self, o: MMOrder) -> int:
        if not self.risk.check_action().allowed:
            return 0
        o.status = "canceling"
        o.updated_ms = self._clock()
        self.stats.cancels += 1
        try:
            await self.gw.cancel(o.cid)
        except Exception as exc:
            log.warning("annulation %s échouée : %s", o.cid, exc)
            o.status = "live"
        return 1

    async def cancel_all(self) -> None:
        try:
            await self.gw.cancel_all()
        finally:
            for o in list(self.orders.values()):
                o.status = "canceling"
