"""Backtest du market making : rejoue des événements (enregistrés ou synthétiques) avec le
simulateur à file d'attente, la latence + gigue, et le MÊME moteur qu'en direct."""
from __future__ import annotations

import gzip
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from zeus_hft.mm.config import MMSettings
from zeus_hft.mm.engine import MMEngine
from zeus_hft.mm.sim import QueueSimExchange
from zeus_hft.risk.risk_manager import RiskLimits, RiskManager


@dataclass(slots=True)
class MMReport:
    events: int
    hours: float
    fills: int
    volume: float
    pnl: float
    realized: float
    fees: float
    max_dd: float
    max_inventory: float
    markout_bps: float
    uptime: float
    orders: int
    cancels: int
    rejects: int

    @property
    def pnl_per_hour(self) -> float:
        return self.pnl / self.hours if self.hours > 0 else 0.0

    @property
    def edge_bps(self) -> float:
        """PnL net par unité de volume traité : l'« edge » réel capturé par trade."""
        return self.pnl / self.volume * 1e4 if self.volume > 0 else 0.0

    def lines(self) -> list[str]:
        verdict = (f"échantillon trop petit ({self.fills} < 100 exécutions) : aucune conclusion"
                   if self.fills < 100 else
                   "edge positif après sélection adverse" if self.pnl > 0 else
                   "PERDANT : la sélection adverse mange le spread")
        return [
            "══════ BACKTEST MARKET MAKING ══════",
            f"• Durée rejouée     : {self.hours:.2f} h · {self.events:,} événements",
            f"• Exécutions        : {self.fills} · volume {self.volume:,.0f} USDT",
            f"• PnL net           : {self.pnl:+,.4f} USDT ({self.pnl_per_hour:+.4f}/h) · frais {self.fees:,.4f}",
            f"• Edge net          : {self.edge_bps:+.2f} bps du volume",
            f"• Markout moyen     : {self.markout_bps:+.2f} bps (négatif = sélection adverse)",
            f"• Drawdown max      : {self.max_dd:,.4f} · inventaire max {self.max_inventory:g} contrats",
            f"• Présence 2 côtés  : {self.uptime:.0%} du temps",
            f"• Ordres / annul. / rejets : {self.orders} / {self.cancels} / {self.rejects}",
            f"• Verdict           : {verdict}",
        ]

    def text(self) -> str:
        return "\n".join(self.lines())


async def run_mm_backtest(s: MMSettings, events: Iterable[tuple]) -> MMReport:
    clock = {"ms": 0}
    sim = QueueSimExchange(maker_fee=s.maker_fee, contract_size=s.contract_size,
                           latency_ms=s.sim_latency_ms, jitter_ms=s.sim_latency_jitter_ms,
                           clock_ms=lambda: clock["ms"])
    limits = RiskLimits(max_daily_drawdown=s.max_daily_loss, max_net_inventory=s.max_inventory,
                        max_orders_per_second=s.max_orders_per_second, max_latency_ms=1e9,
                        max_consecutive_rejects=10_000, max_reject_rate=1.0)
    risk = RiskManager(limits, s.starting_equity, clock_ns=lambda: clock["ms"] * 1_000_000,
                       wall_clock_s=lambda: clock["ms"] / 1000)
    eng = MMEngine(s, sim, risk, lambda: clock["ms"], sim=sim)
    sim.on_event = eng.qm.on_event
    first = last = 0
    for ev in events:
        clock["ms"] = ev[1]
        first = first or ev[1]
        last = ev[1]
        await eng.on_event(ev)
    qm = eng.qm
    snap = eng.snapshot()
    return MMReport(eng.events, (last - first) / 3.6e6, len(qm.stats.fills), qm.volume,
                    qm.pnl(eng.last_mid or 0.0), qm.realized, qm.fees, eng.max_dd,
                    eng.max_abs_inventory, eng.qe.markout.mean_bps, snap["uptime"],
                    qm.stats.orders_sent, qm.stats.cancels, qm.stats.rejects)


# ---------------------------------------------------------------------- données
def load_events(path: str | Path) -> Iterator[tuple]:
    """Événements enregistrés par ``mm run`` (JSON lines gzip)."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            k = d.get("k")
            if k in ("snap", "depth"):
                yield (k, d["t"], [tuple(x) for x in d["b"]], [tuple(x) for x in d["a"]], d["v"])
            elif k == "trade":
                yield ("trade", d["t"], d["p"], d["q"], d["s"])


def event_to_json(ev: tuple) -> str:
    k = ev[0]
    if k in ("snap", "depth"):
        return json.dumps({"k": k, "t": ev[1], "b": ev[2], "a": ev[3], "v": ev[4]})
    return json.dumps({"k": k, "t": ev[1], "p": ev[2], "q": ev[3], "s": ev[4]})


def synthetic_events(n_seconds: int = 3600, *, mid: float = 150.0, tick: float = 0.01,
                     levels: int = 10, trades_per_s: float = 4.0, informed: float = 0.2,
                     vol_ticks_per_s: float = 1.5, seed: int = 3, start_ms: int = 1_767_600_000_000
                     ) -> Iterator[tuple]:
    """Marché synthétique : carnet de ``levels`` niveaux, trades de taille aléatoire.
    Une fraction ``informed`` des trades anticipe le prochain mouvement : c'est la
    sélection adverse que le market maker doit survivre."""
    rng = random.Random(seed)
    ts = start_ms
    version = 1

    def book(m: float) -> tuple[list, list]:
        bb = math.floor(m / tick) * tick
        bids = [(round(bb - i * tick, 8), round(rng.uniform(5, 40) * (1 + i * 0.3), 1)) for i in range(levels)]
        asks = [(round(bb + (i + 1) * tick, 8), round(rng.uniform(5, 40) * (1 + i * 0.3), 1)) for i in range(levels)]
        return bids, asks

    b, a = book(mid)
    yield ("snap", ts, b, a, version)
    drift = 0
    for _ in range(int(n_seconds * 10)):                  # pas de 100 ms
        ts += 100
        if rng.random() < vol_ticks_per_s / 10:
            drift = rng.choice((-1, 1))
            mid += drift * tick
            nb, na = book(mid)
            version += 1
            # delta = nouveaux niveaux + suppression des anciens
            old_b = {p for p, _ in b} - {p for p, _ in nb}
            old_a = {p for p, _ in a} - {p for p, _ in na}
            yield ("depth", ts, nb + [(p, 0.0) for p in old_b], na + [(p, 0.0) for p in old_a], version)
            b, a = nb, na
        if rng.random() < trades_per_s / 10:
            # trade informé : dans le sens du PROCHAIN mouvement (on l'anticipe en tirant le drift)
            if rng.random() < informed:
                side = 1 if rng.random() < 0.5 + 0.4 * drift else -1
            else:
                side = rng.choice((-1, 1))
            px = a[0][0] if side > 0 else b[0][0]
            yield ("trade", ts, px, round(rng.expovariate(1 / 6.0), 1) + 0.1, side)
