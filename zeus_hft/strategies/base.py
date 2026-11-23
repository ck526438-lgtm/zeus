"""Interface commune des stratégies."""
from __future__ import annotations

from abc import ABC, abstractmethod

from zeus_hft.core.orderbook import OrderBook
from zeus_hft.core.types import Decision, Tick


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def on_tick(self, book: OrderBook, tick: Tick, position: float, avg_price: float,
                now_ns: int) -> Decision | None:
        """Appelée à chaque mise à jour de marché (hot path : pas d'I/O, pas d'allocation lourde).

        Retourne une ``Decision`` (position cible) ou None pour ne rien changer.
        """

    def reset(self) -> None:
        """Remise à zéro de l'état interne (après resynchronisation du carnet)."""

    def state(self) -> dict[str, float]:
        """Variables internes exposées à la télémétrie / l'interface."""
        return {}
