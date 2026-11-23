"""Contrat des sources de données de marché."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Protocol

from zeus_hft.core.types import Tick

Level = tuple[float, float]


@dataclass(slots=True)
class MarketUpdate:
    tick: Tick
    bids: list[Level] | None = None     # DOM (meilleur en premier) si disponible
    asks: list[Level] | None = None


class MarketFeed(Protocol):
    connected: bool

    async def run(self, out: asyncio.Queue[MarketUpdate], stop: asyncio.Event) -> None:
        """Pousse des MarketUpdate dans ``out`` jusqu'à ``stop``."""
        ...


def backoff_delays(base: float, cap: float, max_attempts: int = 0):
    """Délais de reconnexion exponentiels avec jitter (« full jitter »)."""
    attempt = 0
    while max_attempts == 0 or attempt < max_attempts:
        yield random.uniform(0.0, min(cap, base * (2 ** attempt)))
        attempt += 1
