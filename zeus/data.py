"""Chargement / téléchargement / génération de données historiques."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def load_csv(path: str | Path) -> pd.DataFrame:
    """CSV avec colonnes time,open,high,low,close (export MT5 accepté aussi)."""
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [c.strip("<>").lower() for c in df.columns]
    if "date" in df and "time" in df:          # export MT5 : <DATE> <TIME> séparés
        df["time"] = df["date"].astype(str) + " " + df["time"].astype(str)
    elif "date" in df:
        df["time"] = df["date"]
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)


def synthetic(n: int = 20000, start_price: float = 2000.0, seed: int = 7) -> pd.DataFrame:
    """Données M1 artificielles (régimes de tendance + bruit) pour tester sans MT5."""
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.normal(0, 0.00004, n // 400 + 1), 400)[:n]
    vol = np.repeat(rng.uniform(0.0002, 0.0006, n // 300 + 1), 300)[:n]
    ret = drift + rng.normal(0, 1, n) * vol
    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.normal(0, 1, n)) * vol * close * 0.6
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 1, n)) * vol * close * 0.6
    time = pd.date_range("2026-01-05", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({"time": time, "open": open_, "high": high, "low": low, "close": close})
