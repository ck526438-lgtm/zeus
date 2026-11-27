"""Enregistrement de chaque tick brut reçu (.csv.gz, un fichier par jour), pour :
- rejouer exactement la séance en backtest (``backtest --csv data/ticks/XAUUSD_20261127.csv.gz``) ;
- analyser un incident après coup.

Écriture dans un thread dédié (le hot path ne fait qu'un ``put`` dans une file) ;
compression gzip (~5 à 10 fois plus petit) ; format identique à ``download`` / ``load_ticks_csv``.
"""
from __future__ import annotations

import gzip
import io
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from zeus_hft.core.types import Tick

HEADER = "time_msc,bid,ask,bid_qty,ask_qty,last,volume\n"


class TickRecorder:
    def __init__(self, directory: str | Path, symbol: str, flush_every: int = 500) -> None:
        self.dir = Path(directory)
        self.symbol = symbol
        self.flush_every = flush_every
        self.count = 0
        self.dropped = 0
        self._q: queue.SimpleQueue[Tick | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._worker, name="tick-recorder", daemon=True)
        self._thread.start()

    def put(self, t: Tick) -> None:
        self._q.put(t)

    def path_for(self, ts_ms: int) -> Path:
        day = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y%m%d")
        return self.dir / f"{self.symbol}_{day}.csv.gz"

    def _worker(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        cur_path: Path | None = None
        fh: io.TextIOWrapper | None = None
        pending = 0
        while True:
            t = self._q.get()
            if t is None:
                break
            try:
                p = self.path_for(t.exchange_ts_ms)
                if p != cur_path:
                    if fh is not None:
                        fh.close()
                    new = not p.exists()
                    # mode « append » : gzip accepte plusieurs membres concaténés dans un fichier
                    fh = io.TextIOWrapper(gzip.open(p, "ab", compresslevel=5), encoding="utf-8")
                    if new:
                        fh.write(HEADER)
                    cur_path = p
                assert fh is not None
                fh.write(f"{t.exchange_ts_ms},{t.bid},{t.ask},{t.bid_qty},{t.ask_qty},"
                         f"{t.last},{t.volume}\n")
                self.count += 1
                pending += 1
                if pending >= self.flush_every:
                    fh.flush()
                    pending = 0
            except OSError:
                self.dropped += 1
        if fh is not None:
            fh.close()

    def close(self) -> None:
        self._q.put(None)
        self._thread.join(5.0)
