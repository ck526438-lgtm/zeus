"""Cycle de vie des ordres + exécution par « position cible ».

La stratégie exprime une position cible ; l'OrderManager calcule l'écart avec
(position + ordres en cours) et agit au minimum : nouvel ordre, modification de
prix (cancel/replace natif), annulation, ou rien.

Anti-doublons :
- ``client_order_id`` déterministe : ``<prefix><session>-<seq>`` (≤ 31 car.,
  compatible avec le champ ``comment`` de MT5). Une même (session, seq) donne
  toujours le même identifiant ; un id déjà connu n'est jamais renvoyé.
- Une seule action en vol à la fois : tant qu'un ordre est PENDING_NEW /
  PENDING_CANCEL / PENDING_REPLACE, aucune nouvelle action n'est émise.
- Au plus un ordre limite actif par côté.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable

from zeus_hft.core.orderbook import BookTop
from zeus_hft.core.telemetry import LatencyTracker
from zeus_hft.core.types import (
    Decision,
    EventKind,
    ExecutionGateway,
    Order,
    OrderEvent,
    OrderStatus,
    OrderType,
    Side,
    Urgency,
)
from zeus_hft.risk.risk_manager import RiskManager

log = logging.getLogger("zeus_hft.orders")

_PENDING = frozenset({OrderStatus.PENDING_NEW, OrderStatus.PENDING_CANCEL,
                      OrderStatus.PENDING_REPLACE})
_EPS = 1e-9


def _to_base36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = chars[r] + out
        if n == 0:
            return out


class OrderManager:
    def __init__(
        self,
        symbol: str,
        gateway: ExecutionGateway,
        risk: RiskManager,
        *,
        point: float,
        volume_step: float,
        volume_min: float,
        contract_size: float,
        id_prefix: str = "ZH",
        session_id: int | None = None,
        min_requote_ms: float = 100.0,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.symbol = symbol
        self.gateway = gateway
        self.risk = risk
        self.point = point
        self.volume_step = volume_step
        self.volume_min = volume_min
        self.contract_size = contract_size
        self._clock = clock_ns
        self._min_requote_ns = int(min_requote_ms * 1e6)
        sid = session_id if session_id is not None else int(time.time())
        self._id_root = f"{id_prefix}{_to_base36(sid)}-"
        self._seq = 0
        self._known_ids: set[str] = set()

        self.orders: dict[str, Order] = {}          # ordres actifs uniquement
        self.position = 0.0
        self.avg_price = 0.0
        self.realized_pnl = 0.0
        self.fees = 0.0
        self.fill_count = 0
        self.order_count = 0
        self.tick_to_order = LatencyTracker("tick->ordre")
        gateway.set_event_sink(self.on_event)

    # ------------------------------------------------------------------ #
    # Utilitaires
    # ------------------------------------------------------------------ #
    def next_client_id(self) -> str:
        self._seq += 1
        return f"{self._id_root}{self._seq}"

    def round_qty(self, qty: float) -> float:
        step = self.volume_step
        q = math.floor(abs(qty) / step + 1e-6) * step
        q = round(q, 8)
        return math.copysign(q, qty) if q >= self.volume_min - _EPS else 0.0

    def round_price(self, price: float) -> float:
        return round(round(price / self.point) * self.point, 10)

    @property
    def working_signed(self) -> float:
        return sum(o.signed_remaining for o in self.orders.values())

    @property
    def has_inflight(self) -> bool:
        return any(o.status in _PENDING for o in self.orders.values())

    def unrealized_pnl(self, mark: float | None) -> float:
        if mark is None or self.position == 0.0:
            return 0.0
        return self.position * (mark - self.avg_price) * self.contract_size

    def net_pnl(self, mark: float | None) -> float:
        return self.realized_pnl + self.unrealized_pnl(mark) - self.fees

    def set_position(self, position: float, avg_price: float) -> None:
        """Synchronisation initiale (positions déjà ouvertes dans MT5)."""
        self.position = position
        self.avg_price = avg_price if position else 0.0

    # ------------------------------------------------------------------ #
    # Exécution d'une décision
    # ------------------------------------------------------------------ #
    async def execute(self, decision: Decision, top: BookTop, t0_ns: int,
                      data_age_ms: float = 0.0) -> int:
        """Rapproche (position + ordres) de la cible. Retourne le nb d'actions."""
        if self.has_inflight:
            return 0
        target = self.round_qty(decision.target_position)
        desired = round(target - self.position, 8)     # ce qu'il reste à exécuter
        actions = 0

        if decision.urgency is Urgency.AGGRESSIVE:
            # On va traverser le spread : on retire d'abord tout ordre limite.
            if self.orders:
                return await self.cancel_all()
            qty = self.round_qty(desired)
            if qty != 0.0:
                actions += await self._new(Side.BUY if qty > 0 else Side.SELL, abs(qty),
                                           OrderType.MARKET, None, t0_ns, data_age_ms)
            return actions

        # ---------- Passif : un seul ordre limite au bon prix ----------
        qty = self.round_qty(desired)
        if qty == 0.0:
            return await self.cancel_all()
        side = Side.BUY if qty > 0 else Side.SELL
        price = decision.limit_price
        if price is None:
            price = top.bid if side is Side.BUY else top.ask
        price = self.round_price(price)

        same: Order | None = None
        for o in list(self.orders.values()):
            if o.side is not side or o.order_type is OrderType.MARKET or same is not None:
                actions += await self._cancel(o)           # côté opposé ou doublon
            else:
                same = o
        if actions:
            return actions

        if same is None:
            return await self._new(side, abs(qty), OrderType.LIMIT, price, t0_ns, data_age_ms)

        if abs(same.remaining - abs(qty)) >= self.volume_step / 2:
            return await self._cancel(same)                # quantité à changer : cancel puis new
        if abs((same.price or 0.0) - price) >= self.point / 2:
            if self._clock() - same.updated_ns >= self._min_requote_ns:
                return await self._replace(same, price, t0_ns)
        return 0

    async def flatten(self, t0_ns: int | None = None) -> int:
        """Annule tout puis ferme la position au marché."""
        n = await self.cancel_all()
        if self.has_inflight:
            return n
        qty = self.round_qty(-self.position)
        if qty != 0.0:
            n += await self._new(Side.BUY if qty > 0 else Side.SELL, abs(qty), OrderType.MARKET,
                                 None, t0_ns or time.perf_counter_ns(), 0.0)
        return n

    async def cancel_all(self) -> int:
        n = 0
        for o in list(self.orders.values()):
            if o.status not in _PENDING:
                n += await self._cancel(o, force=True)
        return n

    # ------------------------------------------------------------------ #
    # Actions élémentaires
    # ------------------------------------------------------------------ #
    async def _new(self, side: Side, qty: float, otype: OrderType, price: float | None,
                   t0_ns: int, data_age_ms: float) -> int:
        signed = qty * int(side)
        verdict = self.risk.check_order(signed, self.position, self.working_signed, data_age_ms)
        if not verdict.allowed:
            log.debug("ordre refusé par le risque : %s %s", verdict.reason.value, verdict.detail)
            return 0
        cid = self.next_client_id()
        if cid in self._known_ids:          # ne peut arriver qu'en cas de bug : on bloque
            log.error("client_order_id dupliqué %s", cid)
            return 0
        self._known_ids.add(cid)
        order = Order(cid, self.symbol, side, qty, otype, price, tick_recv_ns=t0_ns,
                      created_ns=self._clock(), updated_ns=self._clock())
        self.orders[cid] = order
        self.order_count += 1
        self.tick_to_order.record(time.perf_counter_ns() - t0_ns)
        try:
            await self.gateway.submit(order)
        except Exception as exc:  # erreur réseau / passerelle : l'ordre est rejeté localement
            log.exception("échec d'envoi %s", cid)
            self.on_event(OrderEvent(EventKind.REJECTED, cid, reason=str(exc)))
        return 1

    async def _cancel(self, order: Order, force: bool = False) -> int:
        if order.status in _PENDING or order.status.is_terminal:
            return 0
        if not self.risk.check_action().allowed and not force:
            return 0
        order.status = OrderStatus.PENDING_CANCEL
        order.updated_ns = self._clock()
        try:
            await self.gateway.cancel(order)
        except Exception:
            log.exception("échec d'annulation %s", order.client_order_id)
            order.status = OrderStatus.WORKING
        return 1

    async def _replace(self, order: Order, price: float, t0_ns: int) -> int:
        if not self.risk.check_action().allowed:
            return 0
        prev = order.status
        order.status = OrderStatus.PENDING_REPLACE
        order.updated_ns = self._clock()
        self.tick_to_order.record(time.perf_counter_ns() - t0_ns)
        try:
            await self.gateway.replace(order, price, order.qty)
        except Exception:
            log.exception("échec de modification %s", order.client_order_id)
            order.status = prev
        return 1

    # ------------------------------------------------------------------ #
    # Événements de la passerelle
    # ------------------------------------------------------------------ #
    def on_event(self, ev: OrderEvent) -> None:
        order = self.orders.get(ev.client_order_id)
        if order is None:
            return  # ordre inconnu ou déjà terminé : événement tardif ignoré
        order.updated_ns = self._clock()
        if ev.exchange_order_id is not None:
            order.exchange_order_id = ev.exchange_order_id
        k = ev.kind

        if k is EventKind.ACK:
            if order.status is OrderStatus.PENDING_NEW:
                order.status = OrderStatus.WORKING
        elif k is EventKind.FILL:
            qty = min(ev.fill_qty, order.remaining)
            if qty <= _EPS:
                return
            tot = order.filled_qty + qty
            order.avg_fill_price = (order.avg_fill_price * order.filled_qty
                                    + ev.fill_price * qty) / tot
            order.filled_qty = tot
            self.fees += ev.fee
            self.fill_count += 1
            self._apply_fill(order.side, qty, ev.fill_price)
            if order.remaining <= _EPS:
                order.status = OrderStatus.FILLED
            elif order.status not in _PENDING:
                order.status = OrderStatus.PARTIALLY_FILLED
        elif k is EventKind.REPLACED:
            if ev.new_price is not None:
                order.price = ev.new_price
            order.status = (OrderStatus.PARTIALLY_FILLED if order.filled_qty > 0
                            else OrderStatus.WORKING)
        elif k is EventKind.CANCELED:
            order.status = OrderStatus.CANCELED
        elif k is EventKind.REJECTED:
            if order.status is OrderStatus.PENDING_CANCEL:
                # l'annulation a échoué (ordre probablement exécuté) : on attend la réconciliation
                order.status = OrderStatus.WORKING
            elif order.status is OrderStatus.PENDING_REPLACE:
                order.status = (OrderStatus.PARTIALLY_FILLED if order.filled_qty > 0
                                else OrderStatus.WORKING)
            else:
                order.status = OrderStatus.REJECTED
                order.reject_reason = ev.reason
                log.warning("ordre rejeté %s : %s", order.client_order_id, ev.reason)

        if order.status.is_terminal:
            self.orders.pop(order.client_order_id, None)

    def _apply_fill(self, side: Side, qty: float, price: float) -> None:
        signed = qty * int(side)
        pos = self.position
        if pos == 0.0 or (pos > 0) == (signed > 0):
            new = pos + signed
            self.avg_price = (self.avg_price * abs(pos) + price * qty) / abs(new)
            self.position = round(new, 8)
            return
        closing = min(qty, abs(pos))
        self.realized_pnl += closing * (price - self.avg_price) * self.contract_size * (1 if pos > 0 else -1)
        new = round(pos + signed, 8)
        if abs(new) <= _EPS:
            self.position, self.avg_price = 0.0, 0.0
        elif (new > 0) != (pos > 0):        # retournement : le reliquat ouvre au prix du fill
            self.position, self.avg_price = new, price
        else:
            self.position = new
