"""Dimensionnement dynamique : risquer un % fixe du capital selon la distance du stop.

lots = (equity × risk%) / (distance_SL_en_points × valeur d'un point pour 1 lot)
valeur d'un point pour 1 lot = SYMBOL_TRADE_TICK_VALUE × point / SYMBOL_TRADE_TICK_SIZE

Le volume est arrondi AU PAS INFÉRIEUR (on ne risque jamais plus que prévu), borné
par volume_max et max_lots. Si même le lot minimum dépasse le risque autorisé de
plus de 50 %, le trade est refusé (0).
"""
from __future__ import annotations

import math


def lots_for_risk(equity: float, risk_pct: float, stop_points: float, money_per_point_1lot: float,
                  volume_min: float, volume_step: float, volume_max: float,
                  max_lots: float) -> float:
    if equity <= 0 or risk_pct <= 0 or stop_points <= 0 or money_per_point_1lot <= 0:
        return 0.0
    budget = equity * risk_pct / 100.0
    risk_1lot = stop_points * money_per_point_1lot
    raw = budget / risk_1lot
    lots = math.floor(raw / volume_step + 1e-9) * volume_step
    lots = min(lots, volume_max, max_lots)
    if lots < volume_min - 1e-12:
        # le lot minimum risquerait trop : on n'accepte qu'un dépassement modéré (<= 50 %)
        return volume_min if volume_min * risk_1lot <= budget * 1.5 else 0.0
    return round(lots, 8)
