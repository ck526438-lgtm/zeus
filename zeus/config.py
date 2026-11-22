"""Chargement de la configuration (YAML + variables d'environnement)."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "mode": "demo",
    "mt5": {"login": 0, "password": "", "server": "", "path": "", "magic": 777001},
    "trading": {
        "symbols": ["XAUUSD"], "timeframe": "M1", "loop_seconds": 1,
        "history_bars": 600, "max_positions_per_symbol": 1, "max_open_positions": 3,
        "deviation_points": 20, "sessions_utc": [[7, 20]],
    },
    "strategy": {
        "ema_fast": 9, "ema_slow": 21, "ema_trend": 100, "rsi_period": 14,
        "atr_period": 14, "adx_period": 14, "adx_min": 18, "min_score": 0.60,
        "use_model": True, "model_path": "models/zeus_model.joblib",
        "model_min_proba": 0.58, "exit_on_reverse": True, "exit_score": 0.55,
        "max_hold_bars": 30,
    },
    "risk": {
        "risk_per_trade_pct": 0.5, "sl_atr_mult": 1.5, "tp_atr_mult": 2.0,
        "breakeven_atr": 1.0, "trailing_atr": 1.2, "max_spread_points": 40,
        "max_daily_loss_pct": 3.0, "max_daily_trades": 60,
        "max_consecutive_losses": 5, "pause_minutes_after_losses": 60,
        "cooldown_seconds": 30,
    },
    "backtest": {
        "initial_balance": 1000, "spread_points": 25, "commission_per_lot": 7.0,
        "point": 0.01, "contract_size": 100,
    },
    "telegram": {"enabled": False, "token": "", "chat_id": ""},
    "logging": {"journal": "data/journal.csv", "level": "INFO"},
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path = "config.yaml") -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            cfg = _merge(cfg, yaml.safe_load(f) or {})

    env = os.environ
    if env.get("ZEUS_MT5_LOGIN"):
        cfg["mt5"]["login"] = int(env["ZEUS_MT5_LOGIN"])
    if env.get("ZEUS_MT5_PASSWORD"):
        cfg["mt5"]["password"] = env["ZEUS_MT5_PASSWORD"]
    if env.get("ZEUS_MT5_SERVER"):
        cfg["mt5"]["server"] = env["ZEUS_MT5_SERVER"]
    if env.get("ZEUS_TG_TOKEN"):
        cfg["telegram"]["token"] = env["ZEUS_TG_TOKEN"]
    if env.get("ZEUS_TG_CHAT"):
        cfg["telegram"]["chat_id"] = env["ZEUS_TG_CHAT"]
    return cfg
