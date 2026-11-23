from __future__ import annotations

import pytest

from zeus_hft.risk.risk_manager import RiskLimits, RiskManager, RiskReason


class Clock:
    def __init__(self) -> None:
        self.ns = 1_000_000_000_000
        self.s = 1_767_225_600.0  # 2026-01-01 UTC

    def __call__(self) -> int:
        return self.ns

    def wall(self) -> float:
        return self.s


@pytest.fixture
def clk() -> Clock:
    return Clock()


def make(clk: Clock, **kw: float) -> RiskManager:
    lim = RiskLimits(max_daily_drawdown=kw.get("dd", 100.0), max_net_inventory=kw.get("inv", 0.05),
                     max_orders_per_second=int(kw.get("ops", 5)), max_latency_ms=150.0,
                     latency_breaches_to_kill=3, max_data_staleness_ms=1000.0)
    return RiskManager(lim, 10_000.0, clock_ns=clk, wall_clock_s=clk.wall)


def test_allows_normal_order(clk: Clock) -> None:
    r = make(clk)
    assert r.check_order(0.01, 0.0, 0.0).allowed


def test_inventory_limit_counts_working_orders(clk: Clock) -> None:
    r = make(clk)
    assert r.check_order(0.02, 0.03, 0.0).allowed
    d = r.check_order(0.02, 0.03, 0.01)
    assert not d.allowed and d.reason is RiskReason.INVENTORY
    assert r.check_order(-0.02, 0.03, 0.01).allowed       # réduction : toujours autorisée


def test_daily_drawdown_kills_and_is_sticky(clk: Clock) -> None:
    killed: list[str] = []
    r = make(clk)
    r._on_kill = killed.append
    r.update_equity(10_050.0)              # nouveau plus haut
    r.update_equity(9_949.0)               # -101 depuis le plus haut
    assert r.killed and killed
    d = r.check_order(0.01, 0.0, 0.0)
    assert not d.allowed and d.reason is RiskReason.KILLED
    r.update_equity(10_100.0)              # même si l'equity remonte, le kill reste
    assert not r.check_order(0.01, 0.0, 0.0).allowed
    assert r.check_order(-0.01, 0.02, 0.0).allowed        # mais on peut aplatir


def test_drawdown_resets_next_utc_day(clk: Clock) -> None:
    r = make(clk)
    r.update_equity(9_950.0)
    assert r.daily_drawdown == pytest.approx(50.0)
    clk.s += 86_400
    r.update_equity(9_950.0)
    assert r.daily_drawdown == 0.0 and r.daily_pnl == 0.0


def test_throttle_sliding_window(clk: Clock) -> None:
    r = make(clk, ops=3)
    for _ in range(3):
        assert r.check_order(0.01, 0.0, 0.0).allowed
    d = r.check_action()
    assert not d.allowed and d.reason is RiskReason.THROTTLE
    clk.ns += 1_000_000_001
    assert r.check_action().allowed


def test_latency_blocks_then_kills(clk: Clock) -> None:
    r = make(clk)
    r.record_latency(200.0)
    d = r.check_order(0.01, 0.0, 0.0)
    assert not d.allowed and d.reason is RiskReason.LATENCY and not r.killed
    r.record_latency(20.0)                 # retour à la normale
    assert r.check_order(0.01, 0.0, 0.0).allowed
    for _ in range(3):
        r.record_latency(500.0)
    assert r.killed and "latence" in r.kill_reason


def test_stale_data_rejected(clk: Clock) -> None:
    r = make(clk)
    d = r.check_order(0.01, 0.0, 0.0, data_age_ms=5_000)
    assert not d.allowed and d.reason is RiskReason.STALE_DATA


def test_zero_qty_rejected(clk: Clock) -> None:
    assert make(clk).check_order(0.0, 0.0, 0.0).reason is RiskReason.INVALID_QTY
