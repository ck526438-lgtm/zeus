"""Types partagés (dataclasses à slots : empreinte mémoire minimale, accès rapide)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Protocol


class Side(IntEnum):
    BUY = 1
    SELL = -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PENDING_NEW = "PENDING_NEW"          # créé localement, pas encore acquitté
    WORKING = "WORKING"                  # acquitté, au carnet
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_active(self) -> bool:
        return self not in _TERMINAL


_TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED})


class Urgency(str, Enum):
    PASSIVE = "PASSIVE"        # ordre limite au meilleur prix de notre côté (maker)
    AGGRESSIVE = "AGGRESSIVE"  # ordre au marché (taker)


class EventKind(str, Enum):
    ACK = "ACK"
    FILL = "FILL"
    CANCELED = "CANCELED"
    REPLACED = "REPLACED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class Tick:
    """Tick normalisé (MT5, simulateur ou autre source)."""

    symbol: str
    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    last: float = 0.0
    volume: float = 0.0
    exchange_ts_ms: int = 0
    recv_ts_ns: int = 0


@dataclass(slots=True)
class Decision:
    """Sortie de la stratégie : position cible + manière d'y aller."""

    target_position: float
    urgency: Urgency = Urgency.PASSIVE
    limit_price: float | None = None
    reason: str = ""
    score: float = 0.0
    stop_points: float | None = None     # SL de la stratégie (sert au SL de secours serveur)
    strategy: str = ""


@dataclass(slots=True)
class Order:
    client_order_id: str
    symbol: str
    side: Side
    qty: float
    order_type: OrderType
    price: float | None = None
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    exchange_order_id: int | str | None = None
    created_ns: int = field(default_factory=time.perf_counter_ns)
    updated_ns: int = 0
    tick_recv_ns: int = 0          # horodatage du tick qui a déclenché l'ordre
    reject_reason: str = ""
    reduce_only: bool = False      # ordre de fermeture (comptes hedging : clôture par ticket)
    sl: float | None = None        # stop-loss de secours côté serveur
    req_price: float = 0.0         # prix demandé (télémétrie : slippage)
    req_spread_pts: float = 0.0    # spread au moment de l'envoi
    sent_ns: int = 0               # horodatage d'envoi (télémétrie : latence)

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)

    @property
    def signed_remaining(self) -> float:
        return self.remaining * int(self.side)


@dataclass(slots=True)
class OrderEvent:
    kind: EventKind
    client_order_id: str
    exchange_order_id: int | str | None = None
    fill_qty: float = 0.0
    fill_price: float = 0.0
    fee: float = 0.0
    is_maker: bool = False
    new_price: float | None = None
    new_qty: float | None = None
    reason: str = ""
    ts_ns: int = field(default_factory=time.perf_counter_ns)


class EventSink(Protocol):
    def __call__(self, event: OrderEvent) -> None: ...


class ExecutionGateway(Protocol):
    """Interface commune : MT5 réel, paper trading, simulateur de backtest."""

    def set_event_sink(self, sink: EventSink) -> None: ...
    async def submit(self, order: Order) -> None: ...
    async def cancel(self, order: Order) -> None: ...
    async def replace(self, order: Order, new_price: float, new_qty: float) -> None: ...
