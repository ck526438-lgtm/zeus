"""Indicateurs de marché partagés, mis à jour une seule fois par tick (O(1) amorti).

- Bougies M1 reconstruites à partir des ticks -> ATR(14) M1 et ATR « normal »
  (moyenne lente), dont le rapport donne le régime de volatilité.
- Micro-range : plus haut / plus bas des ``range_window_s`` dernières secondes
  (deques monotones, hors tick courant) -> détection de cassure.
- Vitesse : variation du mid sur ``velocity_window_ms`` (en points).
- VWAP intrajournalier (pondéré par le volume, ou par tick quand le broker ne
  fournit pas de volume, ce qui est le cas de l'or en CFD) et EMA courte.
- Calendrier : sessions autorisées, ouvertures de session et fenêtres de news
  en heure UTC ; détection de choc de vitesse (news non annoncée).

Le temps de référence est l'horodatage du tick (heure serveur MT5), converti en
UTC avec ``utc_offset_ms`` (détecté automatiquement en temps réel).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

MS_DAY = 86_400_000


def parse_windows(spec: str) -> list[tuple[int, int]]:
    """'07:00-11:00,13:30-17:00' -> [(420, 660), (810, 1020)] en minutes UTC.
    Une fenêtre qui passe minuit ('22:00-02:00') est acceptée."""
    out: list[tuple[int, int]] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        a, b = part.split("-")
        out.append((_hm(a), _hm(b)))
    return out


def _hm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def in_windows(minute: int, windows: list[tuple[int, int]]) -> bool:
    for a, b in windows:
        if (a <= minute < b) if a <= b else (minute >= a or minute < b):
            return True
    return False


class NewsCalendar:
    """Heures de news : 'HH:MM' (tous les jours ouvrés) ou 'AAAA-MM-JJ HH:MM' (date précise)."""

    def __init__(self, spec: str, before_min: float, after_min: float) -> None:
        self.daily: list[int] = []
        self.dated: list[int] = []        # en ms UTC
        for item in filter(None, (x.strip() for x in spec.split(","))):
            if " " in item or "T" in item:
                dt = datetime.fromisoformat(item.replace(" ", "T")).replace(tzinfo=timezone.utc)
                self.dated.append(int(dt.timestamp() * 1000))
            else:
                self.daily.append(_hm(item))
        self.before_ms = int(before_min * 60_000)
        self.after_ms = int(after_min * 60_000)

    def blocked(self, utc_ms: int) -> bool:
        for t in self.dated:
            if t - self.before_ms <= utc_ms <= t + self.after_ms:
                return True
        if self.daily:
            day0 = utc_ms - utc_ms % MS_DAY
            weekday = (utc_ms // MS_DAY + 3) % 7     # 0 = lundi (1970-01-01 était un jeudi)
            if weekday < 5:
                for m in self.daily:
                    t = day0 + m * 60_000
                    if t - self.before_ms <= utc_ms <= t + self.after_ms:
                        return True
        return False


class _WindowExtremum:
    """Max (ou min) glissant sur une fenêtre de temps, deque monotone."""

    __slots__ = ("_dq", "_win", "_sign")

    def __init__(self, window_ms: int, is_max: bool) -> None:
        self._dq: deque[tuple[int, float]] = deque()
        self._win = window_ms
        self._sign = 1.0 if is_max else -1.0

    def push(self, ts: int, v: float) -> None:
        dq, sv = self._dq, self._sign * v
        while dq and self._sign * dq[-1][1] <= sv:
            dq.pop()
        dq.append((ts, v))

    def value(self, now: int) -> float | None:
        dq = self._dq
        while dq and dq[0][0] < now - self._win:
            dq.popleft()
        return dq[0][1] if dq else None

    def span_ms(self, now: int) -> int:
        return now - self._dq[0][0] if self._dq else 0


@dataclass(slots=True)
class Regime:
    label: str = "INIT"      # INIT, CHOC, NEWS, OUVERTURE, TENDANCE, CALME, NORMAL, HORS-SESSION
    momentum_allowed: bool = False
    meanrev_allowed: bool = False


class MarketFeatures:
    def __init__(self, s, point: float) -> None:  # type: ignore[no-untyped-def]
        self.s = s
        self.point = point
        self.utc_offset_ms: int | None = int(s.server_utc_offset_hours * 3_600_000)
        self.auto_offset = False
        self._mom_win = parse_windows(s.momentum_sessions_utc)
        self._mr_win = parse_windows(s.meanrev_sessions_utc)
        self._opens = [_hm(x) for x in s.session_opens_utc.split(",") if x.strip()]
        self.news = NewsCalendar(s.news_times_utc, s.news_before_min, s.news_after_min)
        rw = int(s.range_window_s * 1000)
        self._hi = _WindowExtremum(rw, True)
        self._lo = _WindowExtremum(rw, False)
        self._vel: deque[tuple[int, float]] = deque()
        self._vel_win = int(s.velocity_window_ms)
        # bougies M1
        self._bar_min = -1
        self._bar = [0.0, 0.0, 0.0, 0.0]   # o, h, l, c
        self._prev_close: float | None = None
        self.atr = 0.0                      # en prix
        self.atr_base = 0.0
        self.bars = 0
        self._a_base = 2.0 / (s.atr_baseline_bars + 1)
        # VWAP / EMA
        self._vwap_day = -1
        self._pv = 0.0
        self._v = 0.0
        self.vwap = 0.0
        self.ema = 0.0
        self._a_ema = 2.0 / (s.mr_ema_ticks + 1)
        self._shock_until = 0
        self.ticks = 0
        # sorties (valeurs du tick courant)
        self.ts = 0
        self.utc_ms = 0
        self.mid = 0.0
        self.spread_pts = 0.0
        self.range_hi: float | None = None
        self.range_lo: float | None = None
        self.range_span_ms = 0
        self.velocity = 0.0                 # en prix sur la fenêtre
        self.regime = Regime()

    # ------------------------------------------------------------------ #
    def detect_offset(self, server_ms: int, system_utc_ms: int) -> None:
        """Décalage heure serveur / UTC, arrondi à la demi-heure (temps réel uniquement)."""
        half_h = 1_800_000
        self.utc_offset_ms = round((server_ms - system_utc_ms) / half_h) * half_h
        self.auto_offset = True

    @property
    def atr_points(self) -> float:
        return self.atr / self.point

    @property
    def vol_ratio(self) -> float:
        return self.atr / self.atr_base if self.atr_base > 0 else 1.0

    @property
    def ready(self) -> bool:
        return self.bars >= self.s.atr_period

    def seed_bars(self, bars: list[tuple[float, float, float, float]]) -> None:
        """Pré-charge l'ATR à partir de bougies M1 historiques (o, h, l, c), de la plus ancienne
        à la plus récente : le bot est opérationnel dès le démarrage, sans 14 min d'attente."""
        n = self.s.atr_period
        for o, h, l, c in bars:
            tr = h - l if self._prev_close is None else max(
                h - l, abs(h - self._prev_close), abs(l - self._prev_close))
            self.bars += 1
            self.atr = tr if self.bars == 1 else self.atr + (tr - self.atr) / min(self.bars, n)
            self.atr_base = (self.atr if self.bars == 1
                             else self.atr_base + self._a_base * (self.atr - self.atr_base))
            self._prev_close = c

    def update(self, ts_ms: int, bid: float, ask: float, volume: float = 0.0) -> None:
        mid = (bid + ask) * 0.5
        self.ts, self.mid = ts_ms, mid
        self.spread_pts = (ask - bid) / self.point
        self.utc_ms = ts_ms - (self.utc_offset_ms or 0)
        self.ticks += 1

        # micro-range AVANT d'ajouter le tick courant (pour détecter la cassure)
        self.range_hi = self._hi.value(ts_ms)
        self.range_lo = self._lo.value(ts_ms)
        self.range_span_ms = self._hi.span_ms(ts_ms)
        self._hi.push(ts_ms, mid)
        self._lo.push(ts_ms, mid)

        # vitesse
        vq = self._vel
        vq.append((ts_ms, mid))
        while len(vq) > 1 and vq[0][0] < ts_ms - self._vel_win:
            vq.popleft()
        self.velocity = mid - vq[0][1]

        self._update_bars(ts_ms, mid)

        # VWAP intrajournalier (remis à zéro à 00:00 UTC), EMA courte
        day = self.utc_ms // MS_DAY
        if day != self._vwap_day:
            self._vwap_day, self._pv, self._v = day, 0.0, 0.0
        w = volume if volume > 0 else 1.0
        self._pv += mid * w
        self._v += w
        self.vwap = self._pv / self._v
        self.ema = mid if self.ticks == 1 else self.ema + self._a_ema * (mid - self.ema)

        self.regime = self._classify()

    def _update_bars(self, ts_ms: int, mid: float) -> None:
        minute = ts_ms // 60_000
        b = self._bar
        if minute != self._bar_min:
            if self._bar_min >= 0:                       # clôture de la bougie précédente
                o, h, l, c = b
                tr = h - l if self._prev_close is None else max(
                    h - l, abs(h - self._prev_close), abs(l - self._prev_close))
                n = self.s.atr_period
                self.bars += 1
                self.atr = tr if self.bars == 1 else self.atr + (tr - self.atr) / min(self.bars, n)
                self.atr_base = (self.atr if self.bars == 1
                                 else self.atr_base + self._a_base * (self.atr - self.atr_base))
                self._prev_close = c
            self._bar_min = minute
            b[0] = b[1] = b[2] = b[3] = mid
        else:
            if mid > b[1]:
                b[1] = mid
            if mid < b[2]:
                b[2] = mid
            b[3] = mid

    def _classify(self) -> Regime:
        s = self.s
        if not self.ready:
            return Regime("INIT")
        now = self.utc_ms
        atr = max(self.atr, self.point)
        if abs(self.velocity) > s.shock_velocity_atr * atr:
            self._shock_until = self.ts + int(s.shock_pause_s * 1000)
        if self.ts < self._shock_until:
            return Regime("CHOC")
        if self.news.blocked(now):
            return Regime("NEWS")
        minute = int((now % MS_DAY) // 60_000)
        for o in self._opens:
            if 0 <= (minute - o) % 1440 < s.session_open_blackout_min:
                return Regime("OUVERTURE")
        vr = self.vol_ratio
        mom = in_windows(minute, self._mom_win) and s.mom_min_vol_ratio <= vr <= s.mom_max_vol_ratio
        mr = (in_windows(minute, self._mr_win) and vr <= s.mr_max_vol_ratio
              and self.atr_points <= s.mr_max_atr_points)
        if mom and mr:
            label = "NORMAL"
        elif mom:
            label = "TENDANCE" if vr >= 1.0 else "NORMAL"
        elif mr:
            label = "CALME"
        else:
            label = "HORS-SESSION"
        return Regime(label, mom, mr)

    def snapshot(self) -> dict[str, float | str]:
        return {"regime": self.regime.label, "atr_pts": round(self.atr_points, 1),
                "vol_ratio": round(self.vol_ratio, 2),
                "velocity_pts": round(self.velocity / self.point, 1),
                "vwap": round(self.vwap, 3),
                "utc_offset_h": (self.utc_offset_ms or 0) / 3_600_000}


def utc_minute_label(utc_ms: int) -> str:
    m = int((utc_ms % MS_DAY) // 60_000)
    return f"{m // 60:02d}:{m % 60:02d}"


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


__all__ = ["MarketFeatures", "Regime", "NewsCalendar", "parse_windows", "in_windows",
           "utc_minute_label", "clamp", "math"]
