"""Alertes temps réel (Telegram), sans jamais bloquer la boucle de trading.

Les messages passent par une file ; un thread dédié les envoie via l'API HTTP de
Telegram (bibliothèque standard, aucune dépendance). Anti-spam : une même clé
d'alerte n'est renvoyée qu'après ``cooldown_s``. Sans jeton configuré, les alertes
sont seulement écrites dans le log.

Jeton et chat : variables ``ZEUS_TG_TOKEN`` / ``ZEUS_TG_CHAT`` (comme Zeus v1), ou
champs de l'appli. Créer un bot : @BotFather sur Telegram ; chat id : @userinfobot.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable

log = logging.getLogger("zeus_hft.alerts")

ICONS = {"info": "ℹ️", "warn": "⚠️", "crit": "🛑", "ok": "✅"}


class Alerter:
    def __init__(self, token: str = "", chat_id: str = "", prefix: str = "Zeus HFT",
                 cooldown_s: float = 300.0,
                 sender: Callable[[str, str, str], None] | None = None) -> None:
        self.token = token.strip()
        self.chat_id = chat_id.strip()
        self.prefix = prefix
        self.cooldown_s = cooldown_s
        self._last: dict[str, float] = {}
        self._q: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._send = sender or _telegram_send
        self.sent = 0
        self.failed = 0
        self._thread: threading.Thread | None = None
        if self.enabled:
            self._thread = threading.Thread(target=self._worker, name="alerts", daemon=True)
            self._thread.start()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def alert(self, level: str, key: str, text: str, force: bool = False) -> bool:
        """Non bloquant. Retourne False si l'alerte est filtrée par l'anti-spam."""
        now = time.monotonic()
        if not force and now - self._last.get(key, -1e18) < self.cooldown_s:
            return False
        self._last[key] = now
        msg = f"{ICONS.get(level, '')} {self.prefix} — {text}"
        (log.critical if level == "crit" else log.warning if level == "warn" else log.info)(
            "ALERTE %s", text)
        if self.enabled:
            self._q.put(msg)
        return True

    def _worker(self) -> None:
        while True:
            msg = self._q.get()
            if msg is None:
                return
            for attempt in range(3):
                try:
                    self._send(self.token, self.chat_id, msg)
                    self.sent += 1
                    break
                except Exception as exc:  # réseau coupé : on réessaie, sans jamais bloquer le bot
                    if attempt == 2:
                        self.failed += 1
                        log.warning("alerte Telegram non envoyée : %s", exc)
                    time.sleep(2 ** attempt)

    def close(self) -> None:
        if self._thread is not None:
            self._q.put(None)
            self._thread.join(5.0)
            self._thread = None


def _telegram_send(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read().decode() or "{}")
        if not body.get("ok", False):
            raise RuntimeError(body.get("description", "réponse Telegram invalide"))
