"""Journal des trades (aller-retours) + statistiques de rentabilité cumulées.

- Écriture CSV dans un thread dédié (``queue.SimpleQueue``) : aucune I/O dans la
  boucle de décision.
- Au démarrage, l'historique existant est relu : les statistiques couvrent
  toutes les sessions, pas seulement la session en cours.
- ``verdict`` répond à « le bot est-il rentable ? » sur les N derniers trades,
  avec un minimum d'échantillon pour ne pas conclure trop tôt.
"""
from __future__ import annotations

import csv
import queue
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ["close_time_utc", "mode", "symbol", "side", "qty", "entry_price", "exit_price",
          "duration_s", "gross_pnl", "fees", "net_pnl", "reason", "strategy"]


@dataclass(slots=True)
class TradeRecord:
    close_time_utc: str
    mode: str
    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float
    duration_s: float
    gross_pnl: float
    fees: float
    net_pnl: float
    reason: str = ""
    strategy: str = ""

    def row(self) -> list[object]:
        return [self.close_time_utc, self.mode, self.symbol, self.side, round(self.qty, 4),
                round(self.entry_price, 5), round(self.exit_price, 5), round(self.duration_s, 2),
                round(self.gross_pnl, 4), round(self.fees, 4), round(self.net_pnl, 4), self.reason,
                self.strategy]


@dataclass(slots=True)
class JournalStats:
    trades: int
    wins: int
    net_pnl: float
    profit_factor: float
    recent_trades: int
    recent_net: float
    recent_pf: float
    verdict: str

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0


def _pf(gains: float, losses: float) -> float:
    if losses > 0:
        return gains / losses
    return float("inf") if gains > 0 else 0.0


class TradeJournal:
    def __init__(self, path: str | Path | None, mode: str, symbol: str,
                 window: int = 100, min_trades: int = 50) -> None:
        self.path = Path(path) if path else None
        self.mode = mode
        self.symbol = symbol
        self.min_trades = min_trades
        self.trades = 0
        self.wins = 0
        self.net = 0.0
        self.gains = 0.0
        self.losses = 0.0
        self.recent: deque[float] = deque(maxlen=window)
        self.by_strategy: dict[str, list[float]] = {}     # stratégie -> [trades, net, gains, pertes]
        self._q: queue.SimpleQueue[TradeRecord | None] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        if self.path is not None:
            self._load_history()
            self._thread = threading.Thread(target=self._writer, name="journal", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ #
    def _load_history(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                header = reader.fieldnames or []
                rows = list(reader)
            if header != FIELDS:            # ancien format (v2.1) : migration sans perte
                with open(self.path, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
                    w.writeheader()
                    for r in rows:
                        w.writerow({k: r.get(k, "") for k in FIELDS})
            for row in rows:
                # on ne mélange pas démo réelle, paper trading et autres symboles
                if row.get("mode") == self.mode and row.get("symbol") == self.symbol:
                    self._account(float(row["net_pnl"]), row.get("strategy") or "")
        except (OSError, ValueError, KeyError):
            pass

    def _writer(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            rec = self._q.get()
            if rec is None:
                return
            new = not self.path.exists()
            try:
                with open(self.path, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(FIELDS)
                    w.writerow(rec.row())
            except OSError:
                pass  # disque plein / fichier ouvert dans Excel : on ne bloque jamais le trading

    def _account(self, net: float, strategy: str = "") -> None:
        st = self.by_strategy.setdefault(strategy or "autre", [0, 0.0, 0.0, 0.0])
        st[0] += 1
        st[1] += net
        if net > 0:
            st[2] += net
        else:
            st[3] -= net
        self.trades += 1
        self.net += net
        if net > 0:
            self.wins += 1
            self.gains += net
        else:
            self.losses -= net
        self.recent.append(net)

    # ------------------------------------------------------------------ #
    def record(self, side: str, qty: float, entry: float, exit_: float, duration_s: float,
               gross: float, fees: float, reason: str = "", strategy: str = "") -> TradeRecord:
        rec = TradeRecord(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), self.mode,
                          self.symbol, side, qty, entry, exit_, duration_s, gross, fees,
                          gross - fees, reason, strategy)
        self._account(rec.net_pnl, strategy)
        if self._thread is not None:
            self._q.put(rec)
        return rec

    def stats(self) -> JournalStats:
        rg = sum(x for x in self.recent if x > 0)
        rl = -sum(x for x in self.recent if x <= 0)
        rpf = _pf(rg, rl)
        n = len(self.recent)
        if self.trades < self.min_trades:
            verdict = f"en apprentissage ({self.trades}/{self.min_trades} trades)"
        elif rpf >= 1.2 and sum(self.recent) > 0:
            verdict = f"RENTABLE sur les {n} derniers trades (PF {rpf:.2f})"
        elif rpf >= 1.0:
            verdict = f"à l'équilibre (PF {rpf:.2f}) : pas encore assez solide"
        else:
            verdict = f"perdant sur les {n} derniers trades (PF {rpf:.2f})"
        return JournalStats(self.trades, self.wins, self.net, _pf(self.gains, self.losses),
                            n, sum(self.recent), rpf, verdict)

    def strategy_summary(self) -> str:
        """Ex. : 'momentum 42 trades PF 1.31 | meanrev 18 trades PF 0.84'."""
        parts = []
        for name, (n, _net, g, l) in sorted(self.by_strategy.items()):
            pf = _pf(g, l)
            pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
            parts.append(f"{name} {int(n)} trades PF {pf_s}")
        return " | ".join(parts)

    def close(self) -> None:
        if self._thread is not None:
            self._q.put(None)
            self._thread.join(2.0)
            self._thread = None
