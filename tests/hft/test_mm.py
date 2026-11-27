"""Market making crypto (MEXC) : parsing, signature, file d'attente, cotation, moteur."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from zeus_hft.mm.backtest import load_events, run_mm_backtest, synthetic_events
from zeus_hft.mm.config import MMSettings
from zeus_hft.mm.engine import MMEngine
from zeus_hft.mm.mexc import (MexcGateway, MexcRest, order_side_code, parse_contract,
                              parse_snapshot, parse_ws, sign)
from zeus_hft.mm.quoting import QuoteEngine
from zeus_hft.mm.quotes import QuoteManager
from zeus_hft.mm.runner import EventRecorder, scan_pairs
from zeus_hft.mm.sim import BUY, SELL, QueueSimExchange
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager


def S(**kw: object) -> MMSettings:
    base = {"tick": 0.01, "contract_size": 1.0, "quote_size": 1.0, "max_inventory": 3.0,
            "min_requote_ms": 0.0}
    base.update(kw)
    return MMSettings(_env_file=None, **base)  # type: ignore[arg-type]


def risk(clock: dict) -> RiskManager:
    return RiskManager(RiskLimits(1000, 3.0, 100, 1e9, max_consecutive_rejects=1000, max_reject_rate=1.0),
                       1000, clock_ns=lambda: clock["ms"] * 1_000_000, wall_clock_s=lambda: clock["ms"] / 1000)


# ------------------------------------------------------------------ parsing / signature MEXC
def test_parse_ws_depth_and_deals() -> None:
    d = parse_ws({"channel": "push.depth", "symbol": "SOL_USDT", "ts": 5,
                  "data": {"asks": [[150.02, 30, 2]], "bids": [], "version": 42}})
    assert d == [("depth", 5, [], [(150.02, 30.0)], 42)]
    t = parse_ws({"channel": "push.deal", "ts": 9, "data": [{"p": 150.01, "v": 3, "T": 2, "t": 8},
                                                             {"p": 150.02, "v": 1, "T": 1, "t": 9}]})
    assert t == [("trade", 8, 150.01, 3.0, -1), ("trade", 9, 150.02, 1.0, 1)]
    assert parse_ws({"channel": "pong", "data": 1}) == []


def test_parse_snapshot_contract_and_sides() -> None:
    snap = parse_snapshot({"data": {"asks": [[1.2, 5, 1]], "bids": [[1.1, 7, 2]], "version": 9,
                                    "timestamp": 100}}, 0)
    assert snap == ("snap", 100, [(1.1, 7.0)], [(1.2, 5.0)], 9)
    c = parse_contract({"data": {"priceUnit": 0.001, "contractSize": 0.1, "minVol": 1, "volUnit": 1,
                                 "makerFeeRate": 0, "takerFeeRate": 0.0002}})
    assert c["tick"] == 0.001 and c["contract_size"] == 0.1 and c["maker_fee"] == 0
    assert [order_side_code(BUY, False), order_side_code(BUY, True),
            order_side_code(SELL, False), order_side_code(SELL, True)] == [1, 2, 3, 4]


def test_signature_matches_spec() -> None:
    expected = hmac.new(b"sec", b"key1700000000000{\"a\":1}", hashlib.sha256).hexdigest()
    assert sign("sec", "key", "1700000000000", '{"a":1}') == expected


def test_rest_signing_get_sorted_and_post_json() -> None:
    calls: list = []

    def transport(method, url, headers, data):  # type: ignore[no-untyped-def]
        calls.append((method, url, headers, data))
        return {"success": True}

    s = S(ZEUS_MM_API_KEY="key", ZEUS_MM_API_SECRET="sec")
    r = MexcRest(s, transport)
    r.private("GET", "/p", {"symbol": "SOL_USDT", "a": 1})
    m, url, h, _ = calls[-1]
    assert url.endswith("/p?a=1&symbol=SOL_USDT")
    assert h["Signature"] == sign("sec", "key", h["Request-Time"], "a=1&symbol=SOL_USDT")
    r.private("POST", "/o", body={"symbol": "SOL_USDT", "vol": 1})
    m, url, h, data = calls[-1]
    assert data == b'{"symbol":"SOL_USDT","vol":1}'
    assert h["Signature"] == sign("sec", "key", h["Request-Time"], data.decode())


# ------------------------------------------------------------------ file d'attente
def _sim(clock: dict) -> tuple[QueueSimExchange, list]:
    sim = QueueSimExchange(maker_fee=0.0, contract_size=1.0, latency_ms=10, jitter_ms=0,
                           clock_ms=lambda: clock["ms"])
    ev: list = []
    sim.on_event = lambda *a: ev.append(a)
    levels = {(BUY, 100.0): 20.0, (SELL, 100.01): 15.0}
    sim.set_level_fn(lambda side, px: levels.get((side, px), 0.0))
    sim.best_bid, sim.best_ask = 100.0, 100.01
    return sim, ev


async def test_queue_position_must_be_consumed_before_fill() -> None:
    clock = {"ms": 0}
    sim, ev = _sim(clock)
    await sim.place("b1", BUY, 100.0, 5, False)
    clock["ms"] = 10
    sim.on_book(100.0, 100.01)
    assert ev[-1][:2] == ("ack", "b1") and sim.orders["b1"].ahead == 20
    sim.on_trade(100.0, 15, SELL)                   # 15 sur 20 devant nous : rien pour nous
    assert not [e for e in ev if e[0] == "fill"]
    sim.on_trade(100.0, 8, SELL)                    # 5 restants devant, puis 3 pour nous
    fills = [e for e in ev if e[0] == "fill"]
    assert fills == [("fill", "b1", 3, 100.0, 0.0)]
    sim.on_trade(99.99, 1, SELL)                    # le prix nous traverse : reste exécuté
    assert sum(e[2] for e in ev if e[0] == "fill") == 5 and "b1" not in sim.orders


async def test_post_only_reject_and_improve_price_has_no_queue() -> None:
    clock = {"ms": 0}
    sim, ev = _sim(clock)
    await sim.place("x", BUY, 100.01, 1, False)     # traverserait le spread
    await sim.place("y", SELL, 100.005, 1, False)   # améliore l'ask : personne devant
    clock["ms"] = 10
    sim.process()
    assert ("rejected", "x", 0, 0, 0) in ev and sim.orders["y"].ahead == 0


async def test_cancel_latency_race_fill_wins() -> None:
    clock = {"ms": 0}
    sim, ev = _sim(clock)
    await sim.place("b", BUY, 100.0, 1, False)
    clock["ms"] = 10
    sim.process()
    await sim.cancel("b")                            # arrive à t=20
    sim.on_trade(99.99, 5, SELL)                     # exécuté avant l'arrivée de l'annulation
    clock["ms"] = 20
    sim.process()
    kinds = [e[0] for e in ev]
    assert "fill" in kinds and kinds[-1] == "cancel_rejected"


async def test_book_moving_through_order_fills_it() -> None:
    clock = {"ms": 0}
    sim, ev = _sim(clock)
    await sim.place("b", BUY, 100.0, 2, False)
    clock["ms"] = 10
    sim.process()
    sim.on_book(99.98, 100.0)                       # le meilleur ask descend sur notre bid
    assert [e for e in ev if e[0] == "fill"][0][2] == 2


# ------------------------------------------------------------------ cotation
def test_quote_post_only_skew_and_inventory_cap() -> None:
    qe = QuoteEngine(S(skew_ticks=2.0, vol_mult=0.0))
    q0 = qe.compute(1000, 100.0, 100.01, 100.005, 0.0)
    assert q0.bid == 100.0 and q0.ask == 100.01            # au meilleur prix, jamais à travers
    q_long = qe.compute(1100, 100.0, 100.01, 100.005, 2.0)
    assert q_long.ask <= q0.ask and q_long.bid <= q0.bid   # long : on baisse pour revendre
    q_max = qe.compute(1200, 100.0, 100.01, 100.005, 3.0)
    assert q_max.bid is None and q_max.ask is not None and "inventaire max" in q_max.reason


def test_toxic_flow_and_sweep_pull_quotes() -> None:
    qe = QuoteEngine(S(toxic_flow=0.6, sweep_levels=3, pull_ms=1000))
    for i in range(10):
        qe.on_trade(1000 + i, 100.01, 5, BUY)               # acheteurs agressifs
    q = qe.compute(1100, 100.0, 100.01, 100.005, 0.0)
    assert q.ask is None and q.bid is not None
    qe2 = QuoteEngine(S(sweep_levels=3, pull_ms=1000, toxic_flow=1.0))   # isole le balayage
    qe2.on_trade(1000, 99.97, 50, SELL, levels_crossed=4)   # balayage de 4 niveaux
    assert qe2.compute(1500, 99.96, 99.97, 99.965, 0.0).bid is None
    assert qe2.compute(2100, 99.96, 99.97, 99.965, 0.0).bid is not None


def test_adverse_selection_withdraws_everything() -> None:
    qe = QuoteEngine(S(markout_s=1.0, max_adverse_bps=2.0))
    for i in range(12):
        qe.markout.on_fill(i * 10, 100.0, BUY)                # on achète…
    qe.compute(5000, 99.9, 99.91, 99.905, 0.0)               # …et le prix baisse de ~10 bps
    q = qe.compute(5001, 99.9, 99.91, 99.905, 0.0)
    assert q.bid is None and q.ask is None and "sélection adverse" in q.reason


async def test_quote_manager_requote_threshold_and_closing_size() -> None:
    clock = {"ms": 0}
    sim, ev = _sim(clock)
    s = S(requote_ticks=2.0)
    qm = QuoteManager(s, sim, risk(clock), lambda: clock["ms"])
    sim.on_event = qm.on_event
    qe = QuoteEngine(S(vol_mult=0.0))
    await qm.update(qe.compute(0, 100.0, 100.01, 100.005, 0.0))
    clock["ms"] = 10
    sim.process()
    bid = qm.orders[qm.side_order[BUY]]
    assert bid.status == "live" and bid.price == 100.0
    q = qe.compute(20, 100.01, 100.02, 100.015, 0.0)          # +1 tick : on garde notre file
    assert await qm.update(q) == 0
    q2 = qe.compute(30, 100.03, 100.04, 100.035, 0.0)         # +3 ticks : on bouge
    assert await qm.update(q2) >= 1 and bid.status == "canceling"
    qm.inventory = -1.0                                       # short 1 : le bid devient fermeture
    clock["ms"] = 50
    sim.process()
    await qm.update(qe.compute(60, 100.03, 100.04, 100.035, -1.0))
    new_bid = qm.orders[qm.side_order[BUY]]
    assert new_bid.closing and new_bid.qty == 1.0


# ------------------------------------------------------------------ moteur et backtest
async def test_engine_pulls_quotes_on_book_gap() -> None:
    clock = {"ms": 0}
    sim, _ = _sim(clock)
    s = S()
    eng = MMEngine(s, sim, risk(clock), lambda: clock["ms"], sim=sim)
    sim.on_event = eng.qm.on_event
    await eng.on_event(("snap", 0, [(100.0, 10)], [(100.01, 10)], 5))
    clock["ms"] = 20
    await eng.on_event(("depth", 20, [(100.0, 9)], [], 6))
    assert eng.qm.side_order[BUY] is not None
    await eng.on_event(("depth", 30, [(100.0, 8)], [], 9))    # trou de version 6 -> 9
    assert eng.need_resync and all(o.status == "canceling" for o in eng.qm.orders.values())


async def test_backtest_synthetic_deterministic() -> None:
    a = await run_mm_backtest(S(max_inventory=5), synthetic_events(600, seed=4))
    b = await run_mm_backtest(S(max_inventory=5), synthetic_events(600, seed=4))
    assert a.fills > 0 and a.fills == b.fills and a.pnl == pytest.approx(b.pnl)
    assert a.max_inventory <= 5 and 0 < a.uptime <= 1


async def test_event_recorder_roundtrip(tmp_path) -> None:
    rec = EventRecorder(str(tmp_path), "SOL_USDT")
    evs = list(synthetic_events(30, seed=1))
    for e in evs:
        rec.put(e)
    rec.close()
    (f,) = list(tmp_path.glob("*.jsonl.gz"))
    back = list(load_events(f))
    assert len(back) == len(evs) and back[0][0] == "snap" and back[1] == evs[1]


# ------------------------------------------------------------------ passerelle MEXC
class FakeRest:
    def __init__(self) -> None:
        self.calls: list = []
        self.key = self.secret = "x"
        self.reply: dict = {"success": True, "data": {"orderId": "777", "ts": 1}}

    def private(self, method, path, params=None, body=None):  # type: ignore[no-untyped-def]
        self.calls.append((method, path, params, body))
        return self.reply

    def public(self, path, params=None):  # type: ignore[no-untyped-def]
        if path.endswith("detail"):
            return {"data": [{"symbol": "AAA_USDT", "priceUnit": 0.001, "makerFeeRate": 0},
                             {"symbol": "BBB_USDT", "priceUnit": 0.01, "makerFeeRate": 0.0002},
                             {"symbol": "CCC_USDT", "priceUnit": 0.0001, "makerFeeRate": 0}]}
        return {"data": [{"symbol": "AAA_USDT", "bid1": 1.000, "ask1": 1.004, "amount24": 2e7},
                         {"symbol": "BBB_USDT", "bid1": 10.0, "ask1": 10.05, "amount24": 9e7},
                         {"symbol": "CCC_USDT", "bid1": 1.0, "ask1": 1.0001, "amount24": 9e7}]}


async def test_gateway_place_cancel_and_private_events() -> None:
    fake = FakeRest()
    gw = MexcGateway(S(symbol="SOL_USDT", leverage=3), fake)  # type: ignore[arg-type]
    ev: list = []
    gw.on_event = lambda *a: ev.append(a)
    await gw.place("ZM1-1", BUY, 150.0, 2, False)
    m, path, _, body = fake.calls[-1]
    assert path == "/api/v1/private/order/create"
    assert body == {"symbol": "SOL_USDT", "price": 150.0, "vol": 2, "side": 1, "type": 2,
                    "openType": 1, "externalOid": "ZM1-1", "leverage": 3}
    assert ev[-1] == ("ack", "ZM1-1") and gw.order_ids["ZM1-1"] == "777"
    fake.reply = {"success": True, "data": [{"orderId": "777", "errorCode": 0}]}
    await gw.cancel("ZM1-1")
    assert fake.calls[-1][1:4:2] == ("/api/v1/private/order/cancel", ["777"]) and ev[-1][0] == "canceled"
    await gw.cancel("ZM1-9")                             # pas encore d'orderId : par externalOid
    assert fake.calls[-1][1] == "/api/v1/private/order/cancel_with_external"
    gw.handle_private({"channel": "push.personal.order.deal",
                       "data": {"symbol": "SOL_USDT", "externalOid": "ZM1-1", "vol": 1, "price": 150.0, "fee": 0}})
    gw.handle_private({"channel": "push.personal.order", "data": {"symbol": "SOL_USDT", "orderId": 777, "state": 5}})
    gw.handle_private({"channel": "push.personal.order.deal",
                       "data": {"symbol": "ETH_USDT", "externalOid": "ZM1-1", "vol": 9, "price": 1}})
    assert ev[-2] == ("fill", "ZM1-1", 1.0, 150.0, 0.0) and ev[-1] == ("rejected", "ZM1-1")
    fake.reply = {"success": False, "code": 2005, "message": "post only"}
    await gw.place("ZM1-2", SELL, 150.0, 1, True)
    assert fake.calls[-1][3]["side"] == 4 and "leverage" not in fake.calls[-1][3]
    assert ev[-1] == ("rejected", "ZM1-2")


def test_scan_pairs_filters_fees_spread_volume() -> None:
    rows = scan_pairs(FakeRest(), min_turnover=1e7)  # type: ignore[arg-type]
    assert [r["symbol"] for r in rows] == ["AAA_USDT"]   # BBB : frais maker ; CCC : spread 1 tick
    assert rows[0]["spread_ticks"] == 4
