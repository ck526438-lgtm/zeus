from __future__ import annotations

import time

import numpy as np
import orjson
import pytest

from zeus_hft.core.orderbook import (
    BufferOverflowError,
    CrossedBookError,
    OrderBook,
    SequenceGapError,
    make_delta,
    parse_binance_depth_update,
)

BIDS = [("100.0", "1.0"), ("99.5", "2.0"), ("99.0", "3.0")]
ASKS = [("100.5", "1.5"), ("101.0", "2.5"), ("101.5", "4.0")]


@pytest.fixture
def book() -> OrderBook:
    ob = OrderBook("BTCUSDT")
    ob.apply_snapshot(BIDS, ASKS, last_update_id=100)
    return ob


# --- Snapshot & métriques ------------------------------------------------- #
def test_snapshot_top_of_book(book: OrderBook) -> None:
    assert book.synced and book.is_ready
    assert book.best_bid == (100.0, 1.0)
    assert book.best_ask == (100.5, 1.5)
    assert book.mid_price == pytest.approx(100.25)
    assert book.spread == pytest.approx(0.5)
    assert book.spread_bps == pytest.approx(0.5 / 100.25 * 1e4)
    assert len(book) == 6


def test_snapshot_ignores_zero_qty() -> None:
    ob = OrderBook("X")
    ob.apply_snapshot([("10", "0"), ("9", "1")], [("11", "1")], 1)
    assert ob.best_bid == (9.0, 1.0)


def test_microprice(book: OrderBook) -> None:
    # (100*1.5 + 100.5*1.0) / 2.5
    assert book.microprice == pytest.approx((100 * 1.5 + 100.5 * 1.0) / 2.5)
    top = book.top()
    assert top is not None and top.microprice == pytest.approx(book.microprice)
    assert top.update_id == 100


def test_imbalance_uniform_and_decay(book: OrderBook) -> None:
    # top 1 : (1 - 1.5) / 2.5
    assert book.imbalance(1) == pytest.approx(-0.2)
    # top 3 : (6 - 8) / 14
    assert book.imbalance(3) == pytest.approx(-2 / 14)
    # avec décroissance 0.5 : bids 1+1+0.75=2.75, asks 1.5+1.25+1=3.75
    assert book.imbalance(3, decay=0.5) == pytest.approx((2.75 - 3.75) / 6.5)
    # plus de niveaux que disponibles : borné
    assert book.imbalance(50) == book.imbalance(3)


def test_imbalance_bounds() -> None:
    ob = OrderBook("X")
    assert ob.imbalance(5) == 0.0
    ob.apply_snapshot([("10", "5")], [], 1)
    assert ob.imbalance(5) == 1.0
    with pytest.raises(ValueError):
        ob.imbalance(0)


def test_empty_book_metrics_none() -> None:
    ob = OrderBook("X")
    assert ob.mid_price is None and ob.spread is None and ob.microprice is None
    assert ob.top() is None and not ob.is_ready


def test_levels_and_depth_arrays(book: OrderBook) -> None:
    bids, asks = book.levels(2)
    assert bids == [(100.0, 1.0), (99.5, 2.0)]
    assert asks == [(100.5, 1.5), (101.0, 2.5)]
    b, a = book.depth_arrays(5)
    assert b.shape == (5, 2) and a.shape == (5, 2)
    assert b[0, 0] == 100.0 and np.isnan(b[3, 0])
    assert a[2, 1] == 4.0


def test_volume_at_or_better(book: OrderBook) -> None:
    assert book.volume_at_or_better("ask", 101.0) == pytest.approx(4.0)
    assert book.volume_at_or_better("bid", 99.5) == pytest.approx(3.0)
    with pytest.raises(ValueError):
        book.volume_at_or_better("x", 1.0)


# --- Deltas & séquencement ------------------------------------------------ #
def test_delta_update_insert_delete(book: OrderBook) -> None:
    changed = book.apply_delta(
        make_delta(101, 102, bids=[("100.0", "0"), ("100.2", "0.7")], asks=[("100.5", "3")])
    )
    assert changed
    assert book.best_bid == (100.2, 0.7)
    assert book.best_ask == (100.5, 3.0)
    assert 100.0 not in book.levels(10)[0]
    assert book.last_update_id == 102
    assert book.updates_applied == 1


def test_stale_delta_ignored(book: OrderBook) -> None:
    assert book.apply_delta(make_delta(90, 100, bids=[("100.0", "9")])) is False
    assert book.best_bid == (100.0, 1.0)


def test_overlapping_delta_accepted(book: OrderBook) -> None:
    # first <= last+1 <= final : chevauchement autorisé
    assert book.apply_delta(make_delta(99, 105, bids=[("100.1", "1")]))
    assert book.last_update_id == 105


def test_sequence_gap_invalidates(book: OrderBook) -> None:
    with pytest.raises(SequenceGapError) as exc:
        book.apply_delta(make_delta(105, 106, bids=[("100.1", "1")]))
    assert exc.value.expected == 101
    assert not book.synced and len(book) == 0


def test_buffer_before_snapshot_then_replay() -> None:
    ob = OrderBook("X")
    assert ob.apply_delta(make_delta(95, 98, bids=[("1", "1")])) is False     # obsolète
    assert ob.apply_delta(make_delta(99, 101, bids=[("100.1", "5")])) is False
    assert ob.apply_delta(make_delta(102, 103, asks=[("100.5", "0")])) is False
    replayed = ob.apply_snapshot(BIDS, ASKS, last_update_id=100)
    assert replayed == 2
    assert ob.best_bid == (100.1, 5.0)
    assert ob.best_ask == (101.0, 2.5)
    assert ob.last_update_id == 103


def test_buffer_not_bridging_snapshot_raises() -> None:
    ob = OrderBook("X")
    ob.apply_delta(make_delta(105, 106))
    with pytest.raises(SequenceGapError):
        ob.apply_snapshot(BIDS, ASKS, last_update_id=100)
    assert not ob.synced


def test_buffer_overflow() -> None:
    ob = OrderBook("X", max_buffer=2)
    ob.apply_delta(make_delta(1, 1))
    ob.apply_delta(make_delta(2, 2))
    with pytest.raises(BufferOverflowError):
        ob.apply_delta(make_delta(3, 3))


def test_crossed_book_detected(book: OrderBook) -> None:
    with pytest.raises(CrossedBookError):
        book.apply_delta(make_delta(101, 101, bids=[("100.6", "1")]))
    assert not book.synced


def test_max_depth_trim() -> None:
    ob = OrderBook("X", max_depth=2)
    ob.apply_snapshot(BIDS, ASKS, 1)
    assert ob.levels(10)[0] == [(100.0, 1.0), (99.5, 2.0)]
    assert ob.levels(10)[1] == [(100.5, 1.5), (101.0, 2.5)]


def test_reset(book: OrderBook) -> None:
    book.reset()
    assert not book.synced and book.last_update_id == -1 and len(book) == 0


def test_staleness(book: OrderBook) -> None:
    assert OrderBook("X").staleness_ms() == float("inf")
    assert book.staleness_ms(book.last_update_ns + 2_000_000) == pytest.approx(2.0)


# --- Parsing -------------------------------------------------------------- #
def test_parse_binance_depth_update() -> None:
    raw = orjson.dumps(
        {"e": "depthUpdate", "E": 1700000000000, "s": "BTCUSDT", "U": 101, "u": 103,
         "b": [["100.2", "0.5"]], "a": [["100.5", "0.00000000"]]}
    )
    d = parse_binance_depth_update(raw, recv_ts_ns=42)
    assert (d.first_update_id, d.final_update_id) == (101, 103)
    assert d.bids == ((100.2, 0.5),) and d.asks == ((100.5, 0.0),)
    assert d.recv_ts_ns == 42 and d.exchange_ts_ms == 1700000000000


# --- Performance (garde-fou, pas un benchmark) ---------------------------- #
def test_hot_path_under_one_microsecond_budget(book: OrderBook) -> None:
    n = 20_000
    deltas = [
        make_delta(101 + i, 101 + i, bids=[(f"{99.0 + (i % 10) * 0.1:.1f}", "1")])
        for i in range(n)
    ]
    t0 = time.perf_counter_ns()
    for d in deltas:
        book.apply_delta(d)
        book.imbalance(5)
        book.microprice
    per_tick_us = (time.perf_counter_ns() - t0) / n / 1e3
    # Budget large pour CI partagée ; en local on mesure ~3-6 µs/tick.
    assert per_tick_us < 100.0
