"""Passerelle MT5 testée contre un faux terminal (le vrai paquet n'existe que sous Windows)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from zeus_hft.config.settings import Mode, Settings
from zeus_hft.core.types import EventKind, Order, OrderEvent, OrderType, Side
from zeus_hft.feeds.mt5_gateway import MT5Gateway


class FakeMT5:
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE = 1, 5, 7, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    BOOK_TYPE_SELL, BOOK_TYPE_BUY = 1, 2
    ACCOUNT_TRADE_MODE_DEMO = 0
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_STATE_CANCELED = 2

    def __init__(self, fail_init: int = 0) -> None:
        self.fail_init = fail_init
        self.requests: list[dict[str, Any]] = []
        self.pending: dict[int, Any] = {}
        self.deals: dict[int, list[Any]] = {}
        self.hist: dict[int, Any] = {}
        self._ticket = 100
        self.tick = NS(bid=2650.0, ask=2650.2, last=0.0, volume=0, volume_real=0.0, time_msc=1)
        self.positions: list[Any] = []

    def initialize(self, **kw: Any) -> bool:
        if self.fail_init > 0:
            self.fail_init -= 1
            return False
        return True

    def last_error(self) -> tuple[int, str]:
        return (-1, "fake")

    def shutdown(self) -> None: ...
    def symbol_select(self, s: str, b: bool) -> bool: return True
    def symbol_info(self, s: str) -> Any:
        return NS(point=0.01, trade_contract_size=100.0, volume_step=0.01, volume_min=0.01,
                  filling_mode=2)
    def account_info(self) -> Any: return NS(login=1, server="Demo", trade_mode=0, equity=5000.0)
    def market_book_add(self, s: str) -> bool: return True
    def market_book_release(self, s: str) -> bool: return True
    def market_book_get(self, s: str) -> Any:
        return (NS(type=1, price=2650.3, volume=3, volume_dbl=3.0),
                NS(type=1, price=2650.2, volume=1, volume_dbl=1.0),
                NS(type=2, price=2650.0, volume=5, volume_dbl=5.0),
                NS(type=2, price=2649.9, volume=2, volume_dbl=2.0))
    def symbol_info_tick(self, s: str) -> Any: return self.tick
    def terminal_info(self) -> Any: return NS(connected=True, ping_last=42_000)
    def positions_get(self, symbol: str) -> Any: return tuple(self.positions)

    def order_send(self, req: dict[str, Any]) -> Any:
        self.requests.append(req)
        self._ticket += 1
        t = self._ticket
        a = req["action"]
        if a == self.TRADE_ACTION_DEAL:
            self.deals[t] = [NS(volume=req["volume"], price=req["price"], commission=-0.35, fee=0.0)]
            return NS(retcode=10009, order=t, volume=req["volume"], price=req["price"], comment="done")
        if a == self.TRADE_ACTION_PENDING:
            self.pending[t] = NS(ticket=t, volume_initial=req["volume"], volume_current=req["volume"])
            return NS(retcode=10008, order=t, volume=0.0, price=0.0, comment="placed")
        if a == self.TRADE_ACTION_REMOVE:
            ok = self.pending.pop(req["order"], None) is not None
            return NS(retcode=10009 if ok else 10013, order=req["order"], volume=0, price=0,
                      comment="")
        if a == self.TRADE_ACTION_MODIFY:
            return NS(retcode=10009, order=req["order"], volume=0, price=0, comment="")
        raise AssertionError(a)

    def orders_get(self, symbol: str) -> Any: return tuple(self.pending.values())
    def history_deals_get(self, ticket: int) -> Any: return tuple(self.deals.get(ticket, ()))
    def history_orders_get(self, ticket: int) -> Any:
        h = self.hist.get(ticket)
        return (h,) if h else ()


@pytest.fixture
def s() -> Settings:
    return Settings(_env_file=None, mode=Mode.LIVE, reconnect_base_delay=0.001,  # type: ignore[call-arg]
                    reconnect_max_delay=0.002)


def test_connect_with_retry_and_specs(s: Settings) -> None:
    fake = FakeMT5(fail_init=2)
    gw = MT5Gateway(s, mt5=fake)
    gw.connect_blocking()
    assert gw.connected and gw.symbol_specs()["contract_size"] == 100.0
    assert gw._filling_market == FakeMT5.ORDER_FILLING_IOC
    assert gw.is_demo() and gw.heartbeat_blocking() == pytest.approx(42.0)


def test_poll_market_reads_dom_sorted(s: Settings) -> None:
    gw = MT5Gateway(s, mt5=FakeMT5())
    gw.connect_blocking()
    upd = gw.poll_market(-1)
    assert upd is not None
    assert upd.bids == [(2650.0, 5.0), (2649.9, 2.0)]
    assert upd.asks == [(2650.2, 1.0), (2650.3, 3.0)]
    assert upd.tick.bid == 2650.0 and upd.tick.recv_ts_ns > 0


def test_current_position_filters_magic(s: Settings) -> None:
    fake = FakeMT5()
    fake.positions = [NS(magic=s.magic, type=0, volume=0.02, price_open=2600.0),
                      NS(magic=s.magic, type=1, volume=0.01, price_open=2610.0),
                      NS(magic=999, type=0, volume=1.0, price_open=1.0)]
    gw = MT5Gateway(s, mt5=fake)
    pos, avg = gw.current_position()
    assert pos == pytest.approx(0.01) and avg == pytest.approx(2590.0)


async def test_market_and_limit_lifecycle(s: Settings) -> None:
    fake = FakeMT5()
    gw = MT5Gateway(s, mt5=fake)
    gw.connect_blocking()
    events: list[OrderEvent] = []
    gw.set_event_sink(events.append)

    mkt = Order("ZH1-1", "XAUUSD", Side.BUY, 0.01, OrderType.MARKET)
    await gw.submit(mkt)
    await asyncio.sleep(0.05)
    assert [e.kind for e in events] == [EventKind.ACK, EventKind.FILL]
    assert events[1].fill_price == 2650.2 and events[1].fee == pytest.approx(0.35)
    req = fake.requests[-1]
    assert req["comment"] == "ZH1-1" and req["magic"] == s.magic and req["type_filling"] == 1

    events.clear()
    lim = Order("ZH1-2", "XAUUSD", Side.SELL, 0.02, OrderType.LIMIT, price=2651.0)
    await gw.submit(lim)
    await asyncio.sleep(0.05)
    assert events[0].kind is EventKind.ACK
    lim.exchange_order_id = events[0].exchange_order_id
    ticket = int(lim.exchange_order_id or 0)

    await gw.replace(lim, 2650.9, 0.02)
    await asyncio.sleep(0.05)
    assert events[-1].kind is EventKind.REPLACED and events[-1].new_price == 2650.9

    # fill partiel détecté par la réconciliation
    fake.deals[ticket] = [NS(volume=0.01, price=2650.9, commission=-0.35, fee=0.0)]
    evs = await gw.reconcile([lim])
    assert len(evs) == 1 and evs[0].kind is EventKind.FILL and evs[0].fill_qty == pytest.approx(0.01)

    await gw.cancel(lim)
    await asyncio.sleep(0.05)
    assert events[-1].kind is EventKind.CANCELED
    gw.shutdown()


async def test_rejected_order(s: Settings) -> None:
    fake = FakeMT5()
    fake.order_send = lambda req: NS(retcode=10019, order=0, volume=0, price=0,  # type: ignore[method-assign]
                                     comment="no money")
    gw = MT5Gateway(s, mt5=fake)
    gw.connect_blocking()
    events: list[OrderEvent] = []
    gw.set_event_sink(events.append)
    await gw.submit(Order("ZH1-9", "XAUUSD", Side.BUY, 0.01, OrderType.MARKET))
    await asyncio.sleep(0.05)
    assert events[0].kind is EventKind.REJECTED and "no money" in events[0].reason


async def test_feed_thread_pushes_updates(s: Settings) -> None:
    fake = FakeMT5()
    gw = MT5Gateway(s.model_copy(update={"poll_interval_ms": 1.0}), mt5=fake)
    gw.connect_blocking()
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    stop = asyncio.Event()
    task = asyncio.create_task(gw.run(q, stop))
    upd = await asyncio.wait_for(q.get(), 2.0)
    assert upd.tick.ask == 2650.2
    fake.tick = NS(bid=2650.1, ask=2650.3, last=0.0, volume=0, volume_real=0.0, time_msc=2)
    for _ in range(50):
        upd = await asyncio.wait_for(q.get(), 2.0)
        if upd.tick.exchange_ts_ms == 2:
            break
    assert upd.tick.bid == 2650.1
    stop.set()
    await asyncio.wait_for(task, 3.0)
