"""Assemblage des composants selon le mode (DRY_RUN / LIVE, MT5 ou synthétique)."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Awaitable, TypeVar

from zeus_hft.config.settings import Mode, Settings
from zeus_hft.core.engine import Engine
from zeus_hft.core.orderbook import OrderBook
from zeus_hft.execution.journal import TradeJournal
from zeus_hft.execution.order_manager import OrderManager
from zeus_hft.execution.sim_exchange import SimExchange
from zeus_hft.feeds.synthetic import SyntheticFeed
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager
from zeus_hft.strategies.hft_strategy import OBIMomentumStrategy

log = logging.getLogger("zeus_hft")
T = TypeVar("T")


class LiveAccountRefused(RuntimeError):
    pass


def run_async(coro: Awaitable[T]) -> T:
    """Lance la coroutine sur la boucle la plus rapide disponible.

    Linux/macOS : uvloop. Windows : winloop si installé, sinon la boucle Proactor
    standard (le flux MT5 tourne de toute façon dans un thread dédié).
    """
    if sys.platform == "win32":
        try:
            import winloop  # type: ignore[import-not-found]
            return winloop.run(coro)  # type: ignore[no-any-return]
        except ImportError:
            return asyncio.run(coro)  # type: ignore[arg-type]
    try:
        import uvloop
        return uvloop.run(coro)  # type: ignore[arg-type,no-any-return]
    except ImportError:
        return asyncio.run(coro)  # type: ignore[arg-type]


def _limits(s: Settings) -> RiskLimits:
    return RiskLimits(s.max_daily_drawdown, s.max_net_inventory, s.max_orders_per_second,
                      s.max_latency_ms, s.latency_breaches_to_kill, s.max_data_staleness_ms)


async def build_engine(s: Settings, synthetic: bool = False, use_model: bool = True) -> tuple[Engine, Any]:
    """Retourne (engine, ressource à fermer ou None). Doit être appelé dans la boucle asyncio."""
    loop = asyncio.get_running_loop()
    if synthetic:
        if s.mode is Mode.LIVE:
            raise ValueError("le mode LIVE exige MT5 (pas de flux synthétique)")
        feed: Any = SyntheticFeed(s.symbol)
        gw: Any = None
        equity = s.starting_equity
        account = "flux synthétique (sans MT5)"
    else:
        from zeus_hft.feeds.mt5_gateway import MT5Gateway
        gw = MT5Gateway(s)
        await asyncio.to_thread(gw.connect_blocking)
        s = s.model_copy(update=gw.symbol_specs())
        feed = gw
        demo = await asyncio.to_thread(gw.is_demo)
        account = "compte DÉMO MT5" if demo else "compte RÉEL MT5"
        if s.mode is Mode.LIVE:
            if not demo and not s.confirm_live:
                gw.shutdown()
                raise LiveAccountRefused(
                    "compte RÉEL détecté : relance avec --confirm-live (ou coche la case dans "
                    "l'appli) après avoir validé la stratégie en démo")
            equity = await asyncio.to_thread(gw.account_equity)
        else:
            equity = s.starting_equity

    risk = RiskManager(_limits(s), equity, on_kill=lambda r: log.critical("KILL-SWITCH : %s", r))
    if s.mode is Mode.LIVE:
        execution: Any = gw
        gw.latency_cb = lambda ms: loop.call_soon_threadsafe(risk.record_latency, ms)
        sim = None
    else:
        sim = SimExchange(point=s.point, contract_size=s.contract_size, latency_ms=s.sim_latency_ms,
                          slippage_points=s.sim_slippage_points, maker_fee_bps=s.maker_fee_bps,
                          taker_fee_bps=s.taker_fee_bps,
                          commission_per_lot=s.commission_per_lot / 2)
        execution = sim
    # Le journal sépare DRY_RUN et LIVE : seuls de vrais ordres (démo ou réel) comptent
    # pour juger la rentabilité en mode LIVE.
    journal = TradeJournal(s.journal_file, s.mode.value, s.symbol,
                           min_trades=s.verdict_min_trades)
    om = OrderManager(s.symbol, execution, risk, point=s.point, volume_step=s.volume_step,
                      volume_min=s.volume_min, contract_size=s.contract_size,
                      session_id=int(time.time()), journal=journal)
    if s.mode is Mode.LIVE:
        pos, avg = await asyncio.to_thread(gw.current_position)
        if pos:
            log.warning("position existante reprise : %+.2f lots @ %.5f", pos, avg)
            om.set_position(pos, avg)

    strategy = OBIMomentumStrategy(s, use_model=use_model)
    if use_model:
        # Même mémoire d'IA en DRY_RUN et en LIVE : elle apprend du marché, pas de nos ordres.
        strategy.attach_model_file(s.resolved_model_file())
        if strategy.model_loaded and strategy.model is not None:
            log.info("IA rechargée : %d exemples appris, précision %.1f %%",
                     strategy.model.samples, strategy.model.accuracy * 100)
        else:
            log.info("IA : nouvel apprentissage (%s)", s.resolved_model_file())
    engine = Engine(
        s, OrderBook(s.symbol, max_depth=50), strategy, om, risk,
        feed=feed, sim=sim, realtime=True,
        heartbeat=gw.heartbeat if gw is not None else None,
        reconcile=gw.reconcile if (gw is not None and s.mode is Mode.LIVE) else None,
        starting_equity=equity,
    )
    engine.account_label = f"{account} · {s.mode.value}"
    log.info("prêt : %s, %s", engine.account_label, journal.stats().verdict)
    return engine, gw
