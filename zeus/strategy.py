"""Cerveau de Zeus : analyse multi-indicateurs + filtre IA.

Le score (0 à 1) mesure à quel point les indicateurs sont d'accord sur une
direction. Zeus n'entre que si :
  1. le marché a une tendance (ADX >= adx_min),
  2. le score dépasse `min_score`,
  3. le modèle IA (s'il est entraîné) confirme avec une probabilité >= model_min_proba.
L'analyse se fait UNIQUEMENT sur des bougies clôturées (pas de repaint).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .features import FEATURE_COLUMNS, build_features

BUY, SELL, NONE = 1, -1, 0


@dataclass
class Signal:
    direction: int          # 1 achat, -1 vente, 0 rien
    score: float            # accord des indicateurs (0-1)
    proba: float | None     # confiance du modèle IA (None si pas de modèle)
    atr: float
    price: float
    reason: str

    @property
    def is_trade(self) -> bool:
        return self.direction != NONE


def score_row(r: pd.Series) -> tuple[float, float]:
    """Retourne (score_achat, score_vente) entre 0 et 1."""
    checks_buy = [
        (r["close"] > r["ema_trend"], 0.20),
        (r["ema_fast"] > r["ema_slow"], 0.20),
        (50 < r["rsi"] < 72, 0.15),
        (r["rsi_slope"] > 0, 0.10),
        (r["macd_hist"] > 0, 0.15),
        (r["macd_slope"] > 0, 0.10),
        (r["close"] > r["hh10"], 0.10),
    ]
    checks_sell = [
        (r["close"] < r["ema_trend"], 0.20),
        (r["ema_fast"] < r["ema_slow"], 0.20),
        (28 < r["rsi"] < 50, 0.15),
        (r["rsi_slope"] < 0, 0.10),
        (r["macd_hist"] < 0, 0.15),
        (r["macd_slope"] < 0, 0.10),
        (r["close"] < r["ll10"], 0.10),
    ]
    sb = sum(w for ok, w in checks_buy if bool(ok))
    ss = sum(w for ok, w in checks_sell if bool(ok))
    return float(sb), float(ss)


class Strategy:
    def __init__(self, params: dict, model=None):
        self.p = params
        self.model = model if params.get("use_model", True) else None

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return build_features(df, self.p)

    def evaluate(self, feat: pd.DataFrame, i: int = -1) -> Signal:
        """Évalue la bougie clôturée `i` d'un DataFrame déjà préparé."""
        r = feat.iloc[i]
        price, a = float(r["close"]), float(r["atr"])
        if r[FEATURE_COLUMNS].isna().any() or not np.isfinite(a) or a <= 0:
            return Signal(NONE, 0.0, None, a, price, "données insuffisantes")
        if r["adx"] < self.p["adx_min"]:
            return Signal(NONE, 0.0, None, a, price, f"pas de tendance (ADX {r['adx']:.1f})")

        sb, ss = score_row(r)
        direction, score = (BUY, sb) if sb >= ss else (SELL, ss)
        if score < self.p["min_score"]:
            return Signal(NONE, score, None, a, price, f"score faible {score:.2f}")

        proba = None
        if self.model is not None:
            proba = self.model.predict_proba(r[FEATURE_COLUMNS].to_frame().T, direction)
            if proba < self.p["model_min_proba"]:
                return Signal(NONE, score, proba, a, price, f"IA refuse ({proba:.2f})")

        side = "ACHAT" if direction == BUY else "VENTE"
        txt = f"{side} score {score:.2f}" + (f" IA {proba:.2f}" if proba is not None else "")
        return Signal(direction, score, proba, a, price, txt)

    def should_exit(self, feat: pd.DataFrame, position_dir: int, i: int = -1) -> tuple[bool, str]:
        """Coupe le trade si l'analyse s'est retournée contre la position."""
        if not self.p.get("exit_on_reverse", True):
            return False, ""
        r = feat.iloc[i]
        if r[FEATURE_COLUMNS].isna().any():
            return False, ""
        sb, ss = score_row(r)
        against = ss if position_dir == BUY else sb
        if against >= self.p["exit_score"]:
            return True, f"analyse inversée ({against:.2f})"
        return False, ""
