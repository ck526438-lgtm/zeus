"""Configuration à chaud : modifier certains réglages SANS redémarrer le bot.

Le moteur surveille ``data/zeus_hft_hot.json`` toutes les 5 s. Exemple :

    {"trading_enabled": false}                     -> pause manuelle (réduction seule)
    {"max_spread_points": 30, "risk_per_trade_pct": 0.25, "velocity_entry_atr": 0.5}

Seules les clés de la liste blanche sont acceptées ; les valeurs sont validées par le
modèle Pydantic complet avant d'être appliquées. Toute erreur est ignorée et loggée.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from zeus_hft.config.settings import Settings

log = logging.getLogger("zeus_hft.hot")

HOT_KEYS = frozenset({
    "max_spread_points", "spread_avg_mult", "min_tp_spread_mult", "risk_per_trade_pct", "max_lots",
    "max_daily_drawdown", "max_daily_loss_pct", "max_consecutive_losses", "max_orders_per_second",
    "velocity_entry_atr", "breakout_buffer_atr", "mom_tp_atr", "mom_sl_atr",
    "mr_entry_dev_atr", "mr_exit_dev_atr", "mr_sl_atr", "mr_stall_velocity_atr", "mr_max_adx",
    "breakeven_at_r", "partial_close_frac", "trail_atr", "exit_cooldown_s", "deviation_points",
})


def read_hot_file(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8") or "{}")


def validate_changes(s: Settings, raw: dict[str, Any]) -> tuple[dict[str, Any], bool | None, list[str]]:
    """Retourne (changements valides, trading_enabled ou None, erreurs)."""
    errors: list[str] = []
    enabled = raw.get("trading_enabled")
    if enabled is not None and not isinstance(enabled, bool):
        errors.append("trading_enabled doit être true ou false")
        enabled = None
    changes = {k: v for k, v in raw.items() if k in HOT_KEYS and getattr(s, k) != v}
    unknown = sorted(set(raw) - HOT_KEYS - {"trading_enabled"})
    if unknown:
        errors.append(f"clés ignorées (non modifiables à chaud) : {', '.join(unknown)}")
    if changes:
        try:
            Settings.model_validate({**s.model_dump(), **changes})
        except Exception as exc:
            errors.append("valeurs refusées : " + " ".join(str(exc).splitlines()[1:3]))
            changes = {}
    return changes, enabled, errors
