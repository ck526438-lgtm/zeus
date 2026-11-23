"""Exchange simulé : latence, slippage, frais maker/taker, fills partiels.

Utilisé à la fois par le paper trading (DRY_RUN, horloge réelle) et par le
backtest (horloge simulée) : le même modèle d'exécution des deux côtés.

Modèle (volontairement conservateur) :
- Chaque action (envoi, annulation, modification) n'atteint l'exchange
  qu'après ``latency_ns``.
- Ordre au marché : exécuté au premier tick après la latence, au prix opposé
  ± ``slippage_points``, frais taker.
- Ordre limite marketable à l'arrivée (achat >= ask) : exécuté au ask, taker.
- Ordre limite au carnet : exécuté seulement si le marché le TRAVERSE
  (achat : ask <= prix limite), au prix limite, frais maker. Être simplement
  au meilleur prix ne suffit pas (file d'attente inconnue). Avec ``touch_fill``,
  un toucher (bid == prix) exécute une fraction ``touch_fill`` de la quantité.
- Si ``max_fill_qty`` > 0, chaque tick exécute au plus cette quantité
  (fills partiels).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

from zeus_hft.core.types import (
    EventKind,
    EventSink,
    Order,
    OrderEvent,
    OrderType,
    Side,
)


class SimExchange:
    def __init__(
        self,
        *,
        point: float,
        contract_size: float,
        latency_ms: float = 40.0,
        slippage_points: float = 1.0,
        maker_fee_bps: float = 0.0,
        taker_fee_bps: float = 0.0,
        commission_per_lot: float = 0.0,
        touch_fill: float = 0.0,
        max_fill_qty: float = 0.0,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.point = point
        self.contract_size = contract_size
        self.latency_ns = int(latency_ms * 1e6)
        self.slippage = slippage_points * point
        self.maker_fee = maker_fee_bps / 1e4
        self.taker_fee = taker_fee_bps / 1e4
        self.commission_per_lot = commission_per_lot
        self.touch_fill = touch_fill
        self.max_fill_qty = max_fill_qty
        self._clock = clock_ns
        self._sink: EventSink | None = None
        self._inbox: deque[tuple[int, str, Order, float, float]] = deque()
        self._resting: dict[str, Order] = {}
        self._remaining: dict[str, float] = {}
        self._next_ticket = 1
        self.bid = 0.0
        self.ask = 0.0

    def set_event_sink(self, sink: EventSink) -> None:
        self._sink = sink

    def _emit(self, ev: OrderEvent) -> None:
        if self._sink is not None:
            self._sink(ev)

    # --- Interface ExecutionGateway ------------------------------------ #
    async def submit(self, order: Order) -> None:
        self._inbox.append((self._clock() + self.latency_ns, "new", order, 0.0, 0.0))

    async def cancel(self, order: Order) -> None:
        self._inbox.append((self._clock() + self.latency_ns, "cancel", order, 0.0, 0.0))

    async def replace(self, order: Order, new_price: float, new_qty: float) -> None:
        self._inbox.append((self._clock() + self.latency_ns, "replace", order, new_price, new_qty))

    # --- Moteur ---------------------------------------------------------- #
    def _fee(self, qty: float, price: float, maker: bool) -> float:
        notional = qty * self.contract_size * price
        return notional * (self.maker_fee if maker else self.taker_fee) + qty * self.commission_per_lot

    def _fill(self, order: Order, qty: float, price: float, maker: bool) -> None:
        cid = order.client_order_id
        rem = self._remaining.get(cid, order.qty)
        if self.max_fill_qty > 0:
            qty = min(qty, self.max_fill_qty)
        qty = min(qty, rem)
        if qty <= 0:
            return
        rem = round(rem - qty, 10)
        self._remaining[cid] = rem
        if rem <= 1e-12:
            self._resting.pop(cid, None)
            self._remaining.pop(cid, None)
        self._emit(OrderEvent(EventKind.FILL, cid, order.exchange_order_id, fill_qty=qty,
                              fill_price=price, fee=self._fee(qty, price, maker), is_maker=maker,
                              ts_ns=self._clock()))

    def on_market(self, bid: float, ask: float) -> None:
        """À appeler à chaque tick, AVANT la décision de stratégie."""
        self.bid, self.ask = bid, ask
        now = self._clock()
        inbox = self._inbox
        while inbox and inbox[0][0] <= now:
            _, action, order, px, qty = inbox.popleft()
            cid = order.client_order_id
            if action == "new":
                order_ticket = self._next_ticket
                self._next_ticket += 1
                self._remaining[cid] = order.qty
                self._emit(OrderEvent(EventKind.ACK, cid, order_ticket, ts_ns=now))
                if order.order_type is OrderType.MARKET:
                    p = ask + self.slippage if order.side is Side.BUY else bid - self.slippage
                    self._fill(order, order.qty, p, maker=False)
                    while cid in self._remaining:          # le marché exécute tout
                        self._fill(order, self._remaining[cid], p, maker=False)
                else:
                    lp = order.price or 0.0
                    marketable = (lp >= ask) if order.side is Side.BUY else (lp <= bid)
                    if marketable:
                        self._fill(order, order.qty, ask if order.side is Side.BUY else bid, False)
                    if cid in self._remaining:
                        self._resting[cid] = order
            elif action == "cancel":
                if cid in self._resting:
                    del self._resting[cid]
                    self._remaining.pop(cid, None)
                    self._emit(OrderEvent(EventKind.CANCELED, cid, ts_ns=now))
                else:
                    self._emit(OrderEvent(EventKind.REJECTED, cid, reason="ordre inconnu ou exécuté",
                                          ts_ns=now))
            elif action == "replace":
                if cid in self._resting:
                    order.price = px
                    self._emit(OrderEvent(EventKind.REPLACED, cid, new_price=px, new_qty=qty, ts_ns=now))
                else:
                    self._emit(OrderEvent(EventKind.REJECTED, cid, reason="ordre inconnu ou exécuté",
                                          ts_ns=now))

        for cid, order in list(self._resting.items()):
            lp = order.price or 0.0
            rem = self._remaining.get(cid, 0.0)
            if order.side is Side.BUY:
                if ask <= lp:
                    self._fill(order, rem, lp, maker=True)
                elif self.touch_fill > 0 and bid >= lp:
                    self._fill(order, rem * self.touch_fill, lp, maker=True)
            else:
                if bid >= lp:
                    self._fill(order, rem, lp, maker=True)
                elif self.touch_fill > 0 and ask <= lp:
                    self._fill(order, rem * self.touch_fill, lp, maker=True)

    @property
    def resting_count(self) -> int:
        return len(self._resting)
