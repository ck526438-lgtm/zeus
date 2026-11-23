"""Apprentissage continu (mémoire de l'IA, journal) et robustesse du mode LIVE MT5."""
from __future__ import annotations

import asyncio
import csv
from types import SimpleNamespace as NS

import numpy as np
import pytest

from zeus_hft.config.settings import Mode, Settings
from zeus_hft.core.orderbook import BookTop
from zeus_hft.core.types import Decision, EventKind, Order, OrderEvent, OrderType, Side, Urgency
from zeus_hft.execution.journal import TradeJournal
from zeus_hft.execution.order_manager import OrderManager
from zeus_hft.execution.sim_exchange import SimExchange
from zeus_hft.feeds.mt5_gateway import MT5Gateway
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager
from zeus_hft.strategies.hft_strategy import OBIMomentumStrategy
from zeus_hft.strategies.online_model import OnlineLogit

from .test_mt5_gateway import FakeMT5


def test_model_persists_between_sessions(tmp_path) -> None:
    rng = np.random.default_rng(0)
    m = OnlineLogit(2, horizon=1, min_samples=100)
    mid = 100.0
    for _ in range(2000):
        x = rng.normal(size=2)
        m.step(x, mid)
        mid += 0.01 if x[0] > 0 else -0.01
    path = tmp_path / "ia.json"
    m.save(path)

    m2 = OnlineLogit(2, horizon=1, min_samples=100)
    assert m2.load(path)
    assert m2.samples == m.samples and m2.accuracy == pytest.approx(m.accuracy)
    assert np.allclose(m2.w, m.w)
    assert m2.confidence > 0.5                  # reprend avec sa confiance acquise
    assert not OnlineLogit(3).load(path)        # autre nombre de features : refusé
    assert not OnlineLogit(2).load(tmp_path / "absent.json")


def test_strategy_model_file(tmp_path) -> None:
    s = Settings(_env_file=None, symbol="XAUUSD",  # type: ignore[call-arg]
                 model_file=str(tmp_path / "m_{symbol}.json"))
    st = OBIMomentumStrategy(s)
    st.attach_model_file(s.resolved_model_file())
    assert not st.model_loaded
    st.save_model()
    assert (tmp_path / "m_XAUUSD.json").exists()
    st2 = OBIMomentumStrategy(s)
    st2.attach_model_file(s.resolved_model_file())
    assert st2.model_loaded


def test_journal_stats_and_history(tmp_path) -> None:
    path = tmp_path / "trades.csv"
    j = TradeJournal(path, "LIVE", "XAUUSD", window=100, min_trades=10)
    for i in range(12):
        j.record("BUY", 0.01, 100, 101, 5.0, gross=2.0 if i % 3 else -1.0, fees=0.1)
    st = j.stats()
    assert st.trades == 12 and st.wins == 8
    assert st.verdict.startswith("RENTABLE")
    j.close()
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert len(rows) == 12 and rows[0]["mode"] == "LIVE"

    # une nouvelle session reprend l'historique LIVE, mais ignore le DRY_RUN
    j_live = TradeJournal(path, "LIVE", "XAUUSD", min_trades=10)
    j_dry = TradeJournal(path, "DRY_RUN", "XAUUSD", min_trades=10)
    assert j_live.stats().trades == 12 and j_dry.stats().trades == 0
    assert j_dry.stats().verdict.startswith("en apprentissage")
    j_live.close()
    j_dry.close()


def test_journal_losing_verdict() -> None:
    j = TradeJournal(None, "LIVE", "X", min_trades=10)
    for _ in range(20):
        j.record("SELL", 0.01, 100, 101, 1.0, gross=-1.0, fees=0.1)
    assert j.stats().verdict.startswith("perdant")


class Clock:
    def __init__(self) -> None:
        self.ns = 10**12

    def __call__(self) -> int:
        return self.ns


def _top(b: float, a: float) -> BookTop:
    return BookTop(b, 1, a, 1, (a + b) / 2, a - b, (a + b) / 2, 1, 0)


async def test_order_manager_writes_round_trips() -> None:
    clk = Clock()
    sim = SimExchange(point=0.01, contract_size=100.0, latency_ms=1, commission_per_lot=3.5,
                      clock_ns=clk)
    risk = RiskManager(RiskLimits(1000, 1.0, 100, 150), 10_000, clock_ns=clk,
                       wall_clock_s=lambda: clk.ns / 1e9)
    j = TradeJournal(None, "LIVE", "XAUUSD", min_trades=10)
    om = OrderManager("XAUUSD", sim, risk, point=0.01, volume_step=0.01, volume_min=0.01,
                      contract_size=100.0, session_id=1, clock_ns=clk, journal=j)

    async def step(target: float, b: float, a: float, reason: str = "") -> None:
        om.exit_reason = reason
        await om.execute(Decision(target, Urgency.AGGRESSIVE), _top(b, a), clk())
        clk.ns += 2_000_000
        sim.on_market(b, a)

    await step(0.01, 100.0, 100.0, "achat")
    await step(0.0, 101.0, 101.0, "take-profit")           # +1 $ par 0,01 lot × 100
    await step(-0.01, 101.0, 101.0, "vente")
    await step(0.01, 102.0, 102.0, "retournement")          # perte sur le short, puis long
    await step(0.0, 102.5, 102.5, "stop-loss")
    st = j.stats()
    assert st.trades == 3
    recs = list(j.recent)
    assert recs[0] == pytest.approx((100.99 - 100.01) - 2 * 0.035)
    assert recs[1] < 0 and st.wins == 2


async def test_partial_market_fill_releases_order() -> None:
    fake = FakeMT5()
    fake_send = fake.order_send

    def partial(req):  # type: ignore[no-untyped-def]
        res = fake_send(req)
        if req["action"] == fake.TRADE_ACTION_DEAL:
            res.volume = req["volume"] / 2
        return res

    fake.order_send = partial  # type: ignore[method-assign]
    s = Settings(_env_file=None, mode=Mode.LIVE)  # type: ignore[call-arg]
    gw = MT5Gateway(s, mt5=fake)
    gw.connect_blocking()
    events: list[OrderEvent] = []
    gw.set_event_sink(events.append)
    await gw.submit(Order("ZH-1", "XAUUSD", Side.BUY, 0.02, OrderType.MARKET))
    await asyncio.sleep(0.05)
    assert [e.kind for e in events] == [EventKind.ACK, EventKind.FILL, EventKind.CANCELED]
    assert events[1].fill_qty == pytest.approx(0.01)
    gw.shutdown()


async def test_filling_mode_fallback() -> None:
    fake = FakeMT5()
    fake_send = fake.order_send

    def picky(req):  # type: ignore[no-untyped-def]
        if req["action"] == fake.TRADE_ACTION_PENDING and req["type_filling"] != fake.ORDER_FILLING_FOK:
            return NS(retcode=10030, order=0, volume=0, price=0, comment="unsupported filling")
        return fake_send(req)

    fake.order_send = picky  # type: ignore[method-assign]
    gw = MT5Gateway(Settings(_env_file=None, mode=Mode.LIVE), mt5=fake)  # type: ignore[call-arg]
    gw.connect_blocking()
    events: list[OrderEvent] = []
    gw.set_event_sink(events.append)
    await gw.submit(Order("ZH-2", "XAUUSD", Side.BUY, 0.01, OrderType.LIMIT, price=2649.0))
    await asyncio.sleep(0.05)
    assert events[0].kind is EventKind.ACK
    assert gw._filling_pending == fake.ORDER_FILLING_FOK      # retenu pour la suite
    n = len(fake.requests)
    await gw.submit(Order("ZH-3", "XAUUSD", Side.BUY, 0.01, OrderType.LIMIT, price=2648.0))
    await asyncio.sleep(0.05)
    assert len(fake.requests) == n + 1                       # plus d'essais inutiles
    gw.shutdown()
