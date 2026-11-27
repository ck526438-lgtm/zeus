"""Lancement temps réel du market making (DRY_RUN = vrai flux + exécution simulée à
file d'attente ; LIVE = ordres post-only réels), scanner de paires et sonde de latence."""
from __future__ import annotations

import asyncio
import gzip
import logging
import math
import queue
import socket
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zeus_hft.mm.backtest import event_to_json
from zeus_hft.mm.config import MMMode, MMSettings
from zeus_hft.mm.engine import MMEngine
from zeus_hft.mm.mexc import MexcFeed, MexcGateway, MexcRest
from zeus_hft.mm.sim import QueueSimExchange
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager

log = logging.getLogger("zeus_hft.mm")


class EventRecorder:
    """Enregistre chaque événement brut (.jsonl.gz par jour) : rejouable avec ``mm backtest``."""

    def __init__(self, directory: str, symbol: str) -> None:
        self.dir = Path(directory)
        self.symbol = symbol
        self._q: queue.SimpleQueue[tuple | None] = queue.SimpleQueue()
        self.count = 0
        self._t = threading.Thread(target=self._worker, name="mm-recorder", daemon=True)
        self._t.start()

    def put(self, ev: tuple) -> None:
        if ev[0] in ("snap", "depth", "trade"):
            self._q.put(ev)

    def _worker(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        cur, fh = None, None
        while True:
            ev = self._q.get()
            if ev is None:
                break
            day = datetime.fromtimestamp(ev[1] / 1000, timezone.utc).strftime("%Y%m%d")
            path = self.dir / f"{self.symbol}_{day}.jsonl.gz"
            if path != cur:
                if fh:
                    fh.close()
                fh = gzip.open(path, "at", encoding="utf-8", compresslevel=5)
                cur = path
            fh.write(event_to_json(ev) + "\n")
            self.count += 1
        if fh:
            fh.close()

    def close(self) -> None:
        self._q.put(None)
        self._t.join(5)


async def run_mm(s: MMSettings) -> None:
    symbol = s.mexc_symbol()
    rest = MexcRest(s)
    gw_live = MexcGateway(s, rest)
    try:
        specs = await asyncio.to_thread(gw_live.fetch_specs)
        s = s.model_copy(update=specs)
        log.info("contrat %s : tick %g, contrat %g, lot min %g, frais maker %.4f %% / taker %.4f %%",
                 symbol, s.tick, s.contract_size, s.min_vol, s.maker_fee * 100, s.taker_fee * 100)
    except Exception as exc:
        log.warning("spécifications du contrat illisibles (%s) : valeurs par défaut utilisées", exc)
    if s.maker_fee > 0:
        log.warning("⚠ frais maker %.4f %% sur %s : le market making y est très difficile. "
                    "Lance « mm scan » pour trouver une paire à 0 %%.", s.maker_fee * 100, symbol)

    if s.mode is MMMode.LIVE:
        if not (s.api_key.get_secret_value() and s.api_secret.get_secret_value()):
            raise SystemExit("clés API MEXC manquantes (ZEUS_MM_API_KEY / ZEUS_MM_API_SECRET)")
        if not s.confirm_live:
            raise SystemExit("mode LIVE : ajoute --confirm-live (ordres réels)")

    clock = lambda: int(time.time() * 1000)  # noqa: E731
    limits = RiskLimits(max_daily_drawdown=s.max_daily_loss, max_net_inventory=s.max_inventory,
                        max_orders_per_second=s.max_orders_per_second, max_latency_ms=1e9)
    risk = RiskManager(limits, s.starting_equity,
                       on_kill=lambda r: log.critical("KILL-SWITCH MM : %s", r))
    sim = None
    if s.mode is MMMode.LIVE:
        gateway: Any = gw_live
        start_inv = await asyncio.to_thread(gw_live.fetch_inventory)
    else:
        sim = QueueSimExchange(maker_fee=s.maker_fee, contract_size=s.contract_size,
                               latency_ms=s.sim_latency_ms, jitter_ms=s.sim_latency_jitter_ms,
                               clock_ms=clock)
        gateway, start_inv = sim, 0.0
    recorder = EventRecorder(s.record_dir, symbol)
    eng = MMEngine(s, gateway, risk, clock, sim=sim, session=int(time.time()), recorder=recorder)
    if sim is not None:
        sim.on_event = eng.qm.on_event
    if start_inv:
        log.warning("inventaire existant repris : %+g contrats", start_inv)
        eng.qm.inventory = start_inv

    feed = MexcFeed(s, rest)
    stop = asyncio.Event()
    q: asyncio.Queue = asyncio.Queue(maxsize=50_000)
    tasks = [asyncio.create_task(feed.run(q, stop))]
    if s.mode is MMMode.LIVE:
        tasks.append(asyncio.create_task(gateway.run_private(stop)))
    log.info("market making %s démarré sur %s (taille %g, inventaire max %g)",
             s.mode.value, symbol, s.quote_size, s.max_inventory)
    last_log = time.monotonic()
    try:
        while True:
            ev = await q.get()
            if ev[0] == "down":                       # flux coupé : on retire tout
                eng.book.reset()
                await eng.qm.cancel_all()
                continue
            await eng.on_event(ev)
            if eng.need_resync:
                feed.request_resync()
            if time.monotonic() - last_log > 10:
                last_log = time.monotonic()
                sn = eng.snapshot()
                log.info("mid %s · inv %+g · PnL %+.4f · fills %d · markout %+.2f bps · 2 côtés %.0f %% · %s",
                         sn["mid"], sn["inventory"], sn["pnl"], sn["fills"], sn["markout_bps"],
                         sn["uptime"] * 100, sn["reason"] or "cotation normale")
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()
        log.info("arrêt : annulation de tous les ordres")
        try:
            await eng.qm.cancel_all()
        except Exception:
            log.exception("annulation finale")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        recorder.close()
        sn = eng.snapshot()
        log.info("bilan : PnL %+.4f USDT · %d fills · inventaire restant %+g · %d événements enregistrés",
                 sn["pnl"], sn["fills"], sn["inventory"], recorder.count)
        if sn["inventory"]:
            log.warning("inventaire restant %+g contrats : à fermer manuellement si besoin", sn["inventory"])


# ---------------------------------------------------------------------- scanner de paires
def scan_pairs(rest: MexcRest, min_turnover: float = 5e6, top: int = 15) -> list[dict[str, Any]]:
    """Classe les perpétuels MEXC pour le market making :
    frais maker <= 0, spread >= 2 ticks, volume 24 h suffisant ; score = spread (bps) × √volume."""
    details = rest.public("/api/v1/contract/detail").get("data") or []
    tickers = rest.public("/api/v1/contract/ticker").get("data") or []
    spec = {d.get("symbol"): d for d in details if isinstance(d, dict)}
    rows = []
    for t in tickers:
        sym = t.get("symbol")
        d = spec.get(sym)
        try:
            bid, ask = float(t.get("bid1") or 0), float(t.get("ask1") or 0)
            turnover = float(t.get("amount24") or 0)
            tick = float(d["priceUnit"]) if d else 0.0
            maker = float(d.get("makerFeeRate", 1)) if d else 1.0
        except (TypeError, ValueError, KeyError):
            continue
        if bid <= 0 or ask <= bid or tick <= 0 or turnover < min_turnover or maker > 0:
            continue
        ticks = round((ask - bid) / tick)
        bps = (ask - bid) / ((ask + bid) / 2) * 1e4
        if ticks < 2:
            continue
        rows.append({"symbol": sym, "spread_ticks": ticks, "spread_bps": round(bps, 2),
                     "turnover_24h": turnover, "maker_fee": maker,
                     "score": round(bps * math.sqrt(turnover / 1e6), 2)})
    rows.sort(key=lambda r: -r["score"])
    return rows[:top]


# ---------------------------------------------------------------------- sonde de latence
def probe_latency(hosts: list[str], n: int = 50, port: int = 443) -> list[dict[str, Any]]:
    """RTT de connexion TCP (handshake = 1 aller-retour réseau) : P50 / P99 / P99.9 / gigue."""
    out = []
    for h in hosts:
        samples = []
        for _ in range(n):
            t0 = time.perf_counter_ns()
            try:
                with socket.create_connection((h, port), timeout=3):
                    samples.append((time.perf_counter_ns() - t0) / 1e6)
            except OSError:
                pass
            time.sleep(0.02)
        if not samples:
            out.append({"host": h, "ok": 0})
            continue
        samples.sort()
        pct = lambda p: samples[min(int(p * (len(samples) - 1) + 0.5), len(samples) - 1)]  # noqa: E731
        out.append({"host": h, "ok": len(samples), "p50": pct(0.5), "p99": pct(0.99),
                    "p999": pct(0.999), "jitter": pct(0.99) - pct(0.5),
                    "mean": statistics.fmean(samples)})
    return out
