"""Routeur automatique : choisit la stratégie selon le régime de marché.

- Calcule les indicateurs (``MarketFeatures``) une seule fois par tick.
- Fait toujours tourner la stratégie OBI + IA : l'IA continue d'apprendre à
  chaque tick et sert de filtre (veto si elle est confiante dans le sens opposé).
- À plat : interroge les stratégies autorisées par ordre de priorité
  (momentum en session active, mean-reversion en marché calme) ; la première
  qui veut entrer devient « propriétaire » de la position.
- En position : seul le propriétaire gère la sortie (SL/TP/durée propres).
- Après chaque sortie : pause ``exit_cooldown_s``.
- Jamais de grille ni de martingale : une seule position, taille fixe, aucun
  renforcement contre la tendance.
"""
from __future__ import annotations

import time
from typing import Any

from zeus_hft.core.orderbook import OrderBook
from zeus_hft.core.types import Decision, Tick, Urgency
from zeus_hft.strategies.base import Strategy
from zeus_hft.strategies.features import MarketFeatures
from zeus_hft.strategies.hft_strategy import OBIMomentumStrategy
from zeus_hft.strategies.mean_reversion import MeanReversion
from zeus_hft.strategies.momentum_breakout import MomentumBreakout

AI_VETO = 0.30


class StrategyRouter(Strategy):
    name = "auto"

    def __init__(self, s, point: float | None = None, use_model: bool = True,  # type: ignore[no-untyped-def]
                 realtime: bool = False) -> None:
        self.s = s
        self.point = point if point is not None else s.point
        self.mode = s.strategy
        self.realtime = realtime
        self.f = MarketFeatures(s, self.point)
        self.obi = OBIMomentumStrategy(s, self.point, use_model=use_model)
        self.mom = MomentumBreakout(s, self.point)
        self.mr = MeanReversion(s, self.point)
        self.enabled: list[Any] = {"auto": [self.mom, self.mr], "momentum": [self.mom],
                                   "meanrev": [self.mr], "obi": []}[self.mode]
        self.owner: Any = None
        self._was_open = False
        self._cool_until = 0
        self._offset_done = not realtime
        self.last_reason = ""
        self.vetoes = 0
        self.entries: dict[str, int] = {"momentum": 0, "meanrev": 0, "obi": 0}

    # --- mémoire de l'IA (déléguée à la stratégie OBI) ----------------- #
    @property
    def model(self) -> Any:
        return self.obi.model

    @property
    def model_loaded(self) -> bool:
        return self.obi.model_loaded

    def attach_model_file(self, path: str) -> None:
        self.obi.attach_model_file(path)

    def save_model(self) -> None:
        self.obi.save_model()

    def reset(self) -> None:
        self.obi.reset()
        self.mom.reset()
        self.mr.reset()
        self.owner = None

    # ------------------------------------------------------------------ #
    def on_tick(self, book: OrderBook, tick: Tick, position: float, avg_price: float,
                now_ns: int) -> Decision | None:
        top = book.top()
        if top is None:
            return None
        if not self._offset_done and tick.exchange_ts_ms:
            self.f.detect_offset(tick.exchange_ts_ms, int(time.time() * 1000))
            self._offset_done = True
        self.f.update(tick.exchange_ts_ms, top.bid, top.ask, tick.volume)
        obi_dec = self.obi.on_tick(book, tick, position, avg_price, now_ns)
        if self.mode == "obi":
            if obi_dec is not None:
                obi_dec.strategy = "obi"
            return obi_dec

        if position != 0.0:
            self._was_open = True
            owner = self.owner or self.enabled[0]
            self.owner = owner
            dec = owner.evaluate(self.f, top, position, avg_price, now_ns)
            if dec is not None:
                self.last_reason = f"{owner.name}: {dec.reason}"
            return dec

        if self._was_open:                     # la position vient d'être fermée
            self._was_open = False
            self._cool_until = now_ns + int(self.s.exit_cooldown_s * 1e9)
            for st in self.enabled:
                st.reset()
            self.owner = None
        if now_ns < self._cool_until:
            return self._flat("pause après sortie")

        for st in self.enabled:
            dec = st.evaluate(self.f, top, 0.0, 0.0, now_ns)
            if dec is None or dec.target_position == 0.0:
                continue
            direction = 1.0 if dec.target_position > 0 else -1.0
            model = self.obi.model
            if model is not None and model.confidence > 0 and model.edge * direction < -AI_VETO:
                self.vetoes += 1
                self.last_reason = f"{st.name}: veto de l'IA"
                continue
            if self.owner is not st:
                self.entries[st.name] += 1
            self.owner = st
            self.last_reason = f"{st.name}: {dec.reason}"
            return dec
        self.owner = None
        return self._flat(f"attente ({self.f.regime.label})")

    def _flat(self, reason: str) -> Decision:
        self.last_reason = reason
        return Decision(0.0, Urgency.PASSIVE, None, reason, 0.0, None, "")

    def state(self) -> dict[str, float]:
        st: dict[str, Any] = dict(self.obi.state())
        st.update(self.f.snapshot())
        st["strategy"] = self.owner.name if self.owner else "-"
        st["mode"] = self.mode
        st["reason"] = self.last_reason
        st["vetoes"] = self.vetoes
        return st
