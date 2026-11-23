"""Coupe-circuit (kill-switch) vérifié avant CHAQUE action d'ordre.

Règles :
1. Kill-switch « collant » : une fois déclenché, plus aucun ordre d'ouverture
   (seules les réductions de position restent autorisées pour aplatir).
2. Drawdown journalier : equity courante vs plus haut de la journée (UTC).
3. Exposition nette : |position + ordres en cours + nouvel ordre| <= max.
4. Throttling : nombre d'actions (envoi, annulation, modification) par seconde
   glissante.
5. Latence : un échantillon > seuil bloque les ordres ; N dépassements
   consécutifs déclenchent l'arrêt d'urgence.
6. Fraîcheur des données : pas d'ordre sur un carnet trop ancien.
Toutes les vérifications sont O(1) amorti et sans I/O.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable


class RiskReason(str, Enum):
    OK = "OK"
    KILLED = "KILLED"
    DAILY_DRAWDOWN = "DAILY_DRAWDOWN"
    INVENTORY = "INVENTORY"
    THROTTLE = "THROTTLE"
    LATENCY = "LATENCY"
    STALE_DATA = "STALE_DATA"
    INVALID_QTY = "INVALID_QTY"


@dataclass(slots=True, frozen=True)
class RiskDecision:
    allowed: bool
    reason: RiskReason
    detail: str = ""


_OK = RiskDecision(True, RiskReason.OK)


@dataclass(slots=True)
class RiskLimits:
    max_daily_drawdown: float
    max_net_inventory: float
    max_orders_per_second: int
    max_latency_ms: float
    latency_breaches_to_kill: int = 3
    max_data_staleness_ms: float = 5_000.0


def _utc_day(ts_s: float) -> int:
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).toordinal()


class RiskManager:
    def __init__(
        self,
        limits: RiskLimits,
        starting_equity: float,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        wall_clock_s: Callable[[], float] = time.time,
        on_kill: Callable[[str], None] | None = None,
    ) -> None:
        self.limits = limits
        self._clock_ns = clock_ns
        self._wall_s = wall_clock_s
        self._on_kill = on_kill
        self.killed = False
        self.kill_reason = ""
        self.equity = starting_equity
        self.day_start_equity = starting_equity
        self.day_peak_equity = starting_equity
        self._day = _utc_day(wall_clock_s())
        self._actions: deque[int] = deque()
        self.last_latency_ms = 0.0
        self._latency_breaches = 0
        self.latency_blocked = False
        self.rejections: dict[RiskReason, int] = {r: 0 for r in RiskReason}

    # ------------------------------------------------------------------ #
    # Entrées
    # ------------------------------------------------------------------ #
    def kill(self, reason: str) -> None:
        if not self.killed:
            self.killed = True
            self.kill_reason = reason
            if self._on_kill is not None:
                self._on_kill(reason)

    def update_equity(self, equity: float) -> None:
        """À appeler à chaque fill / tick (equity = solde + PnL latent)."""
        day = _utc_day(self._wall_s())
        if day != self._day:  # nouvelle journée UTC : on repart de l'equity courante
            self._day = day
            self.day_start_equity = equity
            self.day_peak_equity = equity
        self.equity = equity
        if equity > self.day_peak_equity:
            self.day_peak_equity = equity
        if self.daily_drawdown >= self.limits.max_daily_drawdown:
            self.kill(f"drawdown journalier {self.daily_drawdown:.2f} >= "
                      f"{self.limits.max_daily_drawdown:.2f}")

    def record_latency(self, latency_ms: float) -> None:
        self.last_latency_ms = latency_ms
        if latency_ms > self.limits.max_latency_ms:
            self._latency_breaches += 1
            self.latency_blocked = True
            if self._latency_breaches >= self.limits.latency_breaches_to_kill:
                self.kill(f"latence {latency_ms:.1f} ms > {self.limits.max_latency_ms} ms "
                          f"({self._latency_breaches}x de suite)")
        else:
            self._latency_breaches = 0
            self.latency_blocked = False

    # ------------------------------------------------------------------ #
    # Vérification pré-ordre
    # ------------------------------------------------------------------ #
    @property
    def daily_drawdown(self) -> float:
        return max(self.day_peak_equity - self.equity, 0.0)

    @property
    def daily_pnl(self) -> float:
        return self.equity - self.day_start_equity

    def _throttle_ok(self, now: int) -> bool:
        window_start = now - 1_000_000_000
        a = self._actions
        while a and a[0] <= window_start:
            a.popleft()
        return len(a) < self.limits.max_orders_per_second

    def _reject(self, reason: RiskReason, detail: str = "") -> RiskDecision:
        self.rejections[reason] += 1
        return RiskDecision(False, reason, detail)

    def check_order(
        self,
        signed_qty: float,
        position: float,
        working_signed: float,
        data_age_ms: float = 0.0,
    ) -> RiskDecision:
        """Valide un nouvel ordre (ou une modification qui change la quantité).

        signed_qty     : + achat / − vente
        position       : position nette actuelle (lots)
        working_signed : somme signée des quantités restantes des ordres actifs
        """
        if signed_qty == 0.0:
            return self._reject(RiskReason.INVALID_QTY)
        projected = position + working_signed + signed_qty
        reduces = abs(projected) < abs(position + working_signed) - 1e-12

        if self.killed and not reduces:
            return self._reject(RiskReason.KILLED, self.kill_reason)
        if data_age_ms > self.limits.max_data_staleness_ms:
            return self._reject(RiskReason.STALE_DATA, f"{data_age_ms:.0f} ms")
        if not reduces:
            if self.latency_blocked:
                return self._reject(RiskReason.LATENCY, f"{self.last_latency_ms:.1f} ms")
            if self.daily_drawdown >= self.limits.max_daily_drawdown:
                self.kill("drawdown journalier")
                return self._reject(RiskReason.DAILY_DRAWDOWN)
            if abs(projected) > self.limits.max_net_inventory + 1e-12:
                return self._reject(RiskReason.INVENTORY,
                                    f"{projected:+.4f} > ±{self.limits.max_net_inventory}")
        return self._consume_action()

    def check_action(self) -> RiskDecision:
        """Annulation / modification de prix : seulement le throttling."""
        return self._consume_action()

    def _consume_action(self) -> RiskDecision:
        now = self._clock_ns()
        if not self._throttle_ok(now):
            return self._reject(RiskReason.THROTTLE,
                                f"> {self.limits.max_orders_per_second} ordres/s")
        self._actions.append(now)
        return _OK

    def status(self) -> dict[str, object]:
        return {
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "equity": round(self.equity, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_drawdown": round(self.daily_drawdown, 2),
            "latency_ms": round(self.last_latency_ms, 2),
            "rejections": {k.value: v for k, v in self.rejections.items() if v},
        }
