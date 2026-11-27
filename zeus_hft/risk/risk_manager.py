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
from typing import Any, Callable


class RiskReason(str, Enum):
    OK = "OK"
    KILLED = "KILLED"
    DAILY_DRAWDOWN = "DAILY_DRAWDOWN"
    INVENTORY = "INVENTORY"
    THROTTLE = "THROTTLE"
    LATENCY = "LATENCY"
    STALE_DATA = "STALE_DATA"
    INVALID_QTY = "INVALID_QTY"
    SPREAD = "SPREAD"
    HALTED = "HALTED"
    NOTIONAL = "NOTIONAL"


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
    max_spread_points: float = 0.0          # 0 = pas de plafond
    max_daily_loss_pct: float = 0.0         # % de l'equity du début de journée (0 = off)
    max_consecutive_rejects: int = 5
    max_consecutive_losses: int = 0          # 0 = off
    loss_streak_pause_min: float = 120.0     # 0 = jusqu'au lendemain
    max_notional: float = 0.0                # exposition max en devise du compte (0 = off)
    window_dd: float = 0.0                   # drawdown max sur fenêtre glissante (0 = off)
    window_dd_minutes: float = 60.0
    max_reject_rate: float = 0.3             # > 30 % de rejets sur 60 s -> mode dégradé
    degraded_pause_min: float = 5.0


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
        on_halt: Callable[[str, str], None] | None = None,
    ) -> None:
        self._on_halt = on_halt
        self._eq_win: deque[tuple[int, float]] = deque()     # (ns, equity) pour le max glissant
        self._outcomes: deque[tuple[int, bool]] = deque()    # (ns, accepté ?) des 60 dernières s
        self.limits = limits
        self._clock_ns = clock_ns
        self._wall_s = wall_clock_s
        self._on_kill = on_kill
        self.killed = False
        self.kill_reason = ""
        self._kill_daily = False           # True : levé automatiquement le jour suivant (UTC)
        self.halts: dict[str, str] = {}    # pauses temporaires (flux figé, série de pertes...)
        self._halt_until: dict[str, int] = {}
        self._rejects = 0
        self.loss_streak = 0
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
    def kill(self, reason: str, daily: bool = False) -> None:
        """Arrêt d'urgence. ``daily=True`` : levé automatiquement à la journée UTC suivante."""
        if not self.killed:
            self.killed = True
            self.kill_reason = reason
            self._kill_daily = daily
            if self._on_kill is not None:
                self._on_kill(reason)

    def halt(self, key: str, reason: str, duration_s: float | None = None) -> None:
        if key not in self.halts and self._on_halt is not None:
            self._on_halt(key, reason)
        self.halts[key] = reason
        if duration_s is not None:
            self._halt_until[key] = self._clock_ns() + int(duration_s * 1e9)

    def resume(self, key: str) -> None:
        self.halts.pop(key, None)
        self._halt_until.pop(key, None)

    def _expire_halts(self) -> None:
        if self._halt_until:
            now = self._clock_ns()
            for k, t in list(self._halt_until.items()):
                if now >= t:
                    self.resume(k)

    def record_trade(self, net_pnl: float) -> None:
        """Après chaque aller-retour : série de pertes -> pause (changement de régime probable)."""
        if net_pnl > 0:
            self.loss_streak = 0
            return
        self.loss_streak += 1
        n = self.limits.max_consecutive_losses
        if n and self.loss_streak >= n:
            self.loss_streak = 0
            pause = self.limits.loss_streak_pause_min
            if pause > 0:
                self.halt("pertes", f"{n} pertes de suite : pause {pause:.0f} min", pause * 60)
            else:
                self.kill(f"{n} pertes de suite (reprise demain 00:00 UTC)", daily=True)

    def _record_outcome(self, ok: bool) -> None:
        now = self._clock_ns()
        q = self._outcomes
        q.append((now, ok))
        while q and q[0][0] < now - 60_000_000_000:
            q.popleft()
        if not ok and len(q) >= 10:
            rate = sum(1 for _, o in q if not o) / len(q)
            if rate > self.limits.max_reject_rate:
                self.halt("degrade", f"mode dégradé : {rate:.0%} d'ordres rejetés sur 60 s "
                                     f"(réduction seule)", self.limits.degraded_pause_min * 60)
                q.clear()

    def record_execution_error(self, reason: str) -> None:
        self._record_outcome(False)
        self._rejects += 1
        if self._rejects >= self.limits.max_consecutive_rejects:
            self.kill(f"{self._rejects} erreurs d'exécution de suite (dernière : {reason})")

    def record_execution_ok(self) -> None:
        self._record_outcome(True)
        self._rejects = 0

    @property
    def daily_loss_limit(self) -> float:
        lim = self.limits.max_daily_drawdown
        if self.limits.max_daily_loss_pct > 0:
            lim = min(lim, self.day_start_equity * self.limits.max_daily_loss_pct / 100.0)
        return lim

    def update_equity(self, equity: float) -> None:
        """À appeler à chaque fill / tick (equity = solde + PnL latent)."""
        day = _utc_day(self._wall_s())
        if day != self._day:  # nouvelle journée UTC : on repart de l'equity courante
            self._day = day
            self.day_start_equity = equity
            self.day_peak_equity = equity
            if self.killed and self._kill_daily:       # nouvelle journée : reprise automatique
                self.killed, self.kill_reason, self._kill_daily = False, "", False
        self.equity = equity
        if equity > self.day_peak_equity:
            self.day_peak_equity = equity
        if self.limits.window_dd > 0:
            now = self._clock_ns()
            w = self._eq_win
            while w and w[-1][1] <= equity:                 # deque monotone décroissante
                w.pop()
            w.append((now, equity))
            horizon = now - int(self.limits.window_dd_minutes * 60e9)
            while w and w[0][0] < horizon:
                w.popleft()
            peak = w[0][1] if w else equity
            if peak - equity >= self.limits.window_dd and "fenetre" not in self.halts:
                self.halt("fenetre", f"drawdown {peak - equity:.2f} en moins de "
                                     f"{self.limits.window_dd_minutes:.0f} min (réduction seule)",
                          self.limits.window_dd_minutes * 60)
        if self.daily_drawdown >= self.daily_loss_limit:
            self.kill(f"perte du jour {self.daily_drawdown:.2f} >= {self.daily_loss_limit:.2f} "
                      f"(reprise automatique demain 00:00 UTC)", daily=True)

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
        spread_points: float = 0.0,
        notional_per_lot: float = 0.0,
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
            self._expire_halts()
            if self.halts:
                return self._reject(RiskReason.HALTED, "; ".join(self.halts.values()))
            if 0 < self.limits.max_spread_points < spread_points:
                return self._reject(RiskReason.SPREAD,
                                    f"{spread_points:.0f} > {self.limits.max_spread_points:.0f} pts")
            if self.latency_blocked:
                return self._reject(RiskReason.LATENCY, f"{self.last_latency_ms:.1f} ms")
            if self.daily_drawdown >= self.daily_loss_limit:
                self.kill("perte du jour", daily=True)
                return self._reject(RiskReason.DAILY_DRAWDOWN)
            if (self.limits.max_notional > 0 and notional_per_lot > 0
                    and abs(projected) * notional_per_lot > self.limits.max_notional):
                return self._reject(RiskReason.NOTIONAL,
                                    f"{abs(projected) * notional_per_lot:,.0f} > {self.limits.max_notional:,.0f}")
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

    # ------------------------------------------------------------------ #
    # Sauvegarde / reprise après crash
    # ------------------------------------------------------------------ #
    def to_state(self) -> dict[str, Any]:
        now = self._clock_ns()
        timed = {k: (self.halts[k], (t - now) / 1e9) for k, t in self._halt_until.items()
                 if k in self.halts and t > now}
        return {"version": 1, "day": self._day, "day_start_equity": self.day_start_equity,
                "day_peak_equity": self.day_peak_equity, "daily_kill": self.killed and self._kill_daily,
                "kill_reason": self.kill_reason if self._kill_daily else "",
                "loss_streak": self.loss_streak, "timed_halts": timed}

    def load_state(self, st: dict[str, Any]) -> list[str]:
        """Restaure ce qui doit survivre à un redémarrage (même journée UTC uniquement) :
        arrêt journalier, série de pertes, pauses temporisées. Les arrêts techniques
        (latence, rejets) ne sont pas restaurés : redémarrer est l'action de l'opérateur."""
        restored: list[str] = []
        if st.get("day") != _utc_day(self._wall_s()):
            return restored
        self.day_start_equity = float(st.get("day_start_equity", self.day_start_equity))
        self.day_peak_equity = max(float(st.get("day_peak_equity", self.day_peak_equity)),
                                   self.equity)
        self.loss_streak = int(st.get("loss_streak", 0))
        restored.append(f"début de journée {self.day_start_equity:,.2f}")
        if st.get("daily_kill"):
            self.kill(str(st.get("kill_reason") or "arrêt journalier"), daily=True)
            restored.append("ARRÊT JOURNALIER maintenu")
        for k, (reason, remaining) in (st.get("timed_halts") or {}).items():
            if remaining > 0:
                self.halt(k, reason, remaining)
                restored.append(f"pause « {reason} »")
        return restored

    def status(self) -> dict[str, object]:
        return {
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "equity": round(self.equity, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_drawdown": round(self.daily_drawdown, 2),
            "latency_ms": round(self.last_latency_ms, 2),
            "daily_limit": round(self.daily_loss_limit, 2),
            "halts": list(self.halts.values()),
            "rejections": {k.value: v for k, v in self.rejections.items() if v},
        }
