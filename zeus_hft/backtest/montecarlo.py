"""Tests de Monte Carlo sur une liste de trades (journal ou backtest).

Chaque simulation :
- tire les trades au hasard avec remise (bootstrap) : l'ordre des gains et des pertes change ;
- saute une fraction des trades (``skip_prob``) : trades manqués ;
- ajoute un coût défavorable aléatoire à chaque trade (slippage / spread élargi),
  tiré d'une loi exponentielle de moyenne ``extra_cost_mean`` (en devise du compte).

Sorties : distribution du PnL final et du drawdown max, probabilité de finir en perte et
« risque de ruine » (probabilité qu'un drawdown dépasse ``ruin_dd`` du capital).
Une stratégie robuste reste positive au 5e percentile.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(slots=True)
class MonteCarloReport:
    sims: int
    trades: int
    pnl_p5: float
    pnl_p50: float
    pnl_p95: float
    dd_p50: float
    dd_p95: float
    prob_loss: float
    risk_of_ruin: float
    ruin_dd: float
    original_pnl: float

    def text(self) -> str:
        if self.trades < 30:
            robust = f"ÉCHANTILLON TROP PETIT ({self.trades} trades < 30) : aucune conclusion possible"
        else:
            robust = "ROBUSTE" if self.pnl_p5 > 0 and self.risk_of_ruin < 0.01 else (
                "FRAGILE" if self.pnl_p50 > 0 else "PERDANT")
        return "\n".join([
            "══════ MONTE CARLO ══════",
            f"Simulations          : {self.sims:,} × {self.trades} trades",
            f"PnL réel             : {self.original_pnl:+,.2f}",
            f"PnL simulé           : P5 {self.pnl_p5:+,.2f} · médiane {self.pnl_p50:+,.2f} · "
            f"P95 {self.pnl_p95:+,.2f}",
            f"Drawdown max         : médiane {self.dd_p50:,.2f} · P95 {self.dd_p95:,.2f}",
            f"Probabilité de perte : {self.prob_loss:.1%}",
            f"Risque de ruine      : {self.risk_of_ruin:.2%} (drawdown > {self.ruin_dd:,.0f})",
            f"Verdict              : {robust}",
        ])


def monte_carlo(pnls: list[float] | np.ndarray, *, sims: int = 5000, skip_prob: float = 0.05,
                extra_cost_mean: float = 0.0, equity: float = 10_000.0, ruin_pct: float = 20.0,
                seed: int = 1) -> MonteCarloReport:
    x = np.asarray(pnls, dtype=np.float64)
    n = len(x)
    if n == 0:
        raise ValueError("aucun trade à simuler")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(sims, n))
    sample = x[idx]
    if extra_cost_mean > 0:
        sample = sample - rng.exponential(extra_cost_mean, size=sample.shape)
    if skip_prob > 0:
        sample = np.where(rng.random(sample.shape) < skip_prob, 0.0, sample)
    curve = np.cumsum(sample, axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.zeros((sims, 1)), curve], axis=1), axis=1)[:, 1:]
    dd = (peak - curve).max(axis=1)
    final = curve[:, -1]
    ruin_dd = equity * ruin_pct / 100.0
    p5, p50, p95 = np.percentile(final, [5, 50, 95])
    d50, d95 = np.percentile(dd, [50, 95])
    return MonteCarloReport(sims, n, float(p5), float(p50), float(p95), float(d50), float(d95),
                            float((final < 0).mean()), float((dd > ruin_dd).mean()), ruin_dd,
                            float(x.sum()))


def load_journal_pnls(path: str | Path, mode: str | None = None,
                      strategy: str | None = None) -> list[float]:
    out: list[float] = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if mode and r.get("mode") != mode:
                continue
            if strategy and r.get("strategy") != strategy:
                continue
            try:
                out.append(float(r["net_pnl"]))
            except (KeyError, ValueError):
                continue
    return out
