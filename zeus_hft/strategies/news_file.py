"""Lecture du calendrier économique exporté par ``mql5/ZeusNewsExporter.mq5``.

Le paquet Python MetaTrader5 n'expose pas le calendrier (``CalendarValueHistory``
n'existe qu'en MQL5). Le petit EA ZeusNewsExporter, attaché à un graphique dans MT5,
écrit les annonces à fort impact dans ``Common\\Files\\zeus_news.csv`` ; ce module
les relit. Format : ``time_utc;currency;importance;event`` (time_utc = AAAA.MM.JJ HH:MM).
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path


def load_news_csv(path: str | Path) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        sample = f.read(2048)
        f.seek(0)
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        for row in csv.DictReader(f, delimiter=delim):
            try:
                t = (row.get("time_utc") or "").strip().replace(".", "-").replace("T", " ")
                dt = datetime.strptime(t[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            name = f"{(row.get('currency') or '').strip()} {(row.get('event') or '').strip()}".strip()
            events.append((int(dt.timestamp() * 1000), name))
    return sorted(events)
