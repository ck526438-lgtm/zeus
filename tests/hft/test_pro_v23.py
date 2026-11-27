"""v2.3 : tendance H1/M15, gestion des sorties, lots dynamiques, série de pertes,
SL serveur, télémétrie, calendrier MT5, Monte Carlo, walk-forward."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from zeus_hft.backtest.montecarlo import load_journal_pnls, monte_carlo
from zeus_hft.backtest.simulator import BacktestReport
from zeus_hft.backtest.walkforward import run_walkforward
from zeus_hft.config.settings import Mode, Settings
from zeus_hft.core.orderbook import BookTop
from zeus_hft.core.types import Decision, Tick, Urgency
from zeus_hft.execution.journal import TradeJournal
from zeus_hft.execution.order_manager import OrderManager
from zeus_hft.execution.sim_exchange import SimExchange
from zeus_hft.execution.sizing import lots_for_risk
from zeus_hft.feeds.base import MarketUpdate
from zeus_hft.feeds.mt5_gateway import MT5Gateway
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager
from zeus_hft.strategies.exits import ExitManager
from zeus_hft.strategies.features import MarketFeatures
from zeus_hft.strategies.news_file import load_news_csv
from zeus_hft.strategies.trend import TFBars, TrendFilter

from .test_mt5_gateway import FakeMT5

PT = 0.01


def S(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def top(b: float, a: float) -> BookTop:
    return BookTop(b, 1, a, 1, (a + b) / 2, a - b, (a + b) / 2, 1, 0)


# ------------------------------------------------------------------ tendance
def test_tfbars_ema_direction_and_adx() -> None:
    up = TFBars(60, 5, 20)
    up.seed([(100 + i, 101 + i, 99.5 + i, 100.8 + i) for i in range(80)])
    assert up.direction == 1 and up.adx > 40 and up.plus_di > up.minus_di
    flat = TFBars(60, 5, 20)
    flat.seed([(100, 101, 99, 100 + (0.3 if i % 2 else -0.3)) for i in range(80)])
    assert flat.direction == 0 and flat.adx < 20


def test_tfbars_aggregates_m1_into_h1() -> None:
    tf = TFBars(60, 3, 6)
    for m in range(0, 180):                       # 3 heures de M1
        tf.on_m1(m, 100 + m * 0.01, 100.5 + m * 0.01, 99.5 + m * 0.01, 100.2 + m * 0.01)
    assert tf.bars == 2                           # la 3e heure est encore en cours


def test_trend_filter_rules() -> None:
    s = S(trend_ema_fast=5, trend_ema_slow=20, trend_min_bars=30, mr_max_adx=22)
    tf = TrendFilter(s)
    assert tf.allows_momentum(1) == (False, "tendance H1/M15 pas encore calculée")
    bull = [(100 + i, 101 + i, 99.5 + i, 100.8 + i) for i in range(80)]
    tf.h1.seed(bull)
    tf.m15.seed(bull)
    assert tf.allows_momentum(1)[0] and not tf.allows_momentum(-1)[0]
    ok, why = tf.allows_meanrev(1)
    assert not ok and "ADX" in why                # marché en tendance : pas de mean-reversion


def test_rollover_regime() -> None:
    s = S(server_utc_offset_hours=0, atr_period=3, atr_baseline_bars=10, trend_filter=False,
          meanrev_sessions_utc="00:00-23:59", session_opens_utc="")
    f = MarketFeatures(s, PT)
    ts = 1_767_571_200_000 + 21 * 3_600_000 + 30 * 60_000 - 10 * 60_000    # lundi 21:20 UTC
    for i in range(600):
        f.update(ts + i * 1000, 2650 - 0.1 + (i % 7) * 0.01, 2650 + 0.1)
    assert f.regime.label == "ROLLOVER"


# ------------------------------------------------------------------ sorties
def _exit(**kw: object) -> ExitManager:
    s = S(commission_per_lot=7.0, breakeven_at_r=1.0, partial_close_frac=0.5, trail_atr=1.0, **kw)
    return ExitManager(s, PT, "momentum")


def test_exit_breakeven_partial_trailing() -> None:
    ex = _exit()
    ex.start(2650.0, 0.04, stop_dist=1.0, tp1_dist=1.6)
    assert ex.stop == pytest.approx(2649.0)
    assert ex.update(2650.5, 1.0, top(2650.4, 2650.6), 0.04, 0.01, 0.01) is None
    ex.update(2651.05, 1.0, top(2651.0, 2651.1), 0.04, 0.01, 0.01)         # +1R : break-even
    assert ex.be and ex.stop == pytest.approx(2650.0 + 0.07 + 0.01)         # entrée + commission
    d = ex.update(2651.7, 1.0, top(2651.6, 2651.8), 0.04, 0.01, 0.01)       # TP1
    assert d is not None and d.target_position == pytest.approx(0.02) and "partielle 50" in d.reason
    ex.update(2653.0, 1.0, top(2652.9, 2653.1), 0.02, 0.01, 0.01)           # le reliquat court
    assert ex.stop == pytest.approx(2652.0) and ex.stop_reason == "trailing stop"
    d2 = ex.update(2651.9, 1.0, top(2651.8, 2652.0), 0.02, 0.01, 0.01)      # retour sous le trailing
    assert d2 is not None and d2.target_position == 0.0 and d2.reason == "trailing stop"


def test_exit_min_lot_closes_everything_at_tp1_and_short_side() -> None:
    ex = _exit()
    ex.start(2650.0, -0.01, stop_dist=1.0, tp1_dist=1.0)
    assert ex.stop == pytest.approx(2651.0)
    d = ex.update(2648.9, 1.0, top(2648.8, 2649.0), -0.01, 0.01, 0.01)
    assert d is not None and d.target_position == 0.0 and d.reason == "TP momentum"
    ex2 = _exit()
    ex2.start(2650.0, -0.01, stop_dist=1.0, tp1_dist=3.0)
    d2 = ex2.update(2651.1, 1.0, top(2651.0, 2651.2), -0.01, 0.01, 0.01)
    assert d2 is not None and d2.urgency is Urgency.AGGRESSIVE and d2.reason == "SL momentum"


# ------------------------------------------------------------------ taille des lots
def test_lots_for_risk() -> None:
    # 10 000 × 0,5 % = 50 ; stop 100 pts × 1 $/pt/lot -> 0,5 lot
    assert lots_for_risk(10_000, 0.5, 100, 1.0, 0.01, 0.01, 100, 5) == pytest.approx(0.5)
    assert lots_for_risk(10_000, 0.5, 100, 1.0, 0.01, 0.01, 100, 0.2) == pytest.approx(0.2)
    assert lots_for_risk(10_000, 0.5, 333, 1.0, 0.01, 0.01, 100, 5) == pytest.approx(0.15)  # arrondi bas
    assert lots_for_risk(100, 0.5, 100, 1.0, 0.01, 0.01, 100, 5) == 0.0     # lot min trop risqué
    assert lots_for_risk(150, 0.5, 100, 1.0, 0.01, 0.01, 100, 5) == pytest.approx(0.01)  # <= +50 %
    assert S().money_per_point(1.0) == pytest.approx(1.0)                   # XAUUSD 1 lot


# ------------------------------------------------------------------ risque
def test_loss_streak_pause_then_expiry() -> None:
    clk = {"ns": 10**12}
    lim = RiskLimits(1000, 1.0, 100, 150, max_consecutive_losses=3, loss_streak_pause_min=60)
    r = RiskManager(lim, 10_000, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: 1_767_571_200.0)
    for pnl in (-1, -2):
        r.record_trade(pnl)
    r.record_trade(+1)                                # un gain remet le compteur à zéro
    for _ in range(3):
        r.record_trade(-1)
    assert not r.check_order(0.01, 0, 0).allowed and "3 pertes" in next(iter(r.halts.values()))
    clk["ns"] += 61 * 60 * 10**9
    assert r.check_order(0.01, 0, 0).allowed and not r.halts


def test_loss_streak_until_tomorrow() -> None:
    lim = RiskLimits(1000, 1.0, 100, 150, max_consecutive_losses=2, loss_streak_pause_min=0)
    r = RiskManager(lim, 10_000, clock_ns=lambda: 1, wall_clock_s=lambda: 1_767_571_200.0)
    r.record_trade(-1)
    r.record_trade(-1)
    assert r.killed and "demain" in r.kill_reason


# ------------------------------------------------------------------ ordres
def _om(risk_pct: float = 0.0, journal: TradeJournal | None = None, gateway=None):  # type: ignore[no-untyped-def]
    clk = {"ns": 10**12}
    sim = gateway or SimExchange(point=PT, contract_size=100, latency_ms=25, slippage_points=2,
                                 commission_per_lot=3.5, clock_ns=lambda: clk["ns"])
    risk = RiskManager(RiskLimits(10_000, 1.0, 100, 150), 10_000, clock_ns=lambda: clk["ns"],
                       wall_clock_s=lambda: clk["ns"] / 1e9)
    om = OrderManager("XAUUSD", sim, risk, point=PT, volume_step=0.01, volume_min=0.01,
                      contract_size=100, session_id=1, clock_ns=lambda: clk["ns"], journal=journal,
                      risk_per_trade_pct=risk_pct, money_per_point_1lot=1.0, max_lots=1.0,
                      server_sl_mult=1.5)
    return om, sim, clk


async def test_dynamic_size_and_trade_telemetry() -> None:
    j = TradeJournal(None, "BACKTEST", "XAUUSD", min_trades=10)
    om, sim, clk = _om(risk_pct=0.5, journal=j)
    await om.execute(Decision(0.01, Urgency.AGGRESSIVE, stop_points=200, strategy="momentum"),
                     top(2650.0, 2650.3), clk["ns"])
    (o,) = om.orders.values()
    assert o.qty == pytest.approx(0.25)               # 50 $ / (200 pts × 1 $) = 0,25 lot
    clk["ns"] += 30_000_000
    sim.on_market(2650.0, 2650.3)
    assert om.position == pytest.approx(0.25)
    await om.execute(Decision(0.0, Urgency.AGGRESSIVE), top(2651.0, 2651.3), clk["ns"])
    clk["ns"] += 30_000_000
    sim.on_market(2651.0, 2651.3)
    (rec,) = j.records
    assert rec.strategy == "momentum" and rec.entry_spread_pts == pytest.approx(30.0)
    assert rec.slippage_pts == pytest.approx(4.0)     # 2 pts à l'entrée + 2 pts à la sortie
    assert rec.exec_latency_ms == pytest.approx(30.0)


async def test_protective_sl_only_tightens() -> None:
    calls: list[tuple[float, bool]] = []

    class GW(SimExchange):
        async def modify_sl(self, sl: float, is_long: bool) -> int:
            calls.append((sl, is_long))
            return 1

    clk = {"ns": 10**12}
    gw = GW(point=PT, contract_size=100, latency_ms=1, clock_ns=lambda: clk["ns"])
    om, sim, _ = _om(gateway=gw)
    om._clock = lambda: clk["ns"]                     # type: ignore[method-assign]
    om.set_position(0.02, 2650.0)
    assert await om.update_protective_sl(2650.08, top(2651.0, 2651.2))
    assert calls[-1] == (pytest.approx(2650.08 - 0.05), True)
    clk["ns"] += 2 * 10**9
    assert not await om.update_protective_sl(2650.0, top(2651.0, 2651.2))   # desserrer : refusé
    clk["ns"] += 2 * 10**9
    assert await om.update_protective_sl(2650.5, top(2651.0, 2651.2))


def test_gateway_modify_sl_uses_sltp_and_never_loosens() -> None:
    fake = FakeMT5()
    fake.TRADE_ACTION_SLTP = 6  # type: ignore[attr-defined]
    s = S(mode=Mode.LIVE)
    gw = MT5Gateway(s, mt5=fake)
    fake.positions = [NS(ticket=7, magic=s.magic, type=0, volume=0.02, price_open=2650.0, sl=2649.0, tp=0.0),
                      NS(ticket=8, magic=s.magic, type=0, volume=0.01, price_open=2650.0, sl=2650.5, tp=0.0)]
    fake.order_send = lambda req: (fake.requests.append(req) or  # type: ignore[method-assign]
                                   NS(retcode=10009, order=0, volume=0, price=0, comment=""))
    assert gw.modify_sl_blocking(2650.1, True) == 1    # le ticket 8 a déjà un stop plus serré
    assert fake.requests[-1] == {"action": 6, "symbol": "XAUUSD", "position": 7, "sl": 2650.1,
                                 "tp": 0.0, "magic": s.magic}


# ------------------------------------------------------------------ calendrier MT5
def test_news_csv_from_mt5_exporter(tmp_path) -> None:
    p = tmp_path / "zeus_news.csv"
    p.write_text("time_utc;currency;importance;event\n"
                 "2026.12.04 13:30;USD;high;Nonfarm Payrolls\n"
                 "garbage;;;\n"
                 "2026.12.10 19:00;USD;high;Fed Interest Rate Decision\n", encoding="utf-8")
    ev = load_news_csv(p)
    assert len(ev) == 2 and ev[0][1] == "USD Nonfarm Payrolls"
    s = S(server_utc_offset_hours=0, news_before_min=15, news_after_min=15)
    f = MarketFeatures(s, PT)
    f.news.set_file_events(ev)
    assert f.news.blocked(ev[0][0] - 14 * 60_000) and f.news.blocked(ev[0][0] + 14 * 60_000)
    assert not f.news.blocked(ev[0][0] - 16 * 60_000)


# ------------------------------------------------------------------ validation
def test_monte_carlo_robust_vs_fragile() -> None:
    good = [3.0, -1.0] * 50
    rep = monte_carlo(good, sims=2000, equity=1000, seed=3)
    assert rep.pnl_p5 > 0 and rep.prob_loss < 0.01 and "ROBUSTE" in rep.text()
    bad = [1.0, -1.05] * 50
    rep2 = monte_carlo(bad, sims=2000, extra_cost_mean=0.2, equity=1000, seed=3)
    assert rep2.pnl_p50 < 0 and "PERDANT" in rep2.text()
    assert "TROP PETIT" in monte_carlo([5.0] * 10, sims=100).text()


def test_load_journal_pnls(tmp_path) -> None:
    j = TradeJournal(tmp_path / "t.csv", "LIVE", "XAUUSD")
    j.record("BUY", 0.01, 1, 2, 1, 1.0, 0.1, strategy="momentum")
    j.record("BUY", 0.01, 1, 2, 1, -2.0, 0.1, strategy="meanrev")
    j.close()
    assert load_journal_pnls(tmp_path / "t.csv", "LIVE") == pytest.approx([0.9, -2.1])
    assert load_journal_pnls(tmp_path / "t.csv", "LIVE", "meanrev") == pytest.approx([-2.1])


async def test_walkforward_windows_pick_best_in_sample() -> None:
    day = 86_400_000
    ups = [MarketUpdate(Tick("X", 1, 1.1, exchange_ts_ms=i * 3_600_000)) for i in range(24 * 12 + 1)]
    seen: list[tuple[int, float]] = []

    async def fake_bt(s, it, seed_bars=None):  # type: ignore[no-untyped-def]
        ticks = list(it)
        seen.append((len(ticks), s.velocity_entry_atr))
        r = BacktestReport(trades=20)
        # le paramètre 0.35 gagne en échantillon, mais pas hors échantillon : le WFA doit le voir
        r.net_pnl = 10.0 if s.velocity_entry_atr == 0.35 else 1.0
        r.gross_profit, r.gross_loss = (20.0, 10.0) if r.net_pnl > 5 else (3.0, 2.0)
        if len(ticks) <= 2 * 24:                     # période de test
            r.net_pnl = -5.0 if s.velocity_entry_atr == 0.35 else 1.0
        return r

    rep = await run_walkforward(S(), ups, train_days=4, test_days=2,
                                grid={"velocity_entry_atr": [0.35, 0.6]}, backtest=fake_bt)
    assert len(rep.windows) == 4                      # 12 jours : 4 + 2 + 2 + 2 + 2
    assert all(w.params == {"velocity_entry_atr": 0.35} for w in rep.windows)
    assert rep.oos_pnl == pytest.approx(-20.0) and "sur-optimisation" in rep.text()
    assert seen[0][0] == 4 * 24 and day > 0


async def test_backtest_seed_bars_no_lookahead() -> None:
    from zeus_hft.backtest.simulator import run_backtest
    from zeus_hft.feeds.synthetic import synthetic_updates

    s = S(trend_min_bars=10, trend_ema_fast=3, trend_ema_slow=8)
    first = next(iter(synthetic_updates(1))).tick.exchange_ts_ms
    old = [(first - (30 - i) * 3_600_000, 1.0, 2.0, 0.5, 1.5) for i in range(30)]
    future = [(first + 3_600_000, 9.0, 9.0, 9.0, 9.0)]
    rep = await run_backtest(s, synthetic_updates(200), seed_bars={"H1": old + future, "M15": old})
    assert rep.ticks == 200
