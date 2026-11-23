"""Backtest tick-par-tick avec latence, slippage et frais.

Utilise EXACTEMENT le même code que le live (``Engine.process``, stratégie,
OrderManager, RiskManager) ; seuls l'horloge (simulée, pilotée par les
horodatages des ticks) et l'exécution (``SimExchange``) changent.
Le modèle IA en ligne prédit toujours AVANT de voir le résultat : ses
performances sont hors échantillon par construction.
"""
from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

from zeus_hft.config.settings import Settings
from zeus_hft.core.engine import Engine
from zeus_hft.core.orderbook import OrderBook
from zeus_hft.core.telemetry import LatencyStats
from zeus_hft.execution.order_manager import OrderManager
from zeus_hft.execution.sim_exchange import SimExchange
from zeus_hft.feeds.base import MarketUpdate
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager
from zeus_hft.strategies.hft_strategy import OBIMomentumStrategy


class SimClock:
    __slots__ = ("now",)

    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


@dataclass(slots=True)
class BacktestReport:
    ticks: int = 0
    orders: int = 0
    fills: int = 0
    trades: int = 0
    wins: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    fees: float = 0.0
    max_drawdown: float = 0.0
    killed: bool = False
    kill_reason: str = ""
    model_accuracy: float = 0.0
    decision_latency: LatencyStats | None = None
    equity_curve: list[tuple[int, float]] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else math.inf

    def text(self) -> str:
        lat = self.decision_latency
        lat_s = (f"p50 {lat.p50_us:.1f} µs · p99 {lat.p99_us:.1f} µs" if lat else "n/a")
        return "\n".join([
            "══════ BACKTEST ZEUS HFT ══════",
            f"Ticks traités        : {self.ticks:,}",
            f"Ordres / exécutions  : {self.orders:,} / {self.fills:,}",
            f"Trades (aller-retour): {self.trades:,}  · gagnants {self.win_rate:.1%}",
            f"PnL net              : {self.net_pnl:+,.2f}  (frais {self.fees:,.2f})",
            f"Profit factor        : {self.profit_factor:.2f}",
            f"Drawdown max         : {self.max_drawdown:,.2f}",
            f"Précision IA (live)  : {self.model_accuracy:.1%}",
            f"Latence de décision  : {lat_s}",
            f"Kill-switch          : {'OUI — ' + self.kill_reason if self.killed else 'non'}",
        ])


async def run_backtest(s: Settings, updates: Iterable[MarketUpdate], *, use_model: bool = True,
                       touch_fill: float = 0.0, equity_every: int = 100,
                       export_equity: str | None = None) -> BacktestReport:
    clock = SimClock()
    sim = SimExchange(point=s.point, contract_size=s.contract_size, latency_ms=s.sim_latency_ms,
                      slippage_points=s.sim_slippage_points, maker_fee_bps=s.maker_fee_bps,
                      taker_fee_bps=s.taker_fee_bps, commission_per_lot=s.commission_per_lot / 2,
                      touch_fill=touch_fill, clock_ns=clock)
    limits = RiskLimits(s.max_daily_drawdown, s.max_net_inventory, s.max_orders_per_second,
                        s.max_latency_ms, s.latency_breaches_to_kill, s.max_data_staleness_ms)
    risk = RiskManager(limits, s.starting_equity, clock_ns=clock, wall_clock_s=lambda: clock.now / 1e9)
    om = OrderManager(s.symbol, sim, risk, point=s.point, volume_step=s.volume_step,
                      volume_min=s.volume_min, contract_size=s.contract_size, session_id=0,
                      clock_ns=clock)
    strat = OBIMomentumStrategy(s, use_model=use_model)
    eng = Engine(s, OrderBook(s.symbol, max_depth=50), strat, om, risk, sim=sim,
                 clock_ns=clock, realtime=False)

    rep = BacktestReport()
    peak = s.starting_equity
    flat_pnl = 0.0
    was_open = False
    last: MarketUpdate | None = None
    for upd in updates:
        clock.now = upd.tick.exchange_ts_ms * 1_000_000
        upd.tick.recv_ts_ns = clock.now
        await eng.process(upd)
        last = upd
        rep.ticks += 1
        pnl = om.net_pnl(eng.last_mid)
        eq = s.starting_equity + pnl
        peak = max(peak, eq)
        rep.max_drawdown = max(rep.max_drawdown, peak - eq)
        is_open = om.position != 0.0
        if was_open and not is_open:
            r = pnl - flat_pnl
            rep.trades += 1
            if r > 0:
                rep.wins += 1
                rep.gross_profit += r
            else:
                rep.gross_loss -= r
            flat_pnl = pnl
        was_open = is_open
        if rep.ticks % equity_every == 0:
            rep.equity_curve.append((upd.tick.exchange_ts_ms, round(eq, 2)))

    # Fin de test : on ferme tout au dernier prix connu.
    if last is not None and (om.position != 0.0 or om.orders):
        for _ in range(3):
            await om.flatten(0)
            clock.now += sim.latency_ns + 1
            sim.on_market(last.tick.bid, last.tick.ask)
        if was_open and om.position == 0.0:
            r = om.net_pnl(eng.last_mid) - flat_pnl
            rep.trades += 1
            if r > 0:
                rep.wins += 1
                rep.gross_profit += r
            else:
                rep.gross_loss -= r

    rep.orders = om.order_count
    rep.fills = om.fill_count
    rep.fees = om.fees
    rep.net_pnl = om.net_pnl(eng.last_mid)
    rep.killed = risk.killed
    rep.kill_reason = risk.kill_reason
    rep.model_accuracy = strat.model.accuracy if strat.model else 0.0
    rep.decision_latency = eng.decision_latency.stats()
    if export_equity:
        Path(export_equity).parent.mkdir(parents=True, exist_ok=True)
        with open(export_equity, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["time_msc", "equity"])
            w.writerows(rep.equity_curve)
    return rep


def report_dict(rep: BacktestReport) -> dict[str, object]:
    d = asdict(rep)
    d.pop("equity_curve")
    d["win_rate"] = rep.win_rate
    d["profit_factor"] = rep.profit_factor
    return d
