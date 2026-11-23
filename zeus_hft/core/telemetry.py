"""Télémétrie non bloquante : logs via QueueHandler, métriques via asyncio.Queue.

- Les logs passent par ``logging.handlers.QueueHandler`` : le hot path ne fait
  qu'un ``put_nowait`` ; l'écriture disque/console a lieu dans le thread du
  ``QueueListener``, jamais dans la boucle de décision.
- Les métriques de latence sont stockées dans des ring buffers numpy
  (``LatencyTracker``) ; les percentiles sont calculés hors hot path.
- ``MetricsBus`` : file asyncio bornée pour les événements de télémétrie
  (perte silencieuse si pleine : la télémétrie ne doit jamais ralentir le trading).
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import queue
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from zeus_hft.core.ringbuffer import RingBuffer

_listener: logging.handlers.QueueListener | None = None


def setup_logging(level: str = "INFO", log_file: str | None = "data/zeus_hft.log",
                  extra_handlers: list[logging.Handler] | None = None) -> None:
    """Configure un logging 100 % asynchrone (idempotent)."""
    global _listener
    if _listener is not None:
        return
    q: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s | %(message)s",
                            "%H:%M:%S")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    handlers.extend(extra_handlers or [])
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.handlers.QueueHandler(q))  # type: ignore[arg-type]
    root.setLevel(level)
    _listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)  # type: ignore[arg-type]
    _listener.start()


def shutdown_logging() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


@dataclass(slots=True, frozen=True)
class LatencyStats:
    count: int
    p50_us: float
    p90_us: float
    p99_us: float
    max_us: float
    mean_us: float


class LatencyTracker:
    """Enregistre des durées en nanosecondes (perf_counter_ns)."""

    __slots__ = ("name", "_buf", "total")

    def __init__(self, name: str, capacity: int = 10_000) -> None:
        self.name = name
        self._buf = RingBuffer(capacity, dtype=np.int64)
        self.total = 0

    def record(self, duration_ns: int) -> None:
        self._buf.append(duration_ns)
        self.total += 1

    def record_since(self, start_ns: int) -> int:
        d = time.perf_counter_ns() - start_ns
        self.record(d)
        return d

    def stats(self) -> LatencyStats | None:
        if len(self._buf) == 0:
            return None
        a = self._buf.to_array().astype(np.float64) / 1e3
        p50, p90, p99 = np.percentile(a, [50, 90, 99])
        return LatencyStats(len(a), float(p50), float(p90), float(p99), float(a.max()),
                            float(a.mean()))

    def summary(self) -> str:
        s = self.stats()
        if s is None:
            return f"{self.name}: n/a"
        return (f"{self.name}: p50={s.p50_us:.1f}µs p90={s.p90_us:.1f}µs "
                f"p99={s.p99_us:.1f}µs max={s.max_us:.1f}µs (n={s.count})")


class MetricsBus:
    """File de télémétrie bornée ; ``emit`` ne bloque jamais."""

    def __init__(self, maxsize: int = 10_000) -> None:
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize)
        self.dropped = 0

    def emit(self, name: str, **fields: Any) -> None:
        try:
            self.queue.put_nowait((name, fields))
        except asyncio.QueueFull:
            self.dropped += 1
