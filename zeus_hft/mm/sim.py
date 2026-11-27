"""Exchange simulé pour le market making, avec POSITION DANS LA FILE D'ATTENTE.

Sans file d'attente, un backtest de market making ment : il suppose qu'on est
exécuté dès que le prix touche notre ordre. En réalité, on est derrière tous les
ordres déjà posés au même prix.

Modèle :
- un ordre n'arrive au carnet qu'après la latence (+ gigue) ;
- post-only : s'il traverserait le spread à l'arrivée, il est rejeté ;
- à l'arrivée, ``devant nous`` = quantité déjà visible à ce prix (0 si on améliore le meilleur prix) ;
- un trade agressif au même prix consomme d'abord la file devant nous, puis notre ordre ;
- un trade à un prix PLUS AGRESSIF que le nôtre (le prix nous traverse) nous exécute entièrement ;
- si la quantité visible à notre prix baisse (annulations), la file devant nous ne peut pas
  dépasser la quantité restante (hypothèse prudente : on ne remonte jamais plus vite que ça) ;
- une annulation n'est effective qu'après la latence : on peut être exécuté entre-temps.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from typing import Callable

BUY, SELL = 1, -1


@dataclass(slots=True)
class SimOrder:
    cid: str
    side: int
    price: float
    qty: float
    filled: float = 0.0
    ahead: float = 0.0
    live: bool = False

    @property
    def remaining(self) -> float:
        return self.qty - self.filled


class QueueSimExchange:
    def __init__(self, *, maker_fee: float, contract_size: float, latency_ms: float = 30.0,
                 jitter_ms: float = 20.0, seed: int = 7,
                 clock_ms: Callable[[], int] | None = None) -> None:
        self.maker_fee = maker_fee
        self.contract_size = contract_size
        self.latency = int(latency_ms)
        self.jitter = int(jitter_ms)
        self._rng = random.Random(seed)
        self._clock = clock_ms or (lambda: 0)
        self._inbox: deque[tuple[int, str, SimOrder]] = deque()
        self.orders: dict[str, SimOrder] = {}
        self.best_bid = 0.0
        self.best_ask = 0.0
        self._level: Callable[[int, float], float] = lambda side, px: 0.0
        self.on_event: Callable[[str, str, float, float, float], None] = lambda *a: None
        # on_event(kind, cid, qty, price, fee) ; kind ∈ ack, fill, canceled, rejected

    # --- interface passerelle (appelée par le QuoteManager) -------------- #
    def _arrival(self) -> int:
        t = self._clock() + self.latency + (self._rng.randint(0, self.jitter) if self.jitter else 0)
        if self._inbox and t < self._inbox[-1][0]:
            t = self._inbox[-1][0]
        return t

    async def place(self, cid: str, side: int, price: float, qty: float, closing: bool) -> None:
        self._inbox.append((self._arrival(), "new", SimOrder(cid, side, price, qty)))

    async def cancel(self, cid: str) -> None:
        o = self.orders.get(cid) or SimOrder(cid, 0, 0.0, 0.0)
        self._inbox.append((self._arrival(), "cancel", o))

    async def cancel_all(self) -> None:
        for cid in list(self.orders):
            await self.cancel(cid)

    # --- moteur ----------------------------------------------------------- #
    def set_level_fn(self, fn: Callable[[int, float], float]) -> None:
        """fn(side, price) -> quantité visible à ce prix (côté bid si side=BUY)."""
        self._level = fn

    def _fee(self, qty: float, price: float) -> float:
        return qty * self.contract_size * price * self.maker_fee

    def _fill(self, o: SimOrder, qty: float) -> None:
        qty = min(qty, o.remaining)
        if qty <= 0:
            return
        o.filled += qty
        self.on_event("fill", o.cid, qty, o.price, self._fee(qty, o.price))
        if o.remaining <= 1e-12:
            self.orders.pop(o.cid, None)

    def process(self) -> None:
        """Traite les actions arrivées à l'exchange (à appeler à chaque événement de marché)."""
        now = self._clock()
        while self._inbox and self._inbox[0][0] <= now:
            _, action, o = self._inbox.popleft()
            if action == "new":
                crosses = (o.side == BUY and self.best_ask and o.price >= self.best_ask) or \
                          (o.side == SELL and self.best_bid and o.price <= self.best_bid)
                if crosses:
                    self.on_event("rejected", o.cid, 0, 0, 0)          # post-only
                    continue
                better = (o.side == BUY and o.price > self.best_bid) or \
                         (o.side == SELL and o.price < self.best_ask)
                o.ahead = 0.0 if better else self._level(o.side, o.price)
                o.live = True
                self.orders[o.cid] = o
                self.on_event("ack", o.cid, 0, 0, 0)
            else:
                if o.cid in self.orders:
                    del self.orders[o.cid]
                    self.on_event("canceled", o.cid, 0, 0, 0)
                else:
                    self.on_event("cancel_rejected", o.cid, 0, 0, 0)    # déjà exécuté

    def on_book(self, best_bid: float, best_ask: float) -> None:
        self.best_bid, self.best_ask = best_bid, best_ask
        self.process()
        for o in list(self.orders.values()):
            # le carnet est passé à travers notre prix : nous avons forcément été exécutés
            if (o.side == BUY and best_ask <= o.price) or (o.side == SELL and best_bid >= o.price):
                self._fill(o, o.remaining)
                continue
            visible = self._level(o.side, o.price)
            if o.ahead > visible:
                o.ahead = visible

    def on_trade(self, price: float, qty: float, aggressor: int) -> None:
        """aggressor = BUY : un acheteur prend l'ask (il touche nos ventes)."""
        self.process()
        remaining = qty
        resting_side = -aggressor
        book = sorted((o for o in self.orders.values() if o.side == resting_side),
                      key=lambda o: o.price, reverse=(resting_side == BUY))
        for o in book:
            through = (o.price > price) if o.side == BUY else (o.price < price)
            if through:
                self._fill(o, o.remaining)                  # le prix nous a traversés
            elif o.price == price and remaining > 0:
                use = min(remaining, o.ahead)
                o.ahead -= use
                remaining -= use
                if remaining > 0:
                    take = min(remaining, o.remaining)
                    self._fill(o, take)
                    remaining -= take
