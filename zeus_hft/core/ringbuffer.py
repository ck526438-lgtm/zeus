"""Ring buffer numpy à taille fixe : append O(1), aucune allocation dans le hot path."""
from __future__ import annotations

import numpy as np


class RingBuffer:
    __slots__ = ("_buf", "_cap", "_idx", "_count")

    def __init__(self, capacity: int, dtype: type | np.dtype = np.float64) -> None:
        if capacity < 1:
            raise ValueError("capacity doit être >= 1")
        self._buf = np.zeros(capacity, dtype=dtype)
        self._cap = capacity
        self._idx = 0      # position de la prochaine écriture
        self._count = 0

    def append(self, value: float | int) -> None:
        self._buf[self._idx] = value
        self._idx = (self._idx + 1) % self._cap
        if self._count < self._cap:
            self._count += 1

    def clear(self) -> None:
        self._idx = 0
        self._count = 0

    @property
    def full(self) -> bool:
        return self._count == self._cap

    def __len__(self) -> int:
        return self._count

    def last(self, k: int = 0) -> float:
        """k-ième valeur la plus récente (0 = la dernière)."""
        if k >= self._count or k < 0:
            raise IndexError(k)
        return self._buf[(self._idx - 1 - k) % self._cap].item()

    def to_array(self) -> np.ndarray:
        """Copie ordonnée (plus ancien -> plus récent). Hors hot path."""
        if self._count < self._cap:
            return self._buf[: self._count].copy()
        return np.concatenate((self._buf[self._idx:], self._buf[: self._idx]))
