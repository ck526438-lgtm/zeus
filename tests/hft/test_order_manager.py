from __future__ import annotations

import pytest

from zeus_hft.core.orderbook import BookTop
from zeus_hft.core.types import Decision, OrderStatus, OrderType, Side, Urgency
from zeus_hft.execution.order_manager import OrderManager
from zeus_hft.execution.sim_exchange import SimExchange
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager


class Clock:
    def __init__(self) -> None:
        self.ns = 10**12

    def __call__(self) -> int:
        return self.ns

    def advance_ms(self, ms: float) -> None:
        self.ns += int(ms * 1e6)


def top(bid: float, ask: float) -> BookTop:
    return BookTop(bid, 1.0, ask, 1.0, (bid + ask) / 2, ask - bid, (bid + ask) / 2, 1, 0)


@pytest.fixture
def env() -> tuple[OrderManager, SimExchange, RiskManager, Clock]:
    clk = Clock()
    sim = SimExchange(point=0.01, contract_size=100.0, latency_ms=10, slippage_points=1,
                      commission_per_lot=3.5, clock_ns=clk)
    risk = RiskManager(RiskLimits(1000, 0.05, 100, 150), 10_000.0, clock_ns=clk,
                       wall_clock_s=lambda: clk.ns / 1e9)
    om = OrderManager("XAUUSD", sim, risk, point=0.01, volume_step=0.01, volume_min=0.01,
                      contract_size=100.0, session_id=1234, min_requote_ms=0, clock_ns=clk)
    return om, sim, risk, clk


async def tick(sim: SimExchange, clk: Clock, bid: float, ask: float, ms: float = 11) -> None:
    clk.advance_ms(ms)
    sim.on_market(bid, ask)


def test_deterministic_client_ids(env) -> None:
    om, *_ = env
    assert om.next_client_id() == "ZHya-1"
    assert om.next_client_id() == "ZHya-2"
    assert len("ZHya-99999") <= 31


def test_round_qty(env) -> None:
    om, *_ = env
    assert om.round_qty(0.0149) == 0.01
    assert om.round_qty(-0.027) == -0.02
    assert om.round_qty(0.004) == 0.0


async def test_market_order_fill_and_pnl(env) -> None:
    om, sim, _, clk = env
    n = await om.execute(Decision(0.02, Urgency.AGGRESSIVE), top(100.0, 100.1), clk())
    assert n == 1 and om.has_inflight
    # tant que l'ordre est en vol, aucune nouvelle action (anti-doublon)
    assert await om.execute(Decision(0.02, Urgency.AGGRESSIVE), top(100.0, 100.1), clk()) == 0
    await tick(sim, clk, 100.0, 100.1)
    assert om.position == pytest.approx(0.02)
    assert om.avg_price == pytest.approx(100.11)           # ask + 1 point de slippage
    assert om.fees == pytest.approx(0.02 * 3.5)
    assert not om.orders
    # fermeture
    await om.execute(Decision(0.0, Urgency.AGGRESSIVE), top(101.0, 101.1), clk())
    await tick(sim, clk, 101.0, 101.1)
    assert om.position == 0.0
    assert om.realized_pnl == pytest.approx(0.02 * (100.99 - 100.11) * 100)


async def test_passive_limit_then_replace_then_fill(env) -> None:
    om, sim, _, clk = env
    await om.execute(Decision(0.01, Urgency.PASSIVE), top(100.0, 100.2), clk())
    await tick(sim, clk, 100.0, 100.2)
    (o,) = om.orders.values()
    assert o.status is OrderStatus.WORKING and o.order_type is OrderType.LIMIT and o.price == 100.0
    # le bid monte : cancel/replace au nouveau prix
    assert await om.execute(Decision(0.01, Urgency.PASSIVE), top(100.05, 100.2), clk()) == 1
    assert o.status is OrderStatus.PENDING_REPLACE
    await tick(sim, clk, 100.05, 100.2)
    assert o.status is OrderStatus.WORKING and o.price == pytest.approx(100.05)
    # même prix : rien à faire
    assert await om.execute(Decision(0.01, Urgency.PASSIVE), top(100.05, 100.2), clk()) == 0
    # le marché traverse notre prix : exécution maker au prix limite
    await tick(sim, clk, 99.9, 100.04)
    assert om.position == pytest.approx(0.01) and om.avg_price == pytest.approx(100.05)
    assert not om.orders


async def test_opposite_side_order_is_cancelled(env) -> None:
    om, sim, _, clk = env
    await om.execute(Decision(0.01, Urgency.PASSIVE), top(100.0, 100.2), clk())
    await tick(sim, clk, 100.0, 100.2)
    assert await om.execute(Decision(-0.01, Urgency.PASSIVE), top(100.0, 100.2), clk()) == 1
    await tick(sim, clk, 100.0, 100.2)
    assert not om.orders                                  # annulé
    await om.execute(Decision(-0.01, Urgency.PASSIVE), top(100.0, 100.2), clk())
    (o,) = om.orders.values()
    assert o.side is Side.SELL and o.price == 100.2


async def test_partial_fills() -> None:
    clk = Clock()
    sim = SimExchange(point=0.01, contract_size=100.0, latency_ms=1, max_fill_qty=0.01, clock_ns=clk)
    risk = RiskManager(RiskLimits(1000, 1.0, 100, 150), 10_000.0, clock_ns=clk,
                       wall_clock_s=lambda: clk.ns / 1e9)
    om = OrderManager("X", sim, risk, point=0.01, volume_step=0.01, volume_min=0.01,
                      contract_size=100.0, session_id=1, clock_ns=clk)
    await om.execute(Decision(0.03, Urgency.PASSIVE), top(100.0, 100.2), clk())
    await tick(sim, clk, 100.0, 100.2, 2)
    await tick(sim, clk, 99.8, 99.95, 2)                   # traverse : 0.01 exécuté
    (o,) = om.orders.values()
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.filled_qty == pytest.approx(0.01)
    assert om.position == pytest.approx(0.01)
    await tick(sim, clk, 99.8, 99.95, 2)
    await tick(sim, clk, 99.8, 99.95, 2)
    assert om.position == pytest.approx(0.03) and not om.orders


async def test_risk_blocks_new_order(env) -> None:
    om, _, risk, clk = env
    risk.kill("test")
    assert await om.execute(Decision(0.01, Urgency.AGGRESSIVE), top(100, 100.1), clk()) == 0
    assert om.order_count == 0


async def test_position_flip_avg_price(env) -> None:
    om, sim, _, clk = env
    await om.execute(Decision(0.01, Urgency.AGGRESSIVE), top(100.0, 100.0), clk())
    await tick(sim, clk, 100.0, 100.0)
    om.risk.limits.max_net_inventory = 1.0
    await om.execute(Decision(-0.02, Urgency.AGGRESSIVE), top(101.0, 101.0), clk())
    await tick(sim, clk, 101.0, 101.0)
    assert om.position == pytest.approx(-0.02)
    assert om.avg_price == pytest.approx(100.99)
    assert om.realized_pnl == pytest.approx(0.01 * (100.99 - 100.01) * 100)


async def test_flatten(env) -> None:
    om, sim, _, clk = env
    await om.execute(Decision(0.02, Urgency.AGGRESSIVE), top(100.0, 100.1), clk())
    await tick(sim, clk, 100.0, 100.1)
    await om.flatten(clk())
    await tick(sim, clk, 100.0, 100.1)
    assert om.position == 0.0


async def test_gateway_exception_becomes_reject(env) -> None:
    om, sim, _, clk = env

    async def boom(order):  # type: ignore[no-untyped-def]
        raise ConnectionError("réseau coupé")

    sim.submit = boom  # type: ignore[method-assign]
    assert await om.execute(Decision(0.01, Urgency.AGGRESSIVE), top(100, 100.1), clk()) == 1
    assert not om.orders and om.position == 0.0
