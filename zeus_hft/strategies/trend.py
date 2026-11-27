"""Filtre de tendance supérieure : EMA 50/200 en H1 et M15, ADX(14) en M15.

Les bougies M15 et H1 sont construites à partir des bougies M1 (elles-mêmes
issues des ticks) et pré-chargées depuis l'historique MT5 au démarrage.

Règles (XAUUSD : ne jamais scalper contre une forte impulsion H1) :
- Direction H1 : +1 si close > EMA50 > EMA200, −1 si close < EMA50 < EMA200, sinon 0.
- Momentum : autorisé seulement dans le sens H1 ; si H1 est neutre, dans le sens M15.
- Mean-reversion : seulement en range (ADX M15 <= mr_max_adx) et jamais contre H1.
"""
from __future__ import annotations

from dataclasses import dataclass


class TFBars:
    """Bougies d'une unité de temps (en minutes) + EMA rapide/lente + ADX de Wilder."""

    def __init__(self, minutes: int, ema_fast: int, ema_slow: int, adx_period: int = 14,
                 min_sep_atr: float = 0.2) -> None:
        self.minutes = minutes
        self.min_sep_atr = min_sep_atr
        self._af = 2.0 / (ema_fast + 1)
        self._as = 2.0 / (ema_slow + 1)
        self._n = adx_period
        self.ema_fast = 0.0
        self.ema_slow = 0.0
        self.close = 0.0
        self.bars = 0
        self._cur: list[float] | None = None    # o, h, l, c de la bougie en cours
        self._cur_idx = -1
        self._prev: tuple[float, float, float] | None = None   # h, l, c précédents
        self._tr = self._pdm = self._mdm = 0.0
        self._dx_sum = 0.0
        self._dx_n = 0
        self.adx = 0.0
        self.plus_di = self.minus_di = 0.0

    def _close_bar(self, o: float, h: float, l: float, c: float) -> None:
        self.bars += 1
        if self.bars == 1:
            self.ema_fast = self.ema_slow = c
        else:
            self.ema_fast += self._af * (c - self.ema_fast)
            self.ema_slow += self._as * (c - self.ema_slow)
        self.close = c
        if self._prev is not None:
            ph, pl, pc = self._prev
            tr = max(h - l, abs(h - pc), abs(l - pc))
            up, dn = h - ph, pl - l
            pdm = up if (up > dn and up > 0) else 0.0
            mdm = dn if (dn > up and dn > 0) else 0.0
            n = self._n
            k = self.bars - 1                        # nombre de variations déjà vues
            if k <= n:                               # phase d'initialisation (moyennes simples)
                self._tr += tr
                self._pdm += pdm
                self._mdm += mdm
            else:                                    # lissage de Wilder
                self._tr += tr - self._tr / n
                self._pdm += pdm - self._pdm / n
                self._mdm += mdm - self._mdm / n
            if k >= n and self._tr > 0:
                self.plus_di = 100.0 * self._pdm / self._tr
                self.minus_di = 100.0 * self._mdm / self._tr
                s = self.plus_di + self.minus_di
                dx = 100.0 * abs(self.plus_di - self.minus_di) / s if s > 0 else 0.0
                if self._dx_n < n:
                    self._dx_sum += dx
                    self._dx_n += 1
                    self.adx = self._dx_sum / self._dx_n
                else:
                    self.adx = (self.adx * (n - 1) + dx) / n
        self._prev = (h, l, c)

    def on_m1(self, minute_index: int, o: float, h: float, l: float, c: float) -> None:
        """Ajoute une bougie M1 clôturée (minute_index = minutes depuis l'epoch, UTC ou serveur)."""
        idx = minute_index // self.minutes
        cur = self._cur
        if cur is None or idx != self._cur_idx:
            if cur is not None:
                self._close_bar(*cur)
            self._cur, self._cur_idx = [o, h, l, c], idx
        else:
            cur[1] = max(cur[1], h)
            cur[2] = min(cur[2], l)
            cur[3] = c

    def seed(self, bars: list[tuple[float, float, float, float]]) -> None:
        """Historique MT5 (bougies clôturées, de la plus ancienne à la plus récente)."""
        for b in bars:
            self._close_bar(*b)

    @property
    def atr(self) -> float:
        k = self.bars - 1
        if k <= 0:
            return 0.0
        return self._tr / self._n if k >= self._n else self._tr / k

    @property
    def direction(self) -> int:
        c = self._cur[3] if self._cur is not None else self.close
        # écart minimal entre EMA rapide et lente : pas de « tendance » sur un marché plat
        if abs(self.ema_fast - self.ema_slow) < self.min_sep_atr * self.atr:
            return 0
        if c > self.ema_fast > self.ema_slow:
            return 1
        if c < self.ema_fast < self.ema_slow:
            return -1
        return 0


@dataclass(slots=True)
class TrendView:
    ready: bool
    h1_dir: int
    m15_dir: int
    adx_m15: float


class TrendFilter:
    def __init__(self, s) -> None:  # type: ignore[no-untyped-def]
        self.s = s
        self.enabled = bool(s.trend_filter)
        self.m15 = TFBars(15, s.trend_ema_fast, s.trend_ema_slow, s.adx_period)
        self.h1 = TFBars(60, s.trend_ema_fast, s.trend_ema_slow, s.adx_period)

    def on_m1(self, minute_index: int, o: float, h: float, l: float, c: float) -> None:
        self.m15.on_m1(minute_index, o, h, l, c)
        self.h1.on_m1(minute_index, o, h, l, c)

    @property
    def ready(self) -> bool:
        return (self.h1.bars >= self.s.trend_min_bars and self.m15.bars >= self.s.trend_min_bars
                and self.m15.bars > 2 * self.s.adx_period)

    def view(self) -> TrendView:
        return TrendView(self.ready, self.h1.direction, self.m15.direction, self.m15.adx)

    def allows_momentum(self, direction: int) -> tuple[bool, str]:
        if not self.enabled:
            return True, ""
        if not self.ready:
            return False, "tendance H1/M15 pas encore calculée"
        h1 = self.h1.direction
        if h1 != 0:
            return (h1 == direction), ("" if h1 == direction else "contre la tendance H1")
        m15 = self.m15.direction
        return (m15 == direction), ("" if m15 == direction else "H1 neutre et M15 opposé/neutre")

    def allows_meanrev(self, direction: int) -> tuple[bool, str]:
        if not self.enabled:
            return True, ""
        if not self.ready:
            return False, "tendance H1/M15 pas encore calculée"
        if self.m15.adx > self.s.mr_max_adx:
            return False, f"ADX M15 {self.m15.adx:.0f} : marché en tendance, pas de range"
        if self.h1.direction == -direction:
            return False, "contre l'impulsion H1"
        return True, ""
