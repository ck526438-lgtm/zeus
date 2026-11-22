"""Gestion du risque : la partie la plus importante de Zeus.

Calcule la taille de position et bloque les entrées quand les limites sont
atteintes (perte journalière, série de pertes, spread, horaires, cadence).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def lot_size(balance: float, risk_pct: float, sl_distance: float, tick_value: float,
             tick_size: float, vol_min: float, vol_max: float, vol_step: float) -> float:
    """Lot tel qu'une perte au stop = risk_pct % du capital."""
    if sl_distance <= 0 or tick_size <= 0 or tick_value <= 0:
        return 0.0
    risk_money = balance * risk_pct / 100.0
    loss_per_lot = sl_distance / tick_size * tick_value
    raw = risk_money / loss_per_lot
    steps = math.floor(raw / vol_step + 1e-9)
    lots = round(steps * vol_step, 8)
    if lots < vol_min:
        return 0.0          # le risque serait dépassé même avec le lot minimum
    return min(lots, vol_max)


@dataclass
class RiskState:
    day: str = ""
    start_balance: float = 0.0
    realized_pnl: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    paused_until: datetime | None = None
    last_entry: dict[str, datetime] = field(default_factory=dict)


class RiskManager:
    def __init__(self, params: dict, sessions_utc: list | None = None):
        self.p = params
        self.sessions = sessions_utc or []
        self.state = RiskState()

    def new_day_check(self, now: datetime, balance: float):
        day = now.strftime("%Y-%m-%d")
        if day != self.state.day:
            self.state = RiskState(day=day, start_balance=balance,
                                   last_entry=self.state.last_entry)

    def in_session(self, now: datetime) -> bool:
        if not self.sessions:
            return True
        return any(a <= now.hour < b for a, b in self.sessions)

    def can_open(self, symbol: str, now: datetime, equity: float, spread_points: float) -> tuple[bool, str]:
        s, p = self.state, self.p
        if s.paused_until and now < s.paused_until:
            return False, f"pause jusqu'à {s.paused_until:%H:%M}"
        if not self.in_session(now):
            return False, "hors session"
        if s.start_balance > 0:
            dd = (s.start_balance - equity) / s.start_balance * 100
            if dd >= p["max_daily_loss_pct"]:
                return False, f"perte journalière max atteinte ({dd:.2f}%)"
        if s.trades_today >= p["max_daily_trades"]:
            return False, "nombre max de trades du jour"
        if spread_points > p["max_spread_points"]:
            return False, f"spread trop large ({spread_points:.0f} pts)"
        last = s.last_entry.get(symbol)
        if last and (now - last).total_seconds() < p["cooldown_seconds"]:
            return False, "cooldown"
        return True, "ok"

    def on_open(self, symbol: str, now: datetime):
        self.state.trades_today += 1
        self.state.last_entry[symbol] = now

    def on_close(self, pnl: float, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        self.state.realized_pnl += pnl
        if pnl < 0:
            self.state.consecutive_losses += 1
            if self.state.consecutive_losses >= self.p["max_consecutive_losses"]:
                self.state.paused_until = now + timedelta(minutes=self.p["pause_minutes_after_losses"])
                self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses = 0

    def levels(self, direction: int, price: float, atr: float) -> tuple[float, float]:
        sl = price - direction * self.p["sl_atr_mult"] * atr
        tp = price + direction * self.p["tp_atr_mult"] * atr
        return sl, tp

    def trail(self, direction: int, entry: float, price: float, current_sl: float, atr: float) -> float | None:
        """Nouveau SL (break-even puis stop suiveur) ou None si inchangé."""
        gain = (price - entry) * direction
        new_sl = current_sl
        if gain >= self.p["breakeven_atr"] * atr:
            be = entry
            new_sl = max(new_sl, be) if direction == 1 else min(new_sl, be)
            trail = price - direction * self.p["trailing_atr"] * atr
            new_sl = max(new_sl, trail) if direction == 1 else min(new_sl, trail)
        if (new_sl - current_sl) * direction > 1e-9:
            return new_sl
        return None
