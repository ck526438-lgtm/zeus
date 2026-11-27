"""v2.4 : reprise d'état, risque avancé, alertes, métriques, ticks enregistrés,
configuration à chaud, journal infalsifiable, gigue de latence."""
from __future__ import annotations

import csv
import time
import urllib.request

import pytest

from zeus_hft.backtest.data import load_ticks_csv
from zeus_hft.config.settings import Settings
from zeus_hft.core.engine import Engine
from zeus_hft.core.orderbook import BookTop, OrderBook
from zeus_hft.core.types import Decision, Order, OrderType, Side, Tick, Urgency
from zeus_hft.execution.journal import FIELDS, TradeJournal, verify_journal
from zeus_hft.execution.order_manager import OrderManager
from zeus_hft.execution.sim_exchange import SimExchange
from zeus_hft.ops.alerts import Alerter
from zeus_hft.ops.hotconfig import validate_changes
from zeus_hft.ops.metrics import MetricsServer, render_metrics
from zeus_hft.ops.recorder import TickRecorder
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager, RiskReason
from zeus_hft.strategies.router import StrategyRouter

PT = 0.01
DAY0 = 1_767_571_200.0


def S(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def top(b: float, a: float) -> BookTop:
    return BookTop(b, 1, a, 1, (a + b) / 2, a - b, (a + b) / 2, 1, 0)


# ------------------------------------------------------------------ reprise d'état
def test_risk_state_survives_restart_same_day_only() -> None:
    clk = {"ns": 10**12, "s": DAY0 + 3600}
    lim = RiskLimits(500, 1.0, 100, 150, max_consecutive_losses=2, loss_streak_pause_min=60)
    r = RiskManager(lim, 10_000, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: clk["s"])
    r.update_equity(9_400)                              # -600 : arrêt journalier
    assert r.killed
    st = r.to_state()

    r2 = RiskManager(lim, 9_400, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: clk["s"])
    restored = r2.load_state(st)
    assert r2.killed and "ARRÊT JOURNALIER maintenu" in restored     # le redémarrage ne lève rien
    assert r2.day_start_equity == 10_000

    clk["s"] += 86_400                                  # le lendemain : on repart à neuf
    r3 = RiskManager(lim, 9_400, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: clk["s"])
    assert r3.load_state(st) == [] and not r3.killed


def test_timed_halt_restored_with_remaining_time() -> None:
    clk = {"ns": 10**12}
    lim = RiskLimits(500, 1.0, 100, 150, max_consecutive_losses=1, loss_streak_pause_min=10)
    r = RiskManager(lim, 10_000, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: DAY0)
    r.record_trade(-5)
    clk["ns"] += 4 * 60 * 10**9
    st = r.to_state()
    r2 = RiskManager(lim, 10_000, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: DAY0)
    r2.load_state(st)
    assert "pertes" in r2.halts
    clk["ns"] += 7 * 60 * 10**9                         # 4 + 7 > 10 min : levée
    assert r2.check_order(0.01, 0, 0).allowed


# ------------------------------------------------------------------ risque avancé
def test_window_drawdown_reduce_only() -> None:
    clk = {"ns": 10**12}
    lim = RiskLimits(10_000, 1.0, 100, 150, window_dd=100, window_dd_minutes=60)
    r = RiskManager(lim, 10_000, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: DAY0)
    r.update_equity(10_200)
    clk["ns"] += 10 * 60 * 10**9
    r.update_equity(10_090)                             # -110 depuis le plus haut de la fenêtre
    d = r.check_order(0.01, 0, 0)
    assert not d.allowed and d.reason is RiskReason.HALTED and "drawdown" in d.detail
    assert r.check_order(-0.01, 0.01, 0).allowed        # réduction toujours permise


def test_window_drawdown_forgets_old_peaks() -> None:
    clk = {"ns": 10**12}
    lim = RiskLimits(10_000, 1.0, 100, 150, window_dd=100, window_dd_minutes=60)
    r = RiskManager(lim, 10_000, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: DAY0)
    r.update_equity(10_200)
    clk["ns"] += 61 * 60 * 10**9                        # le pic sort de la fenêtre
    r.update_equity(10_090)
    assert not r.halts


def test_notional_limit() -> None:
    lim = RiskLimits(10_000, 5.0, 100, 150, max_notional=200_000)
    r = RiskManager(lim, 10_000, clock_ns=lambda: 1, wall_clock_s=lambda: DAY0)
    assert r.check_order(0.5, 0, 0, notional_per_lot=265_000).allowed      # 132 500
    d = r.check_order(1.0, 0, 0, notional_per_lot=265_000)
    assert not d.allowed and d.reason is RiskReason.NOTIONAL


def test_reject_rate_degraded_mode() -> None:
    clk = {"ns": 10**12}
    halts: list[str] = []
    lim = RiskLimits(10_000, 1.0, 100, 150, max_consecutive_rejects=100, max_reject_rate=0.3)
    r = RiskManager(lim, 10_000, clock_ns=lambda: clk["ns"], wall_clock_s=lambda: DAY0,
                    on_halt=lambda k, reason: halts.append(k))
    for i in range(12):
        clk["ns"] += 10**9
        (r.record_execution_error("requote") if i % 2 else r.record_execution_ok())
    assert "degrade" in r.halts and halts == ["degrade"] and not r.killed


async def test_sizing_respects_notional_cap() -> None:
    clk = {"ns": 10**12}
    sim = SimExchange(point=PT, contract_size=100, latency_ms=1, clock_ns=lambda: clk["ns"])
    risk = RiskManager(RiskLimits(10_000, 5.0, 100, 150, max_notional=200_000), 10_000,
                       clock_ns=lambda: clk["ns"], wall_clock_s=lambda: DAY0)
    om = OrderManager("XAUUSD", sim, risk, point=PT, volume_step=0.01, volume_min=0.01,
                      contract_size=100, session_id=1, clock_ns=lambda: clk["ns"],
                      risk_per_trade_pct=1.0, money_per_point_1lot=1.0, max_lots=5.0)
    await om.execute(Decision(0.01, Urgency.AGGRESSIVE, stop_points=10), top(2650.0, 2650.2), clk["ns"])
    (o,) = om.orders.values()
    assert o.qty == pytest.approx(0.75)               # 200 000 / 265 010 -> 0,75 lot (pas 5 lots)


# ------------------------------------------------------------------ alertes
def test_alerter_cooldown_and_delivery() -> None:
    sent: list[str] = []
    a = Alerter("tok", "42", prefix="Zeus", cooldown_s=60, sender=lambda t, c, m: sent.append(m))
    assert a.alert("crit", "kill", "KILL-SWITCH : test")
    assert not a.alert("crit", "kill", "KILL-SWITCH : test")        # anti-spam
    assert a.alert("crit", "kill", "KILL-SWITCH : forcé", force=True)
    a.close()
    assert len(sent) == 2 and sent[0].startswith("🛑 Zeus — KILL-SWITCH")


def test_alerter_without_token_only_logs() -> None:
    a = Alerter("", "")
    assert not a.enabled and a.alert("info", "x", "rien à envoyer")


def test_alerter_network_failure_does_not_raise() -> None:
    def boom(t: str, c: str, m: str) -> None:
        raise OSError("pas de réseau")

    a = Alerter("tok", "1", sender=boom)
    a.alert("warn", "k", "test")
    t0 = time.time()
    a.close()
    assert a.failed == 1 and time.time() - t0 < 10


# ------------------------------------------------------------------ métriques
def test_prometheus_metrics_endpoint() -> None:
    snap = {"running": True, "ticks": 1234, "orders": 5, "fills": 4, "position": 0.25, "pnl": 12.5,
            "risk": {"equity": 10_012.5, "daily_drawdown": 3.0, "killed": False, "halts": [],
                     "latency_ms": 41.0},
            "strategy": {"spread_pts": 30.0, "atr_pts": 150.0},
            "decision_p50_us": 40.0, "decision_p99_us": 90.0, "ack_p50_us": 41000.0}
    txt = render_metrics(snap, "XAUUSD")
    assert 'zeus_ticks_total{symbol="XAUUSD"} 1234' in txt
    assert 'zeus_latency_us{symbol="XAUUSD",stage="tick_decision",quantile="0.99"} 90' in txt
    assert txt.count("# TYPE zeus_latency_us gauge") == 1
    srv = MetricsServer(0, lambda: snap, "XAUUSD")
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/metrics", timeout=5).read().decode()
        assert "zeus_position_lots" in body
    finally:
        srv.close()


# ------------------------------------------------------------------ ticks enregistrés
def test_tick_recorder_roundtrip_into_backtest_loader(tmp_path) -> None:
    rec = TickRecorder(tmp_path, "XAUUSD", flush_every=2)
    for i in range(5):
        rec.put(Tick("XAUUSD", 2650 + i * 0.01, 2650.2 + i * 0.01, exchange_ts_ms=1_767_571_200_000 + i))
    rec.close()
    rec2 = TickRecorder(tmp_path, "XAUUSD")              # même jour : ajout au même fichier
    rec2.put(Tick("XAUUSD", 2651.0, 2651.2, exchange_ts_ms=1_767_571_200_010))
    rec2.close()
    (f,) = list(tmp_path.glob("*.csv.gz"))
    assert f.name == "XAUUSD_20260105.csv.gz"
    ups = list(load_ticks_csv(f, "XAUUSD"))
    assert len(ups) == 6 and ups[-1].tick.bid == 2651.0


# ------------------------------------------------------------------ configuration à chaud
def _engine() -> Engine:
    s = S(trend_filter=False)
    clk = {"ns": 10**12}
    sim = SimExchange(point=PT, contract_size=100, clock_ns=lambda: clk["ns"])
    risk = RiskManager(RiskLimits(300, 0.01, 8, 150), 10_000, clock_ns=lambda: clk["ns"],
                       wall_clock_s=lambda: DAY0)
    om = OrderManager("XAUUSD", sim, risk, point=PT, volume_step=0.01, volume_min=0.01,
                      contract_size=100, session_id=1, clock_ns=lambda: clk["ns"])
    return Engine(s, OrderBook("XAUUSD"), StrategyRouter(s), om, risk, sim=sim, realtime=False,
                  clock_ns=lambda: clk["ns"])


def test_hot_config_validation_and_apply() -> None:
    eng = _engine()
    ch, en, err = validate_changes(eng.s, {"max_spread_points": 28, "risk_per_trade_pct": 0.25,
                                           "symbol": "EURUSD", "trading_enabled": False})
    assert ch == {"max_spread_points": 28, "risk_per_trade_pct": 0.25} and en is False
    assert any("symbol" in e for e in err)            # le symbole ne se change pas à chaud
    eng.apply_hot(ch, en)
    assert eng.risk.limits.max_spread_points == 28 and eng.om.risk_pct == 0.25
    assert eng.s.max_spread_points == 28 and eng.strategy.s.max_spread_points == 28
    assert not eng.risk.check_order(0.01, 0, 0).allowed           # pause manuelle
    eng.apply_hot({}, True)
    assert eng.risk.check_order(0.01, 0, 0).allowed
    bad, _, err2 = validate_changes(eng.s, {"risk_per_trade_pct": 50})
    assert bad == {} and err2


# ------------------------------------------------------------------ journal infalsifiable
def test_journal_hash_chain_detects_tampering(tmp_path) -> None:
    p = tmp_path / "t.csv"
    j = TradeJournal(p, "LIVE", "XAUUSD")
    for i in range(4):
        j.record("BUY", 0.01, 2650.0, 2651.0, 2.0, 1.0 - i, 0.07, reason="TP1, partiel",
                 strategy="momentum", entry_spread_pts=30, slippage_pts=1.5, exec_latency_ms=40)
    j.close()
    assert verify_journal(p) == (True, 4, None)
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[3] = lines[3].replace("-1.07", "+9.93")    # on « améliore » un trade perdant
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, _, bad = verify_journal(p)
    assert not ok and bad == 4


def test_journal_migrates_old_format_with_chain(tmp_path) -> None:
    p = tmp_path / "old.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS[:12])                       # format v2.1
        w.writerow(["2026-11-23 10:00:00", "LIVE", "XAUUSD", "BUY", 0.01, 1, 2, 3, 1, 0.1, 0.9, "TP"])
    j = TradeJournal(p, "LIVE", "XAUUSD")
    j.record("SELL", 0.01, 2, 1, 1, 1.0, 0.1)
    j.close()
    assert j.stats().trades == 2 and verify_journal(p) == (True, 2, None)


# ------------------------------------------------------------------ gigue de latence
async def test_sim_latency_jitter_keeps_fifo() -> None:
    clk = {"ns": 0}
    sim = SimExchange(point=PT, contract_size=100, latency_ms=40, latency_jitter_ms=30,
                      clock_ns=lambda: clk["ns"], seed=1)
    times = []
    for i in range(50):
        clk["ns"] += 5_000_000                         # un ordre toutes les 5 ms
        await sim.submit(Order(f"c{i}", "X", Side.BUY, 0.01, OrderType.LIMIT, price=1.0))
        times.append(sim._inbox[-1][0])
    assert times == sorted(times)                       # jamais d'inversion d'ordre
    delays = [t - (i + 1) * 5_000_000 for i, t in enumerate(times)]
    assert min(delays) >= 40e6 and len(set(times)) > 10   # latence fixe + gigue
