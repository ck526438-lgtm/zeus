"""Backtest réaliste : spread, commission, SL/TP intra-bougie (pire cas),
entrée à l'ouverture de la bougie SUIVANTE (aucune triche sur le futur)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .risk import RiskManager, lot_size
from .strategy import Strategy


@dataclass
class Trade:
    entry_time: object
    exit_time: object
    direction: int
    entry: float
    exit: float
    volume: float
    pnl: float
    reason: str


def run_backtest(df: pd.DataFrame, cfg: dict, model=None) -> dict:
    bt, rp = cfg["backtest"], cfg["risk"]
    strat = Strategy(cfg["strategy"], model)
    risk = RiskManager(rp, cfg["trading"].get("sessions_utc"))
    feat = strat.prepare(df).reset_index(drop=True)
    point, cs = bt["point"], bt["contract_size"]
    spread = bt["spread_points"] * point
    comm = bt["commission_per_lot"]

    balance = float(bt["initial_balance"])
    equity_curve, trades = [], []
    pos = None   # dict: dir, entry, sl, tp, vol, bars, time

    times = pd.to_datetime(feat["time"], utc=True) if "time" in feat else pd.Series(pd.NaT, index=feat.index)
    O, H, L, C = (feat[k].to_numpy() for k in ("open", "high", "low", "close"))
    A = feat["atr"].to_numpy()

    def close_pos(i, price, reason):
        nonlocal balance, pos
        pnl = (price - pos["entry"]) * pos["dir"] * pos["vol"] * cs - comm * pos["vol"]
        balance += pnl
        trades.append(Trade(pos["time"], times.iloc[i], pos["dir"], pos["entry"], price,
                            pos["vol"], pnl, reason))
        risk.on_close(pnl, times.iloc[i].to_pydatetime() if pd.notna(times.iloc[i]) else None)
        pos = None

    pending = None
    for i in range(len(feat)):
        now = times.iloc[i].to_pydatetime() if pd.notna(times.iloc[i]) else None
        if now is not None:
            risk.new_day_check(now, balance)

        # 1) exécuter le signal de la bougie précédente à l'ouverture
        if pending is not None and pos is None:
            d, a = pending
            entry = O[i] + spread if d == 1 else O[i]   # achat au ask, vente au bid
            sl, tp = risk.levels(d, entry, a)
            vol = lot_size(balance, rp["risk_per_trade_pct"], abs(entry - sl),
                           tick_value=cs * point, tick_size=point,
                           vol_min=0.01, vol_max=100, vol_step=0.01)
            if vol > 0:
                pos = {"dir": d, "entry": entry, "sl": sl, "tp": tp, "vol": vol, "bars": 0,
                       "time": times.iloc[i]}
                if now is not None:
                    risk.on_open("BT", now)
        pending = None

        # 2) SL / TP intra-bougie (on suppose le pire : SL touché d'abord)
        if pos is not None:
            d = pos["dir"]
            bid_low, bid_high = L[i], H[i]
            exit_hit = None
            if d == 1:
                if bid_low <= pos["sl"]:
                    exit_hit = (min(pos["sl"], O[i]), "SL")
                elif bid_high >= pos["tp"]:
                    exit_hit = (pos["tp"], "TP")
            else:
                if bid_high + spread >= pos["sl"]:
                    exit_hit = (max(pos["sl"], O[i] + spread), "SL")
                elif bid_low + spread <= pos["tp"]:
                    exit_hit = (pos["tp"], "TP")
            if exit_hit:
                close_pos(i, *exit_hit)

        # 3) à la clôture : gestion (coupure, durée, trailing) puis nouveau signal
        if pos is not None:
            pos["bars"] += 1
            d = pos["dir"]
            cut, why = strat.should_exit(feat, d, i)
            if not cut and pos["bars"] >= cfg["strategy"]["max_hold_bars"]:
                cut, why = True, "durée max"
            if cut:
                close_pos(i, C[i] if d == 1 else C[i] + spread, why)
            else:
                ns = risk.trail(d, pos["entry"], C[i] if d == 1 else C[i] + spread, pos["sl"], A[i])
                if ns is not None:
                    pos["sl"] = ns

        if pos is None and i < len(feat) - 1:
            sig = strat.evaluate(feat, i)
            if sig.is_trade:
                ok, _ = risk.can_open("BT", now, balance, bt["spread_points"]) if now else (True, "")
                if ok:
                    pending = (sig.direction, sig.atr)

        unreal = 0.0
        if pos is not None:
            unreal = (C[i] - pos["entry"]) * pos["dir"] * pos["vol"] * cs
        equity_curve.append(balance + unreal)

    if pos is not None:
        close_pos(len(feat) - 1, C[-1], "fin du test")

    return summarize(trades, equity_curve, float(bt["initial_balance"]))


def summarize(trades: list[Trade], equity: list[float], start: float) -> dict:
    pnl = np.array([t.pnl for t in trades]) if trades else np.array([])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    eq = np.array(equity) if equity else np.array([start])
    peak = np.maximum.accumulate(eq)
    dd = ((peak - eq) / peak).max() * 100 if len(eq) else 0.0
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf") if len(wins) else 0.0
    return {
        "trades": len(trades),
        "winrate_%": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(float(pf), 2),
        "net_profit": round(float(pnl.sum()), 2),
        "final_balance": round(start + float(pnl.sum()), 2),
        "return_%": round(float(pnl.sum()) / start * 100, 2),
        "max_drawdown_%": round(float(dd), 2),
        "avg_trade": round(float(pnl.mean()), 2) if trades else 0.0,
        "trade_list": trades,
        "equity": eq,
    }
