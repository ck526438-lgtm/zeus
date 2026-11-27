"""Stratégies momentum / mean-reversion, routeur automatique, sécurités MT5 (v2.2)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from zeus_hft.config.settings import Mode, Settings
from zeus_hft.core.orderbook import BookTop, OrderBook
from zeus_hft.core.types import Decision, EventKind, Order, OrderEvent, OrderType, Side, Tick, Urgency
from zeus_hft.execution.order_manager import OrderManager
from zeus_hft.execution.sim_exchange import SimExchange
from zeus_hft.feeds.mt5_gateway import MT5Gateway
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager, RiskReason
from zeus_hft.strategies.features import MarketFeatures, NewsCalendar, in_windows, parse_windows
from zeus_hft.strategies.mean_reversion import MeanReversion
from zeus_hft.strategies.momentum_breakout import MomentumBreakout
from zeus_hft.strategies.router import StrategyRouter

from .test_mt5_gateway import FakeMT5

PT = 0.01
MON_0800_UTC = 1_767_571_200_000 + 8 * 3_600_000          # lundi 5 janv. 2026 08:00 UTC


def S(**kw: object) -> Settings:
    base = {"server_utc_offset_hours": 0.0, "atr_period": 3, "atr_baseline_bars": 10}
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def top(b: float, a: float) -> BookTop:
    return BookTop(b, 1, a, 1, (a + b) / 2, a - b, (a + b) / 2, 1, 0)


def feed(f: MarketFeatures, start_ms: int, minutes: int, step_ms: int = 1000,
         amp: float = 0.5, mid0: float = 2650.0) -> int:
    """Marché oscillant régulièrement (ATR stable)."""
    ts = start_ms
    for i in range(minutes * 60_000 // step_ms):
        mid = mid0 + (amp if (i // 5) % 2 else -amp) * ((i % 5) / 5)
        f.update(ts, mid - 0.1, mid + 0.1)
        ts += step_ms
    return ts


# ------------------------------------------------------------------ calendrier
def test_windows_and_midnight_wrap() -> None:
    w = parse_windows("07:00-11:00,22:00-02:00")
    assert in_windows(7 * 60, w) and not in_windows(11 * 60, w)
    assert in_windows(23 * 60, w) and in_windows(60, w) and not in_windows(3 * 60, w)


def test_news_calendar() -> None:
    n = NewsCalendar("13:30, 2026-01-07 19:00", before_min=5, after_min=15)
    mon_1330 = 1_767_571_200_000 + 13 * 3_600_000 + 30 * 60_000    # lundi 5 janv. 13:30 UTC
    assert n.blocked(mon_1330 - 4 * 60_000) and n.blocked(mon_1330 + 14 * 60_000)
    assert not n.blocked(mon_1330 + 16 * 60_000)
    assert not n.blocked(mon_1330 - 2 * 86_400_000)                # samedi : pas de news quotidienne
    wed_1900 = 1_767_571_200_000 + 2 * 86_400_000 + 19 * 3_600_000
    assert n.blocked(wed_1900 + 60_000)


# ------------------------------------------------------------------ indicateurs
def test_features_atr_range_velocity_regime() -> None:
    s = S(momentum_sessions_utc="00:00-23:59", session_opens_utc="")
    f = MarketFeatures(s, PT)
    ts = feed(f, MON_0800_UTC, minutes=12)
    assert f.ready and f.atr > 0 and 0.5 < f.vol_ratio < 2.0
    assert f.range_hi is not None and f.range_lo is not None and f.range_hi > f.range_lo
    assert f.regime.label in ("NORMAL", "TENDANCE") and f.regime.momentum_allowed
    # choc de vitesse : saut de 5 ATR en 1 s -> CHOC, plus aucune entrée pendant la pause
    f.update(ts, 2650 + 5 * f.atr, 2650 + 5 * f.atr + 0.2)
    assert f.regime.label == "CHOC" and not f.regime.momentum_allowed


def test_opening_blackout_and_news_regime() -> None:
    s = S(momentum_sessions_utc="00:00-23:59", session_opens_utc="08:20", news_times_utc="08:40")
    f = MarketFeatures(s, PT)
    feed(f, MON_0800_UTC, minutes=21)                 # finit à 08:21 UTC
    assert f.regime.label == "OUVERTURE"
    f2 = MarketFeatures(s, PT)
    feed(f2, MON_0800_UTC + 15 * 60_000, minutes=22)  # finit à 08:37 UTC (3 min avant la news)
    assert f2.regime.label == "NEWS"


# ------------------------------------------------------------------ stratégies
def _momentum_ready() -> tuple[Settings, MarketFeatures, int]:
    s = S(momentum_sessions_utc="00:00-23:59", session_opens_utc="", range_window_s=30,
          min_tp_spread_mult=1.0)
    f = MarketFeatures(s, PT)
    ts = feed(f, MON_0800_UTC, minutes=12)
    return s, f, ts


def test_momentum_breakout_entry_and_exits() -> None:
    s, f, ts = _momentum_ready()
    m = MomentumBreakout(s, PT)
    atr = f.atr
    # accélération : +1,5 ATR en 1 s, au-dessus du micro-range
    hi = (f.range_hi or 2650.0) + 1.5 * atr
    f.update(ts + 1000, hi - 0.1, hi + 0.1)
    d = m.evaluate(f, top(hi - 0.1, hi + 0.1), 0.0, 0.0, 0)
    assert d is not None and d.target_position == s.order_qty and d.urgency is Urgency.AGGRESSIVE
    assert d.stop_points and d.strategy == "momentum"
    # SL serré
    loss = hi - (s.mom_sl_atr + 0.1) * atr
    f.update(ts + 2000, loss - 0.1, loss + 0.1)
    d2 = m.evaluate(f, top(loss - 0.1, loss + 0.1), 0.01, hi, 1)
    assert d2 is not None and d2.target_position == 0.0 and d2.urgency is Urgency.AGGRESSIVE


def test_momentum_refuses_when_spread_eats_tp() -> None:
    s, f, ts = _momentum_ready()
    s2 = s.model_copy(update={"min_tp_spread_mult": 2.5})
    m = MomentumBreakout(s2, PT)
    hi = (f.range_hi or 2650.0) + 1.5 * f.atr
    wide = f.atr * 0.8                         # spread énorme vs TP
    f.update(ts + 1000, hi - wide, hi + wide)
    assert m.evaluate(f, top(hi - wide, hi + wide), 0.0, 0.0, 0) is None
    assert "spread" in m.last_reason


def test_mean_reversion_entry_exit_and_vol_kill() -> None:
    s = S(meanrev_sessions_utc="00:00-23:59", momentum_sessions_utc="", session_opens_utc="",
          mr_ema_ticks=50, min_tp_spread_mult=1.0, mr_max_vol_ratio=5.0, mr_max_atr_points=10_000)
    f = MarketFeatures(s, PT)
    ts = feed(f, MON_0800_UTC, minutes=12, amp=0.3)
    mr = MeanReversion(s, PT)
    assert f.regime.meanrev_allowed
    # extension rapide de ~1,5 ATR au-dessus de l'EMA, puis le prix cale
    px = f.mid
    step = f.atr * 0.3
    for _ in range(5):
        px += step
        ts += 1000
        f.update(ts, px - 0.05, px + 0.05)
        assert mr.evaluate(f, top(px - 0.05, px + 0.05), 0.0, 0.0, 0) is None   # trop rapide
    d = None
    for _ in range(10):                       # l'extension ralentit : entrée contre le mouvement
        ts += 1000
        f.update(ts, px - 0.05, px + 0.05)
        d = mr.evaluate(f, top(px - 0.05, px + 0.05), 0.0, 0.0, 0)
        if d is not None:
            break
    assert d is not None and d.target_position == -s.order_qty and d.urgency is Urgency.PASSIVE
    # en position short : un choc de volatilité force la sortie immédiate
    ts += 1000
    f.update(ts, px + 6 * f.atr, px + 6 * f.atr + 0.1)
    d2 = mr.evaluate(f, top(px + 6 * f.atr, px + 6 * f.atr + 0.1), -0.01, px, 1)
    assert d2 is not None and d2.target_position == 0.0 and d2.urgency is Urgency.AGGRESSIVE


def test_router_owner_cooldown_and_no_grid() -> None:
    s = S(momentum_sessions_utc="00:00-23:59", session_opens_utc="", range_window_s=30,
          min_tp_spread_mult=1.0, exit_cooldown_s=10)
    r = StrategyRouter(s, use_model=False)
    book = OrderBook("XAUUSD")
    ts, mid, now = MON_0800_UTC, 2650.0, 0

    def tick(m: float, pos: float = 0.0, avg: float = 0.0) -> Decision | None:
        nonlocal ts, now
        ts += 1000
        now += 1_000_000_000
        book.apply_snapshot([(m - 0.1, 1.0)], [(m + 0.1, 1.0)], ts)
        return r.on_tick(book, Tick("XAUUSD", m - 0.1, m + 0.1, exchange_ts_ms=ts), pos, avg, now)

    for i in range(12 * 60):
        tick(mid + (0.5 if (i // 5) % 2 else -0.5) * ((i % 5) / 5))
    jump = (r.f.range_hi or mid) + 1.2 * r.f.atr
    d = tick(jump)
    assert d is not None and d.target_position == s.order_qty and r.owner is r.mom
    # en position : seul le propriétaire décide, jamais de renfort (cible = 1 lot max)
    d2 = tick(jump + 0.5, 0.01, jump)
    assert d2 is None or abs(d2.target_position) <= s.order_qty
    # la position se ferme -> pause après sortie
    d3 = tick(jump, 0.0, 0.0)
    assert d3 is not None and d3.target_position == 0.0 and "pause" in d3.reason


# ------------------------------------------------------------------ risque
def test_risk_spread_halt_rejects_and_daily_pct() -> None:
    clk = {"ns": 10**12, "s": 1_767_571_200.0}
    lim = RiskLimits(1000, 0.01, 100, 150, max_spread_points=35, max_daily_loss_pct=2.0,
                     max_consecutive_rejects=3)
    r = RiskManager(lim, 10_000, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: clk["s"])
    assert r.check_order(0.01, 0, 0, spread_points=40).reason is RiskReason.SPREAD
    assert r.check_order(-0.01, 0.01, 0, spread_points=80).allowed       # sortie toujours permise
    r.halt("flux", "flux figé")
    assert r.check_order(0.01, 0, 0).reason is RiskReason.HALTED
    r.resume("flux")
    assert r.check_order(0.01, 0, 0).allowed
    for _ in range(3):
        r.record_execution_error("10019 no money")
    assert r.killed and "erreurs" in r.kill_reason


def test_daily_kill_lifts_next_day() -> None:
    clk = {"s": 1_767_571_200.0}
    lim = RiskLimits(1000, 0.01, 100, 150, max_daily_loss_pct=2.0)
    r = RiskManager(lim, 10_000, clock_ns=lambda: 1, wall_clock_s=lambda: clk["s"])
    assert r.daily_loss_limit == pytest.approx(200.0)              # 2 % < 1000
    r.update_equity(9_790)
    assert r.killed and "demain" in r.kill_reason
    clk["s"] += 86_400
    r.update_equity(9_790)
    assert not r.killed                                           # reprise automatique


# ------------------------------------------------------------------ ordres / MT5
async def test_order_manager_server_sl_and_stops_level() -> None:
    clk = {"ns": 10**12}
    sim = SimExchange(point=PT, contract_size=100, latency_ms=1, clock_ns=lambda: clk["ns"])
    risk = RiskManager(RiskLimits(1000, 0.01, 100, 150), 10_000, clock_ns=lambda: clk["ns"],
                       wall_clock_s=lambda: clk["ns"] / 1e9)
    om = OrderManager("XAUUSD", sim, risk, point=PT, volume_step=0.01, volume_min=0.01,
                      contract_size=100, session_id=1, clock_ns=lambda: clk["ns"],
                      stops_level_points=30, server_sl_mult=1.5)
    await om.execute(Decision(0.01, Urgency.AGGRESSIVE, stop_points=100, strategy="momentum"),
                     top(2650.0, 2650.2), clk["ns"])
    (o,) = om.orders.values()
    assert o.sl == pytest.approx(2650.0 - 1.5)                    # 150 points sous le bid
    clk["ns"] += 2_000_000
    sim.on_market(2650.0, 2650.2)
    # ordre limite de vente : au moins 30 points au-dessus du bid (stops level)
    await om.execute(Decision(0.0, Urgency.PASSIVE, limit_price=2650.1), top(2650.0, 2650.2), clk["ns"])
    (o2,) = om.orders.values()
    assert o2.reduce_only and o2.price == pytest.approx(2650.3) and o2.sl is None


async def test_hedging_account_closes_by_position_ticket() -> None:
    fake = FakeMT5()
    fake.account_info = lambda: NS(login=1, server="Demo", trade_mode=0, equity=5000.0,  # type: ignore[method-assign]
                                   margin_mode=2)
    s = Settings(_env_file=None, mode=Mode.LIVE)  # type: ignore[call-arg]
    gw = MT5Gateway(s, mt5=fake)
    gw.connect_blocking()
    assert gw.hedging and not gw.supports_passive_reduce
    fake.positions = [NS(ticket=555, magic=s.magic, type=0, volume=0.01, price_open=2650.0)]
    events: list[OrderEvent] = []
    gw.set_event_sink(events.append)
    await gw.submit(Order("ZH-9", "XAUUSD", Side.SELL, 0.01, OrderType.MARKET, reduce_only=True))
    await asyncio.sleep(0.05)
    req = fake.requests[-1]
    assert req["position"] == 555 and req["type"] == fake.ORDER_TYPE_SELL
    assert [e.kind for e in events] == [EventKind.ACK, EventKind.FILL]
    gw.shutdown()


async def test_market_order_carries_server_sl() -> None:
    fake = FakeMT5()
    gw = MT5Gateway(Settings(_env_file=None, mode=Mode.LIVE), mt5=fake)  # type: ignore[call-arg]
    gw.connect_blocking()
    gw.set_event_sink(lambda e: None)
    await gw.submit(Order("ZH-1", "XAUUSD", Side.BUY, 0.01, OrderType.MARKET, sl=2648.5))
    await asyncio.sleep(0.05)
    assert fake.requests[-1]["sl"] == 2648.5
    gw.shutdown()


async def test_sync_external_records_server_close() -> None:
    from zeus_hft.execution.journal import TradeJournal

    clk = {"ns": 10**12}
    sim = SimExchange(point=PT, contract_size=100, latency_ms=1, clock_ns=lambda: clk["ns"])
    risk = RiskManager(RiskLimits(1000, 0.01, 100, 150), 10_000, clock_ns=lambda: clk["ns"],
                       wall_clock_s=lambda: clk["ns"] / 1e9)
    j = TradeJournal(None, "LIVE", "XAUUSD", min_trades=10)
    om = OrderManager("XAUUSD", sim, risk, point=PT, volume_step=0.01, volume_min=0.01,
                      contract_size=100, session_id=1, clock_ns=lambda: clk["ns"], journal=j)
    om.set_position(0.01, 2650.0)
    om.sync_external(0.0, 0.0, 2648.5, "fermé côté serveur")
    assert om.position == 0.0 and j.trades == 1 and j.net < 0
