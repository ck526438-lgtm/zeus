"""Modèle adaptatif en ligne : régression logistique entraînée tick après tick.

Objectif : apprendre en continu, sur le marché du moment, la probabilité que le
mid monte d'ici ``horizon`` ticks, à partir des signaux du carnet (OBI, flux de
ticks, momentum, microprice, spread, volatilité).

- Apprentissage SGD O(1) par tick, aucune phase d'entraînement hors ligne.
- Normalisation en ligne (moyenne/variance EWMA) : robuste aux changements de régime.
- Labels différés : les features du tick t sont étiquetées au tick t+horizon.
- Auto-évaluation : le modèle mesure sa propre précision hors échantillon
  (prédiction faite AVANT de voir le label). Son poids dans la décision est
  nul tant qu'il ne bat pas le hasard, et augmente avec sa précision mesurée.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np


class OnlineLogit:
    __slots__ = ("n", "w", "lr", "l2", "horizon", "min_samples", "_mean", "_var", "_alpha",
                 "_pending", "samples", "accuracy", "_acc_alpha", "last_p")

    def __init__(self, n_features: int, horizon: int = 10, lr: float = 0.02, l2: float = 1e-4,
                 norm_halflife: int = 2_000, acc_halflife: int = 500,
                 min_samples: int = 300) -> None:
        self.n = n_features
        self.w = np.zeros(n_features + 1)               # + biais
        self.lr = lr
        self.l2 = l2
        self.horizon = horizon
        self.min_samples = min_samples
        self._mean = np.zeros(n_features)
        self._var = np.ones(n_features)
        self._alpha = 1.0 - 0.5 ** (1.0 / norm_halflife)
        self._acc_alpha = 1.0 - 0.5 ** (1.0 / acc_halflife)
        self._pending: deque[tuple[np.ndarray, float, float]] = deque()
        self.samples = 0
        self.accuracy = 0.5
        self.last_p = 0.5

    def _normalize(self, x: np.ndarray) -> np.ndarray:
        z = np.empty(self.n + 1)
        z[0] = 1.0
        z[1:] = (x - self._mean) / np.sqrt(self._var + 1e-12)
        np.clip(z[1:], -5.0, 5.0, out=z[1:])
        return z

    def _update_norm(self, x: np.ndarray) -> None:
        a = self._alpha
        d = x - self._mean
        self._mean += a * d
        self._var = (1.0 - a) * (self._var + a * d * d)

    def predict(self, z: np.ndarray) -> float:
        s = float(self.w @ z)
        s = max(-30.0, min(30.0, s))
        return 1.0 / (1.0 + math.exp(-s))

    def step(self, x: np.ndarray, mid: float) -> float:
        """Ajoute l'observation courante, apprend sur l'observation mûre, retourne P(hausse)."""
        # 1) apprendre sur l'observation d'il y a ``horizon`` ticks
        if len(self._pending) >= self.horizon:
            z_old, mid_old, p_old = self._pending.popleft()
            move = mid - mid_old
            if move != 0.0:
                y = 1.0 if move > 0 else 0.0
                hit = 1.0 if (p_old > 0.5) == (y == 1.0) else 0.0
                if self.samples >= 50:     # précision mesurée sur des prédictions faites à l'aveugle
                    self.accuracy += self._acc_alpha * (hit - self.accuracy)
                grad = (self.predict(z_old) - y) * z_old
                grad[1:] += self.l2 * self.w[1:]
                self.w -= self.lr * grad
                self.samples += 1
        # 2) prédire sur l'observation courante
        self._update_norm(x)
        z = self._normalize(x)
        p = self.predict(z)
        self._pending.append((z, mid, p))
        self.last_p = p
        return p

    @property
    def edge(self) -> float:
        """Signal dans [-1, 1] : 2·P(hausse) − 1."""
        return 2.0 * self.last_p - 1.0

    @property
    def confidence(self) -> float:
        """Poids à accorder au modèle, dans [0, 1] : 0 tant qu'il ne bat pas 50 %.

        52 % -> 0.2 ; 55 % -> 0.5 ; >= 60 % -> 1.0.
        """
        if self.samples < self.min_samples:
            return 0.0
        return max(0.0, min(1.0, (self.accuracy - 0.5) * 10.0))

    def reset_pending(self) -> None:
        self._pending.clear()
