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
from zeus_hft.strategies.router import StrategyRouter

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


def _limits(s: Settings, equity: float | None = None) -> RiskLimits:
    # Une seule position, taille fixe : l'inventaire max est plafonné au lot (pas de grille,
    # pas de martingale, pas de renforcement contre la tendance).
    max_inv = s.max_lots if s.risk_per_trade_pct > 0 else min(s.max_net_inventory, s.order_qty)
    return RiskLimits(s.max_daily_drawdown, max_inv,
                      s.max_orders_per_second, s.max_latency_ms, s.latency_breaches_to_kill,
                      s.max_data_staleness_ms, s.max_spread_points, s.max_daily_loss_pct,
                      s.max_consecutive_rejects, s.max_consecutive_losses, s.loss_streak_pause_min,
                      max_notional=(equity or s.starting_equity) * s.max_leverage,
                      window_dd=(equity or s.starting_equity) * s.window_dd_pct / 100.0,
                      window_dd_minutes=s.window_dd_minutes, max_reject_rate=s.max_reject_rate,
                      degraded_pause_min=s.degraded_pause_min)


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

    from zeus_hft.ops.alerts import Alerter

    alerter = Alerter(s.telegram_token.get_secret_value(), s.telegram_chat,
                      prefix=f"Zeus HFT {s.symbol} ({s.mode.value})")
    risk = RiskManager(
        _limits(s, equity), equity,
        on_kill=lambda r: alerter.alert("crit", "kill", f"KILL-SWITCH : {r}", force=True),
        on_halt=lambda k, r: alerter.alert("warn", f"halt-{k}", f"pause : {r}"))
    # reprise après crash : arrêt journalier, série de pertes et pauses de la même journée
    import json
    from pathlib import Path
    state_path = Path(s.resolved_state_file())
    if state_path.exists():
        try:
            restored = risk.load_state(json.loads(state_path.read_text(encoding="utf-8"))["risk"])
            if restored:
                log.warning("état restauré après redémarrage : %s", " · ".join(restored))
        except (OSError, ValueError, KeyError) as exc:
            log.warning("état précédent illisible (%s) : on repart à neuf", exc)
    if alerter.enabled:
        log.info("alertes Telegram actives")
    if s.mode is Mode.LIVE:
        execution: Any = gw
        gw.latency_cb = lambda ms: loop.call_soon_threadsafe(risk.record_latency, ms)
        sim = None
    else:
        sim = SimExchange(point=s.point, contract_size=s.contract_size, latency_ms=s.sim_latency_ms,
                          slippage_points=s.sim_slippage_points, maker_fee_bps=s.maker_fee_bps,
                          taker_fee_bps=s.taker_fee_bps,
                          commission_per_lot=s.commission_per_lot / 2,
                          latency_jitter_ms=s.sim_latency_jitter_ms)
        execution = sim
    # Le journal sépare DRY_RUN et LIVE : seuls de vrais ordres (démo ou réel) comptent
    # pour juger la rentabilité en mode LIVE.
    journal = TradeJournal(s.journal_file, s.mode.value, s.symbol,
                           min_trades=s.verdict_min_trades)
    om = OrderManager(s.symbol, execution, risk, point=s.point, volume_step=s.volume_step,
                      volume_min=s.volume_min, contract_size=s.contract_size,
                      session_id=int(time.time()), journal=journal,
                      stops_level_points=s.stops_level_points,
                      freeze_level_points=s.freeze_level_points,
                      server_sl_mult=s.server_sl_mult if s.mode is Mode.LIVE else 0.0,
                      risk_per_trade_pct=s.risk_per_trade_pct,
                      money_per_point_1lot=s.money_per_point(1.0),
                      volume_max=s.volume_max, max_lots=s.max_lots)
    if s.mode is Mode.LIVE:
        pos, avg = await asyncio.to_thread(gw.current_position)
        if pos:
            log.warning("position existante reprise : %+.2f lots @ %.5f", pos, avg)
            om.set_position(pos, avg)

    strategy = StrategyRouter(s, use_model=use_model, realtime=True)
    if gw is not None:
        try:
            bars = await asyncio.to_thread(gw.recent_m1_bars, max(s.atr_baseline_bars, 50))
            strategy.f.seed_bars(bars)
            n_trend = max(s.trend_ema_slow + 100, s.trend_min_bars)
            strategy.f.trend.m15.seed(await asyncio.to_thread(gw.recent_bars, "M15", n_trend))
            strategy.f.trend.h1.seed(await asyncio.to_thread(gw.recent_bars, "H1", n_trend))
            tv = strategy.f.trend.view()
            log.info("pré-chargé : ATR M1 %.0f pts (%d bougies) · H1 %s · M15 %s · ADX M15 %.0f",
                     strategy.f.atr_points, len(bars), "↑↓—"[(1 - tv.h1_dir) if tv.h1_dir else 2],
                     "↑↓—"[(1 - tv.m15_dir) if tv.m15_dir else 2], tv.adx_m15)
        except Exception:
            log.warning("historique MT5 indisponible : indicateurs reconstruits en direct "
                        "(ATR ~%d min, tendance H1 ~%d h)", s.atr_period, s.trend_min_bars)
    if synthetic and s.trend_filter:
        strategy.f.trend.enabled = False
        log.info("flux synthétique : filtre de tendance H1/M15 désactivé (pas d'historique)")
    if s.risk_per_trade_pct > 0:
        log.info("taille des lots : %.2f %% du capital par trade (max %.2f lots)",
                 s.risk_per_trade_pct, s.max_lots)
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
        position_snapshot=gw.position_snapshot if (gw is not None and s.mode is Mode.LIVE) else None,
        alerter=alerter,
        recorder=_recorder(s),
    )
    if s.metrics_port:
        from zeus_hft.ops.metrics import MetricsServer
        try:
            engine.metrics_server = MetricsServer(s.metrics_port, engine.snapshot, s.symbol)
            log.info("métriques Prometheus : http://127.0.0.1:%d/metrics", s.metrics_port)
        except OSError as exc:
            log.error("port de métriques %d indisponible : %s", s.metrics_port, exc)
    hedge = " · hedging" if getattr(gw, "hedging", False) else ""
    engine.account_label = f"{account}{hedge} · {s.mode.value} · stratégie {s.strategy}"
    log.info("prêt : %s, %s", engine.account_label, journal.stats().verdict)
    return engine, gw


def _recorder(s: Settings):  # type: ignore[no-untyped-def]
    if not s.record_ticks:
        return None
    from zeus_hft.ops.recorder import TickRecorder
    log.info("enregistrement des ticks : %s/%s_AAAAMMJJ.csv.gz", s.ticks_dir, s.symbol)
    return TickRecorder(s.ticks_dir, s.symbol)
