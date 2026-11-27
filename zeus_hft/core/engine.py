"""Moteur event-driven : flux -> carnet -> risque -> stratégie -> ordres.

``Engine.process()`` est l'unique chemin de décision, partagé par le live, le
DRY_RUN et le backtest (fidélité du backtest). La boucle temps réel ajoute :
- conflation : si plusieurs ticks attendent, seul le plus récent est traité ;
- heartbeat (ping MT5 -> kill-switch latence) ;
- réconciliation périodique des ordres limites (fills partiels) ;
- télémétrie toutes les 5 s, hors hot path.
"""
from __future__ import annotations

import asyncio
import os
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from zeus_hft.config.settings import Settings
from zeus_hft.core.orderbook import OrderBook, OrderBookError
from zeus_hft.core.telemetry import LatencyTracker
from zeus_hft.execution.order_manager import OrderManager
from zeus_hft.execution.sim_exchange import SimExchange
from zeus_hft.feeds.base import MarketFeed, MarketUpdate
from zeus_hft.risk.risk_manager import RiskManager
from zeus_hft.strategies.base import Strategy

log = logging.getLogger("zeus_hft.engine")


@dataclass(slots=True)
class EngineStats:
    ticks: int = 0
    conflated: int = 0
    decisions: int = 0
    actions: int = 0
    book_errors: int = 0
    last_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Engine:
    def __init__(
        self,
        s: Settings,
        book: OrderBook,
        strategy: Strategy,
        om: OrderManager,
        risk: RiskManager,
        *,
        feed: MarketFeed | None = None,
        sim: SimExchange | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        realtime: bool = True,
        heartbeat: Callable[[], Any] | None = None,
        reconcile: Callable[[list[Any]], Any] | None = None,
        starting_equity: float | None = None,
        position_snapshot: Callable[[], Any] | None = None,
        alerter: Any = None,
        recorder: Any = None,
    ) -> None:
        self.alerter = alerter
        self.recorder = recorder
        self.metrics_server: Any = None
        self._position_snapshot = position_snapshot
        self._last_tick_perf = 0
        self.s = s
        self.book = book
        self.strategy = strategy
        self.om = om
        self.risk = risk
        self.feed = feed
        self.sim = sim
        self._clock = clock_ns
        self.realtime = realtime
        self._heartbeat = heartbeat
        self._reconcile = reconcile
        self.starting_equity = starting_equity if starting_equity is not None else s.starting_equity
        self.stats = EngineStats()
        self.decision_latency = LatencyTracker("tick->décision")
        self.queue_delay = LatencyTracker("flux->moteur")
        self._seq = 0
        self._stop = asyncio.Event() if realtime else None
        self._kill_handled = False
        self.running = False
        self.last_mid: float | None = None
        self._q: asyncio.Queue[MarketUpdate | None] | None = None
        self._feed_error: BaseException | None = None
        self.account_label = ""
        self._last_tick: Any = None

    # ------------------------------------------------------------------ #
    # Chemin critique
    # ------------------------------------------------------------------ #
    async def process(self, upd: MarketUpdate) -> None:
        t0 = upd.tick.recv_ts_ns if self.realtime else time.perf_counter_ns()
        now = self._clock()
        if self.realtime:
            self.queue_delay.record(time.perf_counter_ns() - upd.tick.recv_ts_ns)
        tick = upd.tick
        self.stats.ticks += 1
        self._last_tick = tick
        self._last_tick_perf = time.perf_counter_ns()
        if self.recorder is not None:
            self.recorder.put(tick)

        if self.sim is not None:                  # exécutions simulées avant la décision
            self.sim.on_market(tick.bid, tick.ask)

        self._seq += 1
        try:
            if upd.bids and upd.asks:
                self.book.apply_snapshot(upd.bids, upd.asks, self._seq, tick.exchange_ts_ms, now)
            else:
                self.book.apply_snapshot([(tick.bid, tick.bid_qty or 1.0)],
                                         [(tick.ask, tick.ask_qty or 1.0)],
                                         self._seq, tick.exchange_ts_ms, now)
        except OrderBookError as exc:
            self.stats.book_errors += 1
            log.debug("carnet invalide : %s", exc)
            return

        mid = self.book.mid_price
        self.last_mid = mid
        self.risk.update_equity(self.starting_equity + self.om.net_pnl(mid))
        if self.risk.killed:
            await self._handle_kill(t0)
            return

        decision = self.strategy.on_tick(self.book, tick, self.om.position, self.om.avg_price, now)
        self.decision_latency.record(time.perf_counter_ns() - t0)
        if decision is None:
            return
        self.stats.decisions += 1
        self.stats.last_reason = decision.reason
        self.om.exit_reason = decision.reason
        top = self.book.top()
        if top is None:
            return
        age_ms = (now - self.book.last_update_ns) / 1e6
        self.stats.actions += await self.om.execute(decision, top, t0, age_ms)
        pop = getattr(self.strategy, "pop_protective_sl", None)
        if pop is not None:
            sl = pop()
            if sl is not None:
                await self.om.update_protective_sl(sl, top)

    async def _handle_kill(self, t0: int) -> None:
        if not self._kill_handled:
            log.critical("KILL-SWITCH : %s", self.risk.kill_reason)
            self._kill_handled = True
        if self.s.flatten_on_kill and (self.om.position != 0.0 or self.om.orders):
            self.om.exit_reason = f"kill-switch : {self.risk.kill_reason}"
            await self.om.flatten(t0)

    # ------------------------------------------------------------------ #
    # Boucle temps réel
    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        """Demande d'arrêt (appelable depuis un autre thread via call_soon_threadsafe)."""
        if self._stop is not None:
            self._stop.set()
        if self._q is not None:
            try:
                self._q.put_nowait(None)
            except asyncio.QueueFull:
                self._q.get_nowait()
                self._q.put_nowait(None)

    async def run(self) -> None:
        assert self.feed is not None and self._stop is not None
        self.running = True
        q: asyncio.Queue[MarketUpdate | None] = asyncio.Queue(maxsize=256)
        self._q = q
        tasks = [
            asyncio.create_task(self.feed.run(q, self._stop), name="feed"),  # type: ignore[arg-type]
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._reconcile_loop(), name="reconcile"),
            asyncio.create_task(self._telemetry_loop(), name="telemetry"),
            asyncio.create_task(self._model_save_loop(), name="model-save"),
            asyncio.create_task(self._watchdog_loop(), name="watchdog"),
            asyncio.create_task(self._position_sync_loop(), name="position-sync"),
            asyncio.create_task(self._news_loop(), name="news"),
            asyncio.create_task(self._state_loop(), name="state"),
            asyncio.create_task(self._hot_config_loop(), name="hot-config"),
        ]
        self._alert("info", "start", f"démarré ({self.account_label or self.s.mode.value})", force=True)
        tasks[0].add_done_callback(lambda t: None if t.cancelled() else self._on_feed_done(t))
        log.info("moteur démarré (%s, %s)", self.s.mode.value, self.s.symbol)
        try:
            while not self._stop.is_set():
                upd = await q.get()
                if upd is None:
                    break
                while not q.empty():            # conflation : on ne garde que le plus récent
                    nxt = q.get_nowait()
                    if nxt is None:
                        self._stop.set()
                        break
                    upd = nxt
                    self.stats.conflated += 1
                try:
                    await self.process(upd)
                except Exception:
                    log.exception("erreur dans le traitement du tick")
        finally:
            await self._shutdown(tasks)
        if self._feed_error is not None:
            raise self._feed_error

    def _on_feed_done(self, t: "asyncio.Task[Any]") -> None:
        exc = t.exception()
        if exc is not None:
            log.error("le flux s'est arrêté : %s", exc)
            self._feed_error = exc
        self.stop()

    async def _shutdown(self, tasks: list[asyncio.Task[Any]]) -> None:
        log.info("arrêt : annulation des ordres%s",
                 " et fermeture de la position" if self.s.flatten_on_kill else "")
        try:
            self.om.exit_reason = "arrêt du bot"
            if self.s.flatten_on_kill:
                await self.om.flatten()
            else:
                await self.om.cancel_all()
            await asyncio.sleep(0.5)            # laisse passer les derniers acquittements
            if self.sim is not None and self._last_tick is not None:
                # paper trading : plus de flux après l'arrêt, on exécute la sortie au dernier prix
                self.sim.on_market(self._last_tick.bid, self._last_tick.ask)
        except Exception:
            log.exception("erreur pendant l'arrêt")
        await self._save_model()
        await self._save_state()
        self._alert("info", "stop", "arrêté — " + self._day_line(), force=True)
        if self.om.journal is not None:
            self.om.journal.close()
        if self.recorder is not None:
            await asyncio.to_thread(self.recorder.close)
        if self.metrics_server is not None:
            await asyncio.to_thread(self.metrics_server.close)
        if self.alerter is not None:
            await asyncio.to_thread(self.alerter.close)
        self._stop.set()  # type: ignore[union-attr]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.running = False
        log.info("moteur arrêté — %s", self.summary())

    async def _heartbeat_loop(self) -> None:
        if self._heartbeat is None:
            return
        while not self._stop.is_set():  # type: ignore[union-attr]
            try:
                ping = await self._heartbeat()
                if ping is None:
                    log.warning("terminal MT5 déconnecté")
                    self._alert("warn", "mt5-off", "terminal MT5 déconnecté du serveur du broker")
                    self.risk.record_latency(float("inf"))
                else:
                    if self.risk.latency_blocked and ping <= self.s.max_latency_ms:
                        self._alert("ok", "lat-ok", f"latence revenue à {ping:.0f} ms")
                    self.risk.record_latency(ping)
                    if ping > self.s.max_latency_ms:
                        self._alert("warn", "lat", f"latence {ping:.0f} ms > {self.s.max_latency_ms:.0f} ms "
                                                   "(entrées bloquées)")
            except Exception:
                log.exception("heartbeat")
            await asyncio.sleep(self.s.heartbeat_interval)

    async def _reconcile_loop(self) -> None:
        if self._reconcile is None:
            return
        period = self.s.reconcile_interval_ms / 1e3
        while not self._stop.is_set():  # type: ignore[union-attr]
            tracked = list(self.om.orders.values())
            if tracked:
                try:
                    for ev in await self._reconcile(tracked):
                        self.om.on_event(ev)
                except Exception:
                    log.exception("réconciliation")
            await asyncio.sleep(period)

    async def _save_model(self) -> None:
        save = getattr(self.strategy, "save_model", None)
        if save is None:
            return
        try:
            await asyncio.to_thread(save)
        except Exception:
            log.exception("sauvegarde de l'IA")

    async def _model_save_loop(self) -> None:
        """Sauvegarde l'IA chaque minute : un crash ne fait perdre qu'une minute d'apprentissage."""
        while not self._stop.is_set():  # type: ignore[union-attr]
            await asyncio.sleep(60.0)
            await self._save_model()

    # ------------------------------------------------------------------ #
    # Exploitation : alertes, sauvegarde d'état, configuration à chaud
    # ------------------------------------------------------------------ #
    def _alert(self, level: str, key: str, text: str, force: bool = False) -> None:
        if self.alerter is not None:
            self.alerter.alert(level, key, text, force)

    def _day_line(self) -> str:
        j = self.om.journal.stats() if self.om.journal is not None else None
        rk = self.risk
        base = f"PnL du jour {rk.daily_pnl:+,.2f} · equity {rk.equity:,.2f}"
        return base + (f" · {j.trades} trades, {j.win_rate:.0%} gagnants — {j.verdict}" if j else "")

    async def _save_state(self) -> None:
        path = self.s.resolved_state_file()
        state = {"risk": self.risk.to_state(), "saved_at": time.time(),
                 "position": self.om.position, "avg_price": self.om.avg_price}

        def write() -> None:
            import json
            from pathlib import Path
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, p)
        try:
            await asyncio.to_thread(write)
        except OSError:
            log.exception("sauvegarde de l'état")

    async def _state_loop(self) -> None:
        """Instantané de l'état toutes les 5 s : un crash ne fait pas oublier l'arrêt du jour."""
        day = self.risk._day
        while not self._stop.is_set():  # type: ignore[union-attr]
            await asyncio.sleep(5.0)
            await self._save_state()
            if self.risk._day != day:                       # nouvelle journée UTC : bilan
                self._alert("info", "daily", "bilan de la journée — " + self._day_line(), force=True)
                day = self.risk._day

    def apply_hot(self, changes: dict[str, Any], enabled: bool | None) -> None:
        for k, v in changes.items():
            for target in (self.s, getattr(self.om.gateway, "s", None)):
                if target is not None and hasattr(target, k):
                    setattr(target, k, v)
        lim, s = self.risk.limits, self.s
        lim.max_spread_points = s.max_spread_points
        lim.max_daily_drawdown = s.max_daily_drawdown
        lim.max_daily_loss_pct = s.max_daily_loss_pct
        lim.max_consecutive_losses = s.max_consecutive_losses
        lim.max_orders_per_second = s.max_orders_per_second
        self.om.risk_pct = s.risk_per_trade_pct
        self.om.max_lots = s.max_lots
        if s.risk_per_trade_pct > 0:
            lim.max_net_inventory = s.max_lots
        if enabled is False and "manuel" not in self.risk.halts:
            self.risk.halt("manuel", "pause manuelle (configuration à chaud)")
        elif enabled is True and "manuel" in self.risk.halts:
            self.risk.resume("manuel")
            self._alert("ok", "manuel", "reprise manuelle du trading", force=True)
        if changes:
            log.warning("configuration à chaud appliquée : %s", changes)
            self._alert("info", "hot", f"réglages modifiés à chaud : {changes}", force=True)

    async def _hot_config_loop(self) -> None:
        from zeus_hft.ops.hotconfig import read_hot_file, validate_changes

        path = self.s.hot_config_file
        last = -1.0
        while not self._stop.is_set():  # type: ignore[union-attr]
            try:
                mtime = await asyncio.to_thread(os.path.getmtime, path)
                if mtime != last:
                    last = mtime
                    raw = await asyncio.to_thread(read_hot_file, path)
                    changes, enabled, errors = validate_changes(self.s, raw)
                    for e in errors:
                        log.error("configuration à chaud : %s", e)
                    self.apply_hot(changes, enabled)
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                log.error("configuration à chaud illisible : %s", exc)
            await asyncio.sleep(5.0)

    async def _news_loop(self) -> None:
        """Recharge chaque minute le calendrier exporté par ZeusNewsExporter.mq5 (si présent)."""
        update = getattr(self.strategy, "update_news", None)
        path = self.s.resolved_news_file()
        if update is None or not path:
            return
        from zeus_hft.strategies.news_file import load_news_csv

        last_mtime = -1.0
        warned = False
        while not self._stop.is_set():  # type: ignore[union-attr]
            try:
                mtime = await asyncio.to_thread(os.path.getmtime, path)
                if mtime != last_mtime:
                    events = await asyncio.to_thread(load_news_csv, path)
                    update(events)
                    last_mtime = mtime
                    log.info("calendrier MT5 : %d annonces à fort impact chargées", len(events))
                    if (time.time() - mtime) > 3 * 3600:
                        log.warning("calendrier MT5 ancien (%.0f h) : ZeusNewsExporter tourne-t-il ?",
                                    (time.time() - mtime) / 3600)
            except OSError:
                if not warned:
                    log.warning("calendrier MT5 introuvable (%s) : installe ZeusNewsExporter.mq5 "
                                "ou saisis les heures de news à la main", path)
                    warned = True
            await asyncio.sleep(60.0)

    async def _watchdog_loop(self) -> None:
        """Flux figé (plus aucun tick) -> pause des nouvelles entrées, reprise automatique."""
        limit = self.s.max_feed_silence_s
        start = time.perf_counter_ns()
        while not self._stop.is_set():  # type: ignore[union-attr]
            await asyncio.sleep(1.0)
            ref = self._last_tick_perf or start
            silence = (time.perf_counter_ns() - ref) / 1e9
            if silence > limit:
                if "flux" not in self.risk.halts:
                    log.warning("aucun tick depuis %.0f s : nouvelles entrées suspendues "
                                "(marché fermé ou flux figé)", silence)
                    self.risk.halt("flux", f"flux figé depuis {silence:.0f} s")
                    # heartbeat de données perdu : on annule tout ordre en attente
                    # (les positions restent protégées par leur SL serveur)
                    if self.om.orders:
                        await self.om.cancel_all()
                else:
                    self.risk.halts["flux"] = f"flux figé depuis {silence:.0f} s"
            elif "flux" in self.risk.halts:
                log.info("flux rétabli : reprise")
                self.risk.resume("flux")
                self._alert("ok", "flux-ok", "flux de prix rétabli")

    async def _position_sync_loop(self) -> None:
        """LIVE : compare la position MT5 à celle du bot (SL serveur touché, clôture manuelle)."""
        if self._position_snapshot is None:
            return
        while not self._stop.is_set():  # type: ignore[union-attr]
            await asyncio.sleep(2.0)
            if self.om.orders:
                continue
            fills = self.om.fill_count
            try:
                pos, avg = await self._position_snapshot()
            except Exception:
                log.exception("lecture de la position MT5")
                continue
            if fills != self.om.fill_count or self.om.orders:
                continue                      # un ordre est passé entre-temps : on réessaiera
            if abs(pos - self.om.position) > 1e-9:
                self.om.sync_external(pos, avg, self.last_mid,
                                      "fermé côté serveur (SL de secours ou manuel)")

    async def _telemetry_loop(self) -> None:
        while not self._stop.is_set():  # type: ignore[union-attr]
            await asyncio.sleep(5.0)
            log.info(self.summary())

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """État lisible par l'interface (lecture seule, valeurs simples)."""
        top = self.book.top()
        d = self.decision_latency.stats()
        o = self.om.tick_to_order.stats()
        a = self.om.ack_latency.stats()
        return {
            "running": self.running,
            "bid": top.bid if top else None,
            "ask": top.ask if top else None,
            "position": self.om.position,
            "avg_price": self.om.avg_price,
            "pnl": round(self.om.net_pnl(self.last_mid), 2),
            "realized": round(self.om.realized_pnl, 2),
            "fees": round(self.om.fees, 2),
            "fills": self.om.fill_count,
            "orders": self.om.order_count,
            "working": len(self.om.orders),
            "ticks": self.stats.ticks,
            "reason": self.stats.last_reason,
            "decision_p50_us": d.p50_us if d else None,
            "decision_p99_us": d.p99_us if d else None,
            "tick_to_order_p50_us": o.p50_us if o else None,
            "tick_to_order_p99_us": o.p99_us if o else None,
            "ack_p50_us": a.p50_us if a else None,
            "ack_p99_us": a.p99_us if a else None,
            "risk": self.risk.status(),
            "strategy": self.strategy.state(),
            "account": self.account_label,
            "journal": self.om.journal.stats() if self.om.journal is not None else None,
            "by_strategy": self.om.journal.strategy_summary() if self.om.journal is not None else "",
        }

    def summary(self) -> str:
        st = self.strategy.state()
        j = self.om.journal.stats() if self.om.journal is not None else None
        jtxt = (f"journal: {j.trades} trades, {j.win_rate:.0%} gagnants, net {j.net_pnl:+.2f} — "
                f"{j.verdict} | ") if j else ""
        if "regime" in st:
            jtxt = (f"[{st['regime']} · ATR {st.get('atr_pts', 0):.0f} pts · vol×{st.get('vol_ratio', 0):.2f}"
                    f" · {st.get('reason', '')}] ") + jtxt
        return (jtxt +f"ticks={self.stats.ticks} conflés={self.stats.conflated} pos={self.om.position:+.2f} "
                f"PnL={self.om.net_pnl(self.last_mid):+.2f} frais={self.om.fees:.2f} "
                f"fills={self.om.fill_count} score={st.get('score', 0):+.2f} "
                f"IA={st.get('model_acc', 0):.1%}/{st.get('model_conf', 0):.2f} | "
                f"{self.decision_latency.summary()} | {self.om.tick_to_order.summary()} | "
                f"risque={self.risk.status()}")
