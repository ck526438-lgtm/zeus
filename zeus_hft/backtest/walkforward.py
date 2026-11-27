"""Walk-Forward Analysis : optimiser sur une période, valider SANS modification sur la suivante.

Fenêtres glissantes : [train_days d'optimisation][test_days de validation], puis on décale
de test_days. Pour chaque fenêtre, chaque combinaison de la grille est backtestée sur la
période d'entraînement ; la meilleure (profit factor, avec un minimum de trades) est
ensuite appliquée telle quelle à la période de test. Seuls les résultats HORS ÉCHANTILLON
(OOS) comptent pour juger la stratégie.

Efficacité WF = PnL OOS par jour / PnL in-sample par jour. Au-dessus de ~50 %, les
paramètres « tiennent » ; proche de 0 ou négatif : sur-optimisation probable.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from zeus_hft.backtest.simulator import BacktestReport, run_backtest
from zeus_hft.config.settings import Settings
from zeus_hft.feeds.base import MarketUpdate

DAY_MS = 86_400_000

DEFAULT_GRID: dict[str, list[Any]] = {
    "velocity_entry_atr": [0.35, 0.45, 0.6],
    "mom_sl_atr": [0.5, 0.8],
    "mr_entry_dev_atr": [1.2, 1.6],
}


@dataclass(slots=True)
class WindowResult:
    train_start_ms: int
    test_start_ms: int
    test_end_ms: int
    params: dict[str, Any]
    is_pnl: float
    is_pf: float
    is_trades: int
    oos_pnl: float
    oos_pf: float
    oos_trades: int


@dataclass(slots=True)
class WalkForwardReport:
    windows: list[WindowResult] = field(default_factory=list)
    train_days: float = 0.0
    test_days: float = 0.0

    @property
    def oos_pnl(self) -> float:
        return sum(w.oos_pnl for w in self.windows)

    @property
    def oos_trades(self) -> int:
        return sum(w.oos_trades for w in self.windows)

    @property
    def efficiency(self) -> float:
        is_day = sum(w.is_pnl for w in self.windows) / max(len(self.windows) * self.train_days, 1e-9)
        oos_day = self.oos_pnl / max(len(self.windows) * self.test_days, 1e-9)
        return oos_day / is_day if is_day > 0 else float("nan")

    def text(self) -> str:
        from datetime import datetime, timezone

        def d(ms: int) -> str:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")

        lines = ["══════ WALK-FORWARD ══════",
                 f"{len(self.windows)} fenêtres · optimisation {self.train_days:g} j → test {self.test_days:g} j"]
        for i, w in enumerate(self.windows, 1):
            p = ", ".join(f"{k}={v}" for k, v in w.params.items())
            lines.append(f"#{i} test {d(w.test_start_ms)}→{d(w.test_end_ms)} | IS {w.is_pnl:+.2f} "
                         f"({w.is_trades} tr) | OOS {w.oos_pnl:+.2f} ({w.oos_trades} tr, "
                         f"PF {_fmt_pf(w.oos_pf)}) | {p}")
        win = sum(1 for w in self.windows if w.oos_pnl > 0)
        eff = self.efficiency
        lines += [f"OOS total            : {self.oos_pnl:+,.2f} sur {self.oos_trades} trades",
                  f"Fenêtres OOS gagnantes : {win}/{len(self.windows)}",
                  f"Efficacité WF        : {'n/a' if math.isnan(eff) else f'{eff:.0%}'}",
                  "Verdict              : " + (
                      "les paramètres tiennent hors échantillon" if self.oos_pnl > 0 and win * 2 >= len(self.windows)
                      else "sur-optimisation probable : ne pas passer en réel")]
        return "\n".join(lines)


def _fmt_pf(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _score(rep: BacktestReport, min_trades: int) -> float:
    if rep.trades < min_trades:
        return -math.inf
    pf = min(rep.profit_factor, 10.0)
    return pf if rep.net_pnl > 0 else pf - 10.0


async def run_walkforward(
    s: Settings,
    updates: list[MarketUpdate],
    *,
    train_days: float = 20.0,
    test_days: float = 5.0,
    grid: dict[str, list[Any]] | None = None,
    min_trades: int = 10,
    seed_bars: dict[str, list[tuple[int, float, float, float, float]]] | None = None,
    progress: Callable[[str], None] | None = None,
    backtest: Callable[..., Awaitable[BacktestReport]] = run_backtest,
) -> WalkForwardReport:
    if not updates:
        raise ValueError("aucun tick")
    grid = grid or DEFAULT_GRID
    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]
    ts = [u.tick.exchange_ts_ms for u in updates]
    t_first, t_last = ts[0], ts[-1]
    tr, te = int(train_days * DAY_MS), int(test_days * DAY_MS)
    rep = WalkForwardReport(train_days=train_days, test_days=test_days)

    import bisect

    def slice_ms(a: int, b: int) -> list[MarketUpdate]:
        return updates[bisect.bisect_left(ts, a):bisect.bisect_left(ts, b)]

    start = t_first
    while start + tr + te <= t_last + 1:
        train = slice_ms(start, start + tr)
        test = slice_ms(start + tr, start + tr + te)
        best, best_score, best_rep = combos[0], -math.inf, None
        for combo in combos:
            r = await backtest(s.model_copy(update=combo), iter(_fresh(train)), seed_bars=seed_bars)
            sc = _score(r, min_trades)
            if sc > best_score:
                best, best_score, best_rep = combo, sc, r
        oos = await backtest(s.model_copy(update=best), iter(_fresh(test)), seed_bars=seed_bars)
        w = WindowResult(start, start + tr, start + tr + te, best,
                         best_rep.net_pnl if best_rep else 0.0,
                         best_rep.profit_factor if best_rep else 0.0,
                         best_rep.trades if best_rep else 0,
                         oos.net_pnl, oos.profit_factor, oos.trades)
        rep.windows.append(w)
        if progress:
            progress(f"fenêtre {len(rep.windows)} : OOS {oos.net_pnl:+.2f} ({oos.trades} trades)")
        start += te
    if not rep.windows:
        raise ValueError(f"pas assez de données : il faut au moins {train_days + test_days:g} jours de ticks")
    return rep


def _fresh(ups: list[MarketUpdate]) -> list[MarketUpdate]:
    """Le simulateur modifie recv_ts_ns : on repart de copies légères des ticks."""
    from dataclasses import replace
    return [MarketUpdate(replace(u.tick), u.bids, u.asks) for u in ups]
