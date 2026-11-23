"""Passerelle MetaTrader 5 : flux de marché + exécution réelle (Windows).

Architecture d'accès à MT5 (le paquet ``MetaTrader5`` est synchrone, par IPC
avec le terminal) :
- Un thread dédié « feed » lit tick + DOM toutes les ``poll_interval_ms``
  avec ``time.sleep`` (haute résolution sous Windows avec Python >= 3.11,
  contrairement à ``asyncio.sleep`` limité à ~15,6 ms) et pousse les mises à
  jour dans la boucle asyncio via ``call_soon_threadsafe`` (sans blocage).
- Les ordres passent par un ``ThreadPoolExecutor`` à 1 worker : la boucle de
  décision n'attend jamais un appel MT5 de façon bloquante.
- Un verrou unique sérialise tous les appels à l'API MT5.
- Reconnexion avec backoff exponentiel + jitter ; heartbeat via
  ``terminal_info()`` (``connected``, ``ping_last`` = ping serveur en µs).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from zeus_hft.config.settings import Settings
from zeus_hft.core.telemetry import LatencyTracker
from zeus_hft.core.types import (
    EventKind,
    EventSink,
    Order,
    OrderEvent,
    OrderType,
    Side,
    Tick,
)
from zeus_hft.feeds.base import MarketUpdate, backoff_delays

log = logging.getLogger("zeus_hft.mt5")


def load_mt5() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - dépend de la plateforme
        raise RuntimeError("Paquet MetaTrader5 introuvable : il n'existe que sous Windows "
                           "(pip install MetaTrader5).") from exc
    return mt5


# Codes retour MT5 (valeurs documentées, utilisées si la constante manque)
RET_PLACED, RET_DONE, RET_DONE_PARTIAL = 10008, 10009, 10010
RET_TOO_MANY_REQUESTS = 10024
RET_INVALID_FILL = 10030
_OK_CODES = {RET_PLACED, RET_DONE, RET_DONE_PARTIAL}


class MT5Gateway:
    """Implémente ``MarketFeed`` et ``ExecutionGateway``."""

    def __init__(self, s: Settings, mt5: Any | None = None) -> None:
        self.s = s
        self.mt5 = mt5 if mt5 is not None else load_mt5()
        self.symbol = s.symbol
        self.magic = s.magic
        self.connected = False
        self._lock = threading.Lock()
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5-exec")
        self._sink: EventSink | None = None
        self._book_enabled = False
        self._filling_market: int = 0
        self._filling_pending: int = int(getattr(self.mt5, "ORDER_FILLING_RETURN", 2))
        self.info: Any = None
        self.order_rtt = LatencyTracker("aller-retour ordre MT5")
        self._throttled_until = 0.0
        self.latency_cb: Any = None      # appelé avec le RTT (ms) de chaque ordre

    # ------------------------------------------------------------------ #
    # Connexion
    # ------------------------------------------------------------------ #
    def _call(self, fn: str, *a: Any, **kw: Any) -> Any:
        with self._lock:
            return getattr(self.mt5, fn)(*a, **kw)

    def connect_blocking(self) -> Any:
        s = self.s
        kw: dict[str, Any] = {}
        if s.mt5_path:
            kw["path"] = s.mt5_path
        if s.mt5_login:
            kw.update(login=int(s.mt5_login), password=s.mt5_password.get_secret_value(),
                      server=s.mt5_server)
        for delay in backoff_delays(s.reconnect_base_delay, s.reconnect_max_delay,
                                    s.reconnect_max_attempts):
            if self._call("initialize", **kw):
                break
            log.warning("connexion MT5 impossible (%s) ; nouvel essai dans %.1fs",
                        self._call("last_error"), delay)
            time.sleep(delay)
        else:
            raise RuntimeError("connexion MT5 impossible après plusieurs essais")
        if not self._call("symbol_select", self.symbol, True):
            raise RuntimeError(f"symbole indisponible : {self.symbol}")
        self.info = self._call("symbol_info", self.symbol)
        self._filling_market = self._pick_filling(int(getattr(self.info, "filling_mode", 0)))
        if self.s.use_market_book:
            self._book_enabled = bool(self._call("market_book_add", self.symbol))
            if not self._book_enabled:
                log.warning("DOM indisponible chez ce broker : repli sur le flux de ticks")
        self.connected = True
        acc = self._call("account_info")
        log.info("MT5 connecté : compte %s (%s), symbole %s, point=%s, contrat=%s, DOM=%s",
                 getattr(acc, "login", "?"), getattr(acc, "server", "?"), self.symbol,
                 self.info.point, self.info.trade_contract_size, self._book_enabled)
        return acc

    def _pick_filling(self, mode_flags: int) -> int:
        m = self.mt5
        if mode_flags & 2:   # SYMBOL_FILLING_IOC
            return int(getattr(m, "ORDER_FILLING_IOC", 1))
        if mode_flags & 1:   # SYMBOL_FILLING_FOK
            return int(getattr(m, "ORDER_FILLING_FOK", 0))
        return int(getattr(m, "ORDER_FILLING_RETURN", 2))

    def is_demo(self) -> bool:
        acc = self._call("account_info")
        return acc is not None and acc.trade_mode == getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)

    def account_equity(self) -> float:
        acc = self._call("account_info")
        return float(acc.equity) if acc is not None else 0.0

    def symbol_specs(self) -> dict[str, float]:
        i = self.info
        return {"point": float(i.point), "contract_size": float(i.trade_contract_size),
                "volume_step": float(i.volume_step), "volume_min": float(i.volume_min)}

    def current_position(self) -> tuple[float, float]:
        """Position nette (lots signés) et prix moyen des positions de ce bot."""
        pos = self._call("positions_get", symbol=self.symbol) or ()
        buy_t = getattr(self.mt5, "POSITION_TYPE_BUY", 0)
        net, notional = 0.0, 0.0
        for p in pos:
            if p.magic != self.magic:
                continue
            v = p.volume if p.type == buy_t else -p.volume
            net += v
            notional += v * p.price_open
        return round(net, 8), (notional / net if net else 0.0)

    def shutdown(self) -> None:
        self.connected = False
        try:
            if self._book_enabled:
                self._call("market_book_release", self.symbol)
            self._call("shutdown")
        finally:
            self._exec.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ #
    # Flux de marché (thread dédié)
    # ------------------------------------------------------------------ #
    def poll_market(self, last_time_msc: int) -> MarketUpdate | None:
        t = self._call("symbol_info_tick", self.symbol)
        recv = time.perf_counter_ns()
        if t is None:
            raise ConnectionError(f"symbol_info_tick a échoué : {self._call('last_error')}")
        bids = asks = None
        if self._book_enabled:
            book = self._call("market_book_get", self.symbol)
            if book:
                sell_t = getattr(self.mt5, "BOOK_TYPE_SELL", 1)
                buy_t = getattr(self.mt5, "BOOK_TYPE_BUY", 2)
                asks = sorted(((b.price, float(b.volume_dbl or b.volume)) for b in book
                               if b.type == sell_t), key=lambda x: x[0])
                bids = sorted(((b.price, float(b.volume_dbl or b.volume)) for b in book
                               if b.type == buy_t), key=lambda x: -x[0])
        if t.time_msc == last_time_msc and bids is None:
            return None    # rien de neuf
        tick = Tick(self.symbol, float(t.bid), float(t.ask), last=float(t.last),
                    volume=float(getattr(t, "volume_real", 0.0) or t.volume),
                    exchange_ts_ms=int(t.time_msc), recv_ts_ns=recv)
        return MarketUpdate(tick, bids or None, asks or None)

    def _feed_thread(self, loop: asyncio.AbstractEventLoop, out: asyncio.Queue[MarketUpdate],
                     stop: threading.Event) -> None:
        period = self.s.poll_interval_ms / 1e3
        last_msc = -1
        last_book: tuple[Any, Any] | None = None
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                upd = self.poll_market(last_msc)
                if upd is not None:
                    book_key = (upd.bids and upd.bids[0], upd.asks and upd.asks[0])
                    if upd.tick.exchange_ts_ms != last_msc or book_key != last_book:
                        last_msc, last_book = upd.tick.exchange_ts_ms, book_key
                        loop.call_soon_threadsafe(_put_latest, out, upd)
            except Exception as exc:
                log.error("flux MT5 interrompu : %s — reconnexion", exc)
                self.connected = False
                for delay in backoff_delays(self.s.reconnect_base_delay, self.s.reconnect_max_delay):
                    if stop.wait(delay):
                        return
                    try:
                        self._call("shutdown")
                        self.connect_blocking()
                        break
                    except Exception as exc2:
                        log.warning("reconnexion échouée : %s", exc2)
            dt = time.perf_counter() - t0
            if dt < period:
                time.sleep(period - dt)

    async def run(self, out: asyncio.Queue[MarketUpdate], stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        tstop = threading.Event()
        th = threading.Thread(target=self._feed_thread, args=(loop, out, tstop),
                              name="mt5-feed", daemon=True)
        th.start()
        try:
            await stop.wait()
        finally:
            tstop.set()
            await asyncio.to_thread(th.join, 2.0)

    def heartbeat_blocking(self) -> float | None:
        """Retourne le ping serveur en ms, ou None si le terminal est déconnecté."""
        ti = self._call("terminal_info")
        if ti is None or not ti.connected:
            self.connected = False
            return None
        self.connected = True
        return float(ti.ping_last) / 1000.0

    async def heartbeat(self) -> float | None:
        return await asyncio.get_running_loop().run_in_executor(self._exec, self.heartbeat_blocking)

    # ------------------------------------------------------------------ #
    # Exécution
    # ------------------------------------------------------------------ #
    def set_event_sink(self, sink: EventSink) -> None:
        self._sink = sink

    def _emit(self, ev: OrderEvent, loop: asyncio.AbstractEventLoop) -> None:
        if self._sink is not None:
            loop.call_soon_threadsafe(self._sink, ev)

    def _send(self, req: dict[str, Any]) -> Any:
        if time.monotonic() < self._throttled_until:
            raise RuntimeError("MT5 limite les requêtes (TOO_MANY_REQUESTS) : pause en cours")
        t0 = time.perf_counter_ns()
        res = self._call("order_send", req)
        rtt = self.order_rtt.record_since(t0)
        if self.latency_cb is not None:
            self.latency_cb(rtt / 1e6)
        if res is None:
            raise ConnectionError(f"order_send None : {self._call('last_error')}")
        if res.retcode == RET_TOO_MANY_REQUESTS:
            self._throttled_until = time.monotonic() + 1.0
        return res

    def _submit_blocking(self, order: Order, loop: asyncio.AbstractEventLoop) -> None:
        m = self.mt5
        cid = order.client_order_id
        try:
            if order.order_type is OrderType.MARKET:
                t = self._call("symbol_info_tick", self.symbol)
                req = {
                    "action": m.TRADE_ACTION_DEAL, "symbol": self.symbol,
                    "volume": float(order.qty),
                    "type": m.ORDER_TYPE_BUY if order.side is Side.BUY else m.ORDER_TYPE_SELL,
                    "price": t.ask if order.side is Side.BUY else t.bid,
                    "deviation": self.s.deviation_points, "magic": self.magic, "comment": cid,
                    "type_time": m.ORDER_TIME_GTC, "type_filling": self._filling_market,
                }
            else:
                req = {
                    "action": m.TRADE_ACTION_PENDING, "symbol": self.symbol,
                    "volume": float(order.qty),
                    "type": m.ORDER_TYPE_BUY_LIMIT if order.side is Side.BUY else m.ORDER_TYPE_SELL_LIMIT,
                    "price": float(order.price or 0.0), "magic": self.magic, "comment": cid,
                    "type_time": m.ORDER_TIME_GTC,
                    "type_filling": self._filling_pending,
                }
            res = self._send_with_filling(req, order.order_type)
        except Exception as exc:
            self._emit(OrderEvent(EventKind.REJECTED, cid, reason=str(exc)), loop)
            return
        if res.retcode not in _OK_CODES:
            self._emit(OrderEvent(EventKind.REJECTED, cid, reason=f"{res.retcode} {res.comment}"), loop)
            return
        self._emit(OrderEvent(EventKind.ACK, cid, int(res.order)), loop)
        if order.order_type is OrderType.MARKET:
            if res.volume > 0:
                fee = self._deal_fees(int(res.order))
                self._emit(OrderEvent(EventKind.FILL, cid, int(res.order), fill_qty=float(res.volume),
                                      fill_price=float(res.price), fee=fee), loop)
            if float(res.volume) < float(order.qty) - 1e-9:
                # Ordre au marché exécuté partiellement (IOC) : le reliquat n'existe plus côté
                # serveur. On le clôt localement, sinon l'ordre resterait « actif » pour toujours.
                self._emit(OrderEvent(EventKind.CANCELED, cid, int(res.order),
                                      reason="reliquat IOC non exécuté"), loop)

    def _send_with_filling(self, req: dict[str, Any], otype: OrderType) -> Any:
        """Envoie l'ordre ; si le broker refuse le mode de remplissage (10030), essaie les autres
        modes et retient celui qui fonctionne pour les ordres suivants."""
        res = self._send(req)
        if res.retcode != RET_INVALID_FILL:
            return res
        m = self.mt5
        modes = [int(getattr(m, n, v)) for n, v in (("ORDER_FILLING_RETURN", 2),
                 ("ORDER_FILLING_IOC", 1), ("ORDER_FILLING_FOK", 0))]
        for mode in modes:
            if mode == req.get("type_filling"):
                continue
            res = self._send({**req, "type_filling": mode})
            if res.retcode != RET_INVALID_FILL:
                if res.retcode in _OK_CODES:
                    if otype is OrderType.MARKET:
                        self._filling_market = mode
                    else:
                        self._filling_pending = mode
                    log.info("mode de remplissage %s retenu pour les ordres %s", mode, otype.value)
                return res
        return res

    def _deal_fees(self, order_ticket: int) -> float:
        deals = self._call("history_deals_get", ticket=order_ticket) or ()
        return -sum(float(d.commission) + float(getattr(d, "fee", 0.0)) for d in deals)

    def _cancel_blocking(self, order: Order, loop: asyncio.AbstractEventLoop) -> None:
        cid = order.client_order_id
        try:
            res = self._send({"action": self.mt5.TRADE_ACTION_REMOVE,
                              "order": int(order.exchange_order_id or 0)})
        except Exception as exc:
            self._emit(OrderEvent(EventKind.REJECTED, cid, reason=str(exc)), loop)
            return
        kind = EventKind.CANCELED if res.retcode in _OK_CODES else EventKind.REJECTED
        self._emit(OrderEvent(kind, cid, reason=f"{res.retcode} {res.comment}"), loop)

    def _replace_blocking(self, order: Order, price: float, loop: asyncio.AbstractEventLoop) -> None:
        cid = order.client_order_id
        try:
            res = self._send({"action": self.mt5.TRADE_ACTION_MODIFY,
                              "order": int(order.exchange_order_id or 0), "price": float(price),
                              "type_time": self.mt5.ORDER_TIME_GTC})
        except Exception as exc:
            self._emit(OrderEvent(EventKind.REJECTED, cid, reason=str(exc)), loop)
            return
        if res.retcode in _OK_CODES:
            self._emit(OrderEvent(EventKind.REPLACED, cid, new_price=price), loop)
        else:
            self._emit(OrderEvent(EventKind.REJECTED, cid, reason=f"{res.retcode} {res.comment}"), loop)

    async def submit(self, order: Order) -> None:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(self._exec, self._submit_blocking, order, loop)

    async def cancel(self, order: Order) -> None:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(self._exec, self._cancel_blocking, order, loop)

    async def replace(self, order: Order, new_price: float, new_qty: float) -> None:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(self._exec, self._replace_blocking, order, new_price, loop)

    # ------------------------------------------------------------------ #
    # Réconciliation (fills des ordres limites, annulations côté serveur)
    # ------------------------------------------------------------------ #
    def reconcile_blocking(self, tracked: list[Order]) -> list[OrderEvent]:
        m = self.mt5
        events: list[OrderEvent] = []
        tracked = [o for o in tracked if o.exchange_order_id and o.order_type is OrderType.LIMIT]
        if not tracked:
            return events
        live = {int(o.ticket) for o in (self._call("orders_get", symbol=self.symbol) or ())}
        st_canceled = {getattr(m, n, v) for n, v in (("ORDER_STATE_CANCELED", 2),
                       ("ORDER_STATE_REJECTED", 5), ("ORDER_STATE_EXPIRED", 6))}
        for o in tracked:
            ticket = int(o.exchange_order_id or 0)
            deals = self._call("history_deals_get", ticket=ticket) or ()
            vol = sum(float(d.volume) for d in deals)
            if vol > o.filled_qty + 1e-9:
                new_vol = vol - o.filled_qty
                vwap = sum(float(d.volume) * float(d.price) for d in deals) / vol
                # prix de la partie nouvelle : on utilise le VWAP cumulé (approximation sûre)
                fee = -sum(float(d.commission) + float(getattr(d, "fee", 0.0)) for d in deals)
                events.append(OrderEvent(EventKind.FILL, o.client_order_id, ticket,
                                         fill_qty=new_vol, fill_price=vwap,
                                         fee=fee * new_vol / vol, is_maker=True))
            if ticket not in live and vol < o.qty - 1e-9:
                hist = self._call("history_orders_get", ticket=ticket) or ()
                if hist and hist[0].state in st_canceled:
                    events.append(OrderEvent(EventKind.CANCELED, o.client_order_id, ticket,
                                             reason="annulé côté serveur"))
        return events

    async def reconcile(self, tracked: list[Order]) -> list[OrderEvent]:
        return await asyncio.get_running_loop().run_in_executor(
            self._exec, self.reconcile_blocking, tracked)


def _put_latest(q: asyncio.Queue[MarketUpdate], upd: MarketUpdate) -> None:
    """Insertion non bloquante ; si la file est pleine on jette le plus ancien (conflation)."""
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(upd)
