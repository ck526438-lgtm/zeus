"""Construction des features utilisées par la stratégie et le modèle IA."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind

FEATURE_COLUMNS = [
    "ret_1", "ret_3", "ret_10", "dist_fast", "dist_slow", "dist_trend",
    "ema_spread", "rsi", "rsi_slope", "adx", "macd_hist", "macd_slope",
    "atr_norm", "bb_pos", "body", "upper_wick", "lower_wick", "range_pos",
    "hour_sin", "hour_cos",
]


def build_features(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Ajoute indicateurs + features normalisées. `df` doit avoir time/open/high/low/close."""
    d = df.copy()
    c = d["close"]
    d["ema_fast"] = ind.ema(c, p["ema_fast"])
    d["ema_slow"] = ind.ema(c, p["ema_slow"])
    d["ema_trend"] = ind.ema(c, p["ema_trend"])
    d["rsi"] = ind.rsi(c, p["rsi_period"])
    d["atr"] = ind.atr(d, p["atr_period"])
    d["adx"] = ind.adx(d, p["adx_period"])
    _, _, hist = ind.macd(c)
    d["macd_hist"] = hist
    up, mid, lo = ind.bollinger(c)

    a = d["atr"].replace(0, np.nan)
    d["ret_1"] = c.pct_change(1)
    d["ret_3"] = c.pct_change(3)
    d["ret_10"] = c.pct_change(10)
    d["dist_fast"] = (c - d["ema_fast"]) / a
    d["dist_slow"] = (c - d["ema_slow"]) / a
    d["dist_trend"] = (c - d["ema_trend"]) / a
    d["ema_spread"] = (d["ema_fast"] - d["ema_slow"]) / a
    d["rsi_slope"] = d["rsi"].diff(3)
    d["macd_hist"] = d["macd_hist"] / a
    d["macd_slope"] = d["macd_hist"].diff(2)
    d["atr_norm"] = d["atr"] / c
    d["bb_pos"] = (c - lo) / (up - lo).replace(0, np.nan)
    rng = (d["high"] - d["low"]).replace(0, np.nan)
    d["body"] = (d["close"] - d["open"]) / rng
    d["upper_wick"] = (d["high"] - d[["open", "close"]].max(axis=1)) / rng
    d["lower_wick"] = (d[["open", "close"]].min(axis=1) - d["low"]) / rng
    hh = d["high"].rolling(20).max()
    ll = d["low"].rolling(20).min()
    d["range_pos"] = (c - ll) / (hh - ll).replace(0, np.nan)
    d["hh10"] = d["high"].rolling(10).max().shift(1)
    d["ll10"] = d["low"].rolling(10).min().shift(1)

    hours = pd.to_datetime(d["time"]).dt.hour if "time" in d else pd.Series(0, index=d.index)
    d["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    d["hour_cos"] = np.cos(2 * np.pi * hours / 24)

    d[FEATURE_COLUMNS] = d[FEATURE_COLUMNS].replace([np.inf, -np.inf], np.nan)
    return d
