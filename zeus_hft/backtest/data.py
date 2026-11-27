"""Chargement / téléchargement de ticks (sans pandas)."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from zeus_hft.core.types import Tick
from zeus_hft.feeds.base import MarketUpdate


def _parse_time_ms(row: dict[str, str]) -> int:
    if row.get("time_msc"):
        return int(float(row["time_msc"]))
    t = row.get("time") or row.get("timestamp") or ""
    try:
        v = float(t)
        return int(v if v > 1e11 else v * 1000)        # epoch s ou ms
    except ValueError:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)


def load_ticks_csv(path: str | Path, symbol: str) -> Iterator[MarketUpdate]:
    """CSV avec au minimum : time_msc (ou time), bid, ask. Colonnes optionnelles :
    bid_qty, ask_qty, last, volume. Les lignes sans bid/ask valides sont ignorées."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bid, ask = float(row["bid"]), float(row["ask"])
            except (KeyError, ValueError):
                continue
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            ts = _parse_time_ms(row)
            yield MarketUpdate(Tick(symbol, bid, ask,
                                    bid_qty=float(row.get("bid_qty") or 0.0),
                                    ask_qty=float(row.get("ask_qty") or 0.0),
                                    last=float(row.get("last") or 0.0),
                                    volume=float(row.get("volume") or 0.0),
                                    exchange_ts_ms=ts, recv_ts_ns=ts * 1_000_000))


def download_ticks_mt5(symbol: str, days: int, out: str | Path, mt5: object | None = None) -> int:
    """Télécharge l'historique de ticks depuis le terminal MT5 (Windows) vers un CSV."""
    if mt5 is None:
        from zeus_hft.feeds.mt5_gateway import load_mt5
        mt5 = load_mt5()
    m = mt5  # type: ignore[assignment]
    if not m.initialize():  # type: ignore[attr-defined]
        raise RuntimeError(f"connexion MT5 impossible : {m.last_error()}")  # type: ignore[attr-defined]
    try:
        m.symbol_select(symbol, True)  # type: ignore[attr-defined]
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        ticks = m.copy_ticks_range(symbol, start, end, m.COPY_TICKS_ALL)  # type: ignore[attr-defined]
        if ticks is None or len(ticks) == 0:
            raise RuntimeError(f"aucun tick reçu : {m.last_error()}")  # type: ignore[attr-defined]
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["time_msc", "bid", "ask", "last", "volume"])
            for t in ticks:
                w.writerow([int(t["time_msc"]), t["bid"], t["ask"], t["last"], t["volume"]])
        # bougies M15 / H1 (avec 30 jours d'avance) pour le filtre de tendance du backtest
        for tf_name in ("M15", "H1"):
            tf = getattr(m, f"TIMEFRAME_{tf_name}")
            rates = m.copy_rates_range(symbol, tf, start - timedelta(days=30), end)  # type: ignore[attr-defined]
            if rates is None:
                continue
            with open(bars_path(out, symbol, tf_name), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time_ms", "open", "high", "low", "close"])
                for r in rates:
                    w.writerow([int(r["time"]) * 1000, r["open"], r["high"], r["low"], r["close"]])
        return len(ticks)
    finally:
        m.shutdown()  # type: ignore[attr-defined]


def bars_path(ticks_csv: str | Path, symbol: str, tf: str) -> Path:
    return Path(ticks_csv).with_name(f"{symbol}_{tf}.csv")


def load_bars_csv(path: str | Path) -> list[tuple[int, float, float, float, float]]:
    """Bougies (time_ms d'ouverture, o, h, l, c) triées par date ; [] si le fichier n'existe pas.
    Le simulateur n'utilise que celles clôturées avant le 1er tick (pas de biais du futur)."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[tuple[int, float, float, float, float]] = []
    with open(p, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out.append((int(float(r["time_ms"])), float(r["open"]), float(r["high"]),
                            float(r["low"]), float(r["close"])))
            except (KeyError, ValueError):
                continue
    out.sort()
    return out
