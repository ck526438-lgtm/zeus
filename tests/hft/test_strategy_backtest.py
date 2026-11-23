from __future__ import annotations

import numpy as np
import pytest

from zeus_hft.backtest.data import load_ticks_csv
from zeus_hft.backtest.simulator import run_backtest
from zeus_hft.config.settings import Settings
from zeus_hft.core.orderbook import OrderBook
from zeus_hft.core.ringbuffer import RingBuffer
from zeus_hft.core.telemetry import LatencyTracker
from zeus_hft.core.types import Tick, Urgency
from zeus_hft.feeds.synthetic import synthetic_updates
from zeus_hft.strategies.hft_strategy import OBIMomentumStrategy
from zeus_hft.strategies.online_model import OnlineLogit


@pytest.fixture
def s() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_ring_buffer() -> None:
    rb = RingBuffer(3)
    for v in (1, 2, 3, 4):
        rb.append(v)
    assert rb.full and len(rb) == 3
    assert rb.last() == 4 and rb.last(2) == 2
    assert rb.to_array().tolist() == [2, 3, 4]
    with pytest.raises(IndexError):
        rb.last(3)


def test_latency_tracker() -> None:
    lt = LatencyTracker("x", capacity=100)
    for v in range(1, 101):
        lt.record(v * 1000)
    st = lt.stats()
    assert st is not None and st.count == 100 and st.p50_us == pytest.approx(50.5)


def test_settings_validation() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, momentum_fast=50, momentum_slow=10)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Settings(_env_file=None, order_qty=1.0, max_net_inventory=0.1)  # type: ignore[call-arg]


def test_online_model_learns_predictable_signal() -> None:
    rng = np.random.default_rng(0)
    m = OnlineLogit(2, horizon=1, min_samples=200)
    mid = 100.0
    x = np.zeros(2)
    for _ in range(4000):
        x[0] = rng.normal()
        x[1] = rng.normal()          # bruit pur
        m.step(x.copy(), mid)
        mid += 0.01 * (1 if x[0] + 0.3 * rng.normal() > 0 else -1)
    assert m.accuracy > 0.8
    assert m.confidence == 1.0
    assert abs(m.w[1]) > 5 * abs(m.w[2])           # le signal utile domine le bruit


def test_online_model_stays_humble_on_noise() -> None:
    rng = np.random.default_rng(1)
    m = OnlineLogit(3, horizon=1, min_samples=200)
    mid = 100.0
    for _ in range(4000):
        m.step(rng.normal(size=3), mid)
        mid += 0.01 * rng.choice([-1, 1])
    assert m.confidence < 0.5          # pas de faux avantage sur du bruit


def test_strategy_warmup_and_entry(s: Settings) -> None:
    s = s.model_copy(update={"warmup_ticks": 5, "decision_cooldown_ms": 0})
    strat = OBIMomentumStrategy(s, use_model=False)
    book = OrderBook("XAUUSD")
    out = None
    for i in range(40):
        px = 2650 + i * 0.05                           # tendance haussière
        book.apply_snapshot([(px, 9.0), (px - 0.05, 9.0)], [(px + 0.1, 1.0), (px + 0.15, 1.0)], i)
        out = strat.on_tick(book, Tick("XAUUSD", px, px + 0.1), 0.0, 0.0, i * 10**8)
        if i < 4:
            assert out is None
    assert out is not None and out.target_position == s.order_qty
    assert out.urgency is Urgency.AGGRESSIVE           # OBI fort + momentum


def test_strategy_stop_loss(s: Settings) -> None:
    s = s.model_copy(update={"warmup_ticks": 1})
    strat = OBIMomentumStrategy(s, use_model=False)
    book = OrderBook("XAUUSD")
    book.apply_snapshot([(2650.0, 1.0)], [(2650.1, 1.0)], 1)
    strat.on_tick(book, Tick("XAUUSD", 2650.0, 2650.1), 0.0, 0.0, 0)
    book.apply_snapshot([(2649.0, 1.0)], [(2649.1, 1.0)], 2)       # -100 points
    d = strat.on_tick(book, Tick("XAUUSD", 2649.0, 2649.1), 0.01, 2650.1, 10**8)
    assert d is not None and d.target_position == 0.0 and d.reason == "stop-loss"
    assert d.urgency is Urgency.AGGRESSIVE


async def test_backtest_synthetic_end_to_end(s: Settings) -> None:
    rep = await run_backtest(s, synthetic_updates(15_000, seed=11))
    assert rep.ticks == 15_000
    assert rep.fills > 0 and rep.trades > 0
    assert rep.decision_latency is not None and rep.decision_latency.p50_us < 2_000
    assert not rep.killed


async def test_backtest_deterministic(s: Settings) -> None:
    a = await run_backtest(s, synthetic_updates(5_000, seed=5))
    b = await run_backtest(s, synthetic_updates(5_000, seed=5))
    assert a.net_pnl == pytest.approx(b.net_pnl) and a.fills == b.fills


async def test_backtest_csv(tmp_path, s: Settings) -> None:
    p = tmp_path / "ticks.csv"
    lines = ["time_msc,bid,ask,last,volume"]
    rng = np.random.default_rng(3)
    mid = 2650.0
    for i in range(3000):
        mid += rng.normal(0, 0.03)
        lines.append(f"{1767225600000 + i * 200},{mid - 0.08:.2f},{mid + 0.08:.2f},0,0")
    lines.append("bad,row,,,")
    p.write_text("\n".join(lines))
    ups = list(load_ticks_csv(p, "XAUUSD"))
    assert len(ups) == 3000 and ups[0].bids is None
    rep = await run_backtest(s, iter(ups))
    assert rep.ticks == 3000
