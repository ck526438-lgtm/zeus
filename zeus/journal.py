"""Journal des trades (CSV) + notifications Telegram optionnelles."""
from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger("zeus.journal")

FIELDS = ["time", "event", "symbol", "direction", "volume", "price", "sl", "tp",
          "pnl", "score", "proba", "reason", "ticket"]


class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, FIELDS).writeheader()

    def write(self, **row):
        with self.path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, FIELDS, extrasaction="ignore").writerow(row)


class Notifier:
    def __init__(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled") and cfg.get("token") and cfg.get("chat_id"))
        self.token, self.chat = cfg.get("token"), cfg.get("chat_id")

    def send(self, text: str):
        if not self.enabled:
            return
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          data={"chat_id": self.chat, "text": text}, timeout=5)
        except Exception as e:  # une notif ratée ne doit jamais arrêter le bot
            log.warning("Telegram : %s", e)
