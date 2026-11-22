"""Modèle IA de Zeus.

Pour chaque bougie, on regarde ce qui s'est passé ensuite (méthode « triple
barrière ») : un trade ouvert ici aurait-il touché son take profit AVANT son
stop loss ? Le modèle apprend à reconnaître les configurations gagnantes,
séparément pour l'achat et la vente. Validation walk-forward (dans le temps,
jamais aléatoire) pour éviter de se mentir sur la performance.
"""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import precision_score, roc_auc_score

from .features import FEATURE_COLUMNS, build_features


def triple_barrier_labels(feat: pd.DataFrame, sl_mult: float, tp_mult: float,
                          horizon: int) -> tuple[np.ndarray, np.ndarray]:
    """label_long[i] = 1 si un achat à la clôture i touche TP avant SL dans `horizon` bougies."""
    c = feat["close"].to_numpy()
    h = feat["high"].to_numpy()
    lo = feat["low"].to_numpy()
    a = feat["atr"].to_numpy()
    n = len(c)
    long_y = np.full(n, np.nan)
    short_y = np.full(n, np.nan)
    for i in range(n - 1):
        if not np.isfinite(a[i]) or a[i] <= 0:
            continue
        end = min(n, i + 1 + horizon)
        if end - (i + 1) < horizon:
            continue
        tp_l, sl_l = c[i] + tp_mult * a[i], c[i] - sl_mult * a[i]
        tp_s, sl_s = c[i] - tp_mult * a[i], c[i] + sl_mult * a[i]
        rl = rs = 0
        for j in range(i + 1, end):
            if rl == 0:
                if lo[j] <= sl_l:      # pire cas d'abord : SL avant TP sur la même bougie
                    rl = -1
                elif h[j] >= tp_l:
                    rl = 1
            if rs == 0:
                if h[j] >= sl_s:
                    rs = -1
                elif lo[j] <= tp_s:
                    rs = 1
            if rl and rs:
                break
        long_y[i] = 1.0 if rl == 1 else 0.0
        short_y[i] = 1.0 if rs == 1 else 0.0
    return long_y, short_y


class ZeusModel:
    def __init__(self):
        self.models: dict[int, HistGradientBoostingClassifier] = {}
        self.report: dict = {}
        self.train_end = None   # dernière bougie vue à l'entraînement

    @staticmethod
    def _new():
        return HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.05, max_depth=5,
            l2_regularization=1.0, min_samples_leaf=40, random_state=42,
        )

    def fit(self, df: pd.DataFrame, strat_params: dict, risk_params: dict,
            horizon: int = 30, n_splits: int = 4, holdout: float = 0.3) -> dict:
        """Entraîne sur les (1 - holdout) premières bougies ; le reste est gardé
        pour un backtest honnête (données jamais vues par le modèle)."""
        cut = int(len(df) * (1 - holdout))
        df = df.iloc[:cut]
        if "time" in df:
            self.train_end = pd.Timestamp(df["time"].iloc[-1])
        feat = build_features(df, strat_params)
        ly, sy = triple_barrier_labels(feat, risk_params["sl_atr_mult"],
                                       risk_params["tp_atr_mult"], horizon)
        X = feat[FEATURE_COLUMNS]
        report = {}
        for direction, y in ((1, ly), (-1, sy)):
            mask = X.notna().all(axis=1).to_numpy() & np.isfinite(y)
            Xd, yd = X[mask].to_numpy(), y[mask].astype(int)
            if len(yd) < 500 or len(set(yd)) < 2:
                raise ValueError("Pas assez de données pour entraîner (min ~500 bougies utilisables).")

            # Walk-forward : on entraîne sur le passé, on teste sur le futur.
            folds = []
            size = len(yd) // (n_splits + 1)
            for k in range(1, n_splits + 1):
                tr_end, te_end = size * k, size * (k + 1)
                m = self._new().fit(Xd[:tr_end], yd[:tr_end])
                p = m.predict_proba(Xd[tr_end:te_end])[:, 1]
                yt = yd[tr_end:te_end]
                auc = roc_auc_score(yt, p) if len(set(yt)) > 1 else float("nan")
                pred = (p >= 0.58).astype(int)
                prec = precision_score(yt, pred, zero_division=0)
                folds.append({"auc": round(float(auc), 3), "precision@0.58": round(float(prec), 3),
                              "trades": int(pred.sum()), "base_winrate": round(float(yt.mean()), 3)})
            self.models[direction] = self._new().fit(Xd, yd)
            report["achat" if direction == 1 else "vente"] = folds
        self.report = report
        return report

    def predict_proba(self, X_row: pd.DataFrame, direction: int) -> float:
        m = self.models.get(direction)
        if m is None:
            return 1.0
        return float(m.predict_proba(X_row[FEATURE_COLUMNS].astype(float).to_numpy())[0, 1])

    def save(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"models": self.models, "report": self.report,
                     "train_end": self.train_end}, path)

    @classmethod
    def load(cls, path: str | Path) -> "ZeusModel | None":
        p = Path(path)
        if not p.exists():
            return None
        data = joblib.load(p)
        m = cls()
        m.models, m.report = data["models"], data.get("report", {})
        m.train_end = data.get("train_end")
        return m
