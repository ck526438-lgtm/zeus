"""Carnet d'ordres local L2 maintenu en temps réel (snapshot + deltas).

Protocole de synchronisation (modèle Binance « diff depth », transposable à la
plupart des exchanges qui numérotent leurs updates) :

1. On s'abonne au flux de deltas AVANT de demander le snapshot REST.
   Tant que le carnet n'est pas synchronisé, les deltas sont mis en tampon.
2. On applique le snapshot (``last_update_id = L``).
3. On rejoue le tampon : les deltas dont ``final_id <= L`` sont ignorés ; le
   premier delta retenu doit vérifier ``first_id <= L + 1 <= final_id``.
4. Ensuite, chaque delta doit vérifier ``first_id == last_update_id + 1``
   (contigu). Sinon : ``SequenceGapError`` -> le carnet est invalidé et
   l'appelant doit redemander un snapshot.

Choix de performance (hot path) :
- ``SortedDict`` (sortedcontainers) : insertion/suppression O(log n), accès au
  meilleur prix O(1) amorti, indexation positionnelle sans copie.
- Les métriques (mid, spread, imbalance, microprice) se calculent sur les
  quelques premiers niveaux en Python pur : pour n <= ~20, c'est plus rapide
  que de créer un ``numpy.ndarray`` à chaque tick. ``numpy`` n'est utilisé que
  pour l'export de profondeur (``depth_arrays``), consommé par la stratégie ou
  le backtest.
- Aucune I/O, aucun log dans ce module.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Sequence, TypeAlias

import numpy as np
import orjson
from sortedcontainers import SortedDict

Num: TypeAlias = float | str
LevelInput: TypeAlias = Sequence[Num]           # (price, qty) ; str accepté (format exchange)
Levels: TypeAlias = Iterable[LevelInput]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class OrderBookError(Exception):
    """Erreur générique du carnet."""


class SequenceGapError(OrderBookError):
    """Un ou plusieurs deltas ont été perdus : resynchronisation nécessaire."""

    def __init__(self, expected: int, got_first: int, got_final: int) -> None:
        super().__init__(
            f"trou de séquence : attendu {expected}, reçu [{got_first}, {got_final}]"
        )
        self.expected = expected
        self.got_first = got_first
        self.got_final = got_final


class CrossedBookError(OrderBookError):
    """best_bid >= best_ask après mise à jour : état incohérent."""


class BufferOverflowError(OrderBookError):
    """Le tampon de pré-synchronisation a débordé (snapshot trop lent)."""


# --------------------------------------------------------------------------- #
# Structures de données
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class DepthDelta:
    """Delta normalisé, indépendant de l'exchange."""

    first_update_id: int
    final_update_id: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    exchange_ts_ms: int = 0
    recv_ts_ns: int = 0


@dataclass(slots=True, frozen=True)
class BookTop:
    """Photo instantanée du haut du carnet (immuable, sûre à partager)."""

    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    mid: float
    spread: float
    microprice: float
    update_id: int
    ts_ns: int


# --------------------------------------------------------------------------- #
# Carnet d'ordres
# --------------------------------------------------------------------------- #
class OrderBook:
    """Carnet L2 local pour un seul symbole. Non thread-safe (conçu pour asyncio)."""

    __slots__ = (
        "symbol",
        "max_depth",
        "_bids",
        "_asks",
        "_buffer",
        "_max_buffer",
        "last_update_id",
        "synced",
        "last_update_ns",
        "last_exchange_ts_ms",
        "updates_applied",
    )

    def __init__(self, symbol: str, max_depth: int = 1000, max_buffer: int = 10_000) -> None:
        if max_depth < 1:
            raise ValueError("max_depth doit être >= 1")
        self.symbol: str = symbol
        self.max_depth: int = max_depth
        # Les deux côtés sont triés par prix croissant :
        #   meilleur bid = dernier élément, meilleur ask = premier élément.
        self._bids: SortedDict = SortedDict()
        self._asks: SortedDict = SortedDict()
        self._buffer: deque[DepthDelta] = deque()
        self._max_buffer: int = max_buffer
        self.last_update_id: int = -1
        self.synced: bool = False
        self.last_update_ns: int = 0
        self.last_exchange_ts_ms: int = 0
        self.updates_applied: int = 0

    # ------------------------------------------------------------------ #
    # Cycle de vie
    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        """Invalide le carnet (à appeler après déconnexion ou trou de séquence)."""
        self._bids.clear()
        self._asks.clear()
        self._buffer.clear()
        self.last_update_id = -1
        self.synced = False

    def apply_snapshot(
        self,
        bids: Levels,
        asks: Levels,
        last_update_id: int,
        exchange_ts_ms: int = 0,
        recv_ts_ns: int | None = None,
    ) -> int:
        """Remplace intégralement le carnet puis rejoue les deltas en tampon.

        Retourne le nombre de deltas rejoués. Lève ``SequenceGapError`` si le
        tampon ne se raccorde pas au snapshot (le carnet reste alors invalide).
        """
        self._bids.clear()
        self._asks.clear()
        _load_side(self._bids, bids)
        _load_side(self._asks, asks)
        self._trim()
        self.last_update_id = last_update_id
        self.last_exchange_ts_ms = exchange_ts_ms
        self.last_update_ns = recv_ts_ns if recv_ts_ns is not None else time.perf_counter_ns()
        self.synced = True
        self._check_crossed()

        replayed = 0
        buffered = self._buffer
        self._buffer = deque()
        first = True
        for d in buffered:
            if d.final_update_id <= self.last_update_id:
                continue  # déjà inclus dans le snapshot
            if first and not (d.first_update_id <= self.last_update_id + 1 <= d.final_update_id):
                expected = self.last_update_id + 1
                self.reset()
                raise SequenceGapError(expected, d.first_update_id, d.final_update_id)
            first = False
            self._apply(d)
            replayed += 1
        return replayed

    def apply_delta(self, delta: DepthDelta) -> bool:
        """Applique un delta. Retourne True si le carnet a changé.

        - Carnet non synchronisé : le delta est mis en tampon (retour False).
        - Delta obsolète (déjà couvert) : ignoré (retour False).
        - Trou de séquence : carnet invalidé + ``SequenceGapError``.
        """
        if not self.synced:
            if len(self._buffer) >= self._max_buffer:
                self._buffer.clear()
                raise BufferOverflowError(
                    f"tampon > {self._max_buffer} deltas sans snapshot"
                )
            self._buffer.append(delta)
            return False

        if delta.final_update_id <= self.last_update_id:
            return False

        expected = self.last_update_id + 1
        if delta.first_update_id > expected:
            self.reset()
            raise SequenceGapError(expected, delta.first_update_id, delta.final_update_id)

        self._apply(delta)
        return True

    # ------------------------------------------------------------------ #
    # Interne
    # ------------------------------------------------------------------ #
    def _apply(self, d: DepthDelta) -> None:
        bids = self._bids
        asks = self._asks
        for p, q in d.bids:
            if q == 0.0:
                bids.pop(p, None)
            else:
                bids[p] = q
        for p, q in d.asks:
            if q == 0.0:
                asks.pop(p, None)
            else:
                asks[p] = q
        self._trim()
        self.last_update_id = d.final_update_id
        self.last_exchange_ts_ms = d.exchange_ts_ms
        self.last_update_ns = d.recv_ts_ns or time.perf_counter_ns()
        self.updates_applied += 1
        self._check_crossed()

    def _trim(self) -> None:
        md = self.max_depth
        bids = self._bids
        asks = self._asks
        while len(bids) > md:
            bids.popitem(0)    # retire le bid le plus bas (le plus éloigné)
        while len(asks) > md:
            asks.popitem(-1)   # retire l'ask le plus haut

    def _check_crossed(self) -> None:
        if self._bids and self._asks:
            bb = self._bids.peekitem(-1)[0]
            ba = self._asks.peekitem(0)[0]
            if bb >= ba:
                self.reset()
                raise CrossedBookError(f"carnet croisé : bid {bb} >= ask {ba}")

    # ------------------------------------------------------------------ #
    # Lecture (hot path)
    # ------------------------------------------------------------------ #
    @property
    def is_ready(self) -> bool:
        return self.synced and bool(self._bids) and bool(self._asks)

    @property
    def best_bid(self) -> tuple[float, float] | None:
        return self._bids.peekitem(-1) if self._bids else None

    @property
    def best_ask(self) -> tuple[float, float] | None:
        return self._asks.peekitem(0) if self._asks else None

    @property
    def mid_price(self) -> float | None:
        if not (self._bids and self._asks):
            return None
        return (self._bids.peekitem(-1)[0] + self._asks.peekitem(0)[0]) * 0.5

    @property
    def spread(self) -> float | None:
        if not (self._bids and self._asks):
            return None
        return self._asks.peekitem(0)[0] - self._bids.peekitem(-1)[0]

    @property
    def spread_bps(self) -> float | None:
        if not (self._bids and self._asks):
            return None
        bb = self._bids.peekitem(-1)[0]
        ba = self._asks.peekitem(0)[0]
        return (ba - bb) / ((ba + bb) * 0.5) * 1e4

    @property
    def microprice(self) -> float | None:
        """Mid pondéré par les quantités opposées au top of book."""
        if not (self._bids and self._asks):
            return None
        bp, bq = self._bids.peekitem(-1)
        ap, aq = self._asks.peekitem(0)
        tot = bq + aq
        if tot <= 0.0:
            return (bp + ap) * 0.5
        return (bp * aq + ap * bq) / tot

    def imbalance(self, levels: int = 5, decay: float = 1.0) -> float:
        """Order Book Imbalance sur ``levels`` niveaux, dans [-1, 1].

        OBI = (Σ w_i·Qbid_i − Σ w_i·Qask_i) / (Σ w_i·Qbid_i + Σ w_i·Qask_i)
        avec w_i = decay**i (i = 0 au top). decay = 1.0 -> pondération uniforme.
        > 0 : pression acheteuse ; < 0 : pression vendeuse.
        """
        if levels < 1:
            raise ValueError("levels doit être >= 1")
        bv = self._bids.values()
        av = self._asks.values()  # vues indexables, sans copie
        nb = min(levels, len(bv))
        na = min(levels, len(av))
        if nb == 0 and na == 0:
            return 0.0
        b_sum = 0.0
        a_sum = 0.0
        w = 1.0
        # bv[-1] est le meilleur bid ; av[0] est le meilleur ask.
        for i in range(max(nb, na)):
            if i < nb:
                b_sum += w * bv[-1 - i]
            if i < na:
                a_sum += w * av[i]
            w *= decay
        tot = b_sum + a_sum
        return 0.0 if tot <= 0.0 else (b_sum - a_sum) / tot

    def top(self) -> BookTop | None:
        """Snapshot immuable du top of book, ou None si un côté est vide."""
        if not (self._bids and self._asks):
            return None
        bp, bq = self._bids.peekitem(-1)
        ap, aq = self._asks.peekitem(0)
        tot = bq + aq
        micro = (bp * aq + ap * bq) / tot if tot > 0.0 else (bp + ap) * 0.5
        return BookTop(
            bid=bp, bid_qty=bq, ask=ap, ask_qty=aq,
            mid=(bp + ap) * 0.5, spread=ap - bp, microprice=micro,
            update_id=self.last_update_id, ts_ns=self.last_update_ns,
        )

    def levels(self, n: int = 10) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """n meilleurs niveaux : (bids décroissants, asks croissants)."""
        bi = self._bids.items()
        ai = self._asks.items()
        nb = min(n, len(bi))
        na = min(n, len(ai))
        bids = [bi[-1 - i] for i in range(nb)]
        asks = [ai[i] for i in range(na)]
        return bids, asks

    def depth_arrays(self, n: int = 10) -> tuple[np.ndarray, np.ndarray]:
        """Profondeur en tableaux numpy (n, 2) [prix, qty], complétés par NaN."""
        bids, asks = self.levels(n)
        b = np.full((n, 2), np.nan, dtype=np.float64)
        a = np.full((n, 2), np.nan, dtype=np.float64)
        if bids:
            b[: len(bids)] = bids
        if asks:
            a[: len(asks)] = asks
        return b, a

    def volume_at_or_better(self, side: str, price: float) -> float:
        """Quantité cumulée disponible jusqu'à ``price`` (estimation d'impact)."""
        if side == "bid":
            return float(sum(self._bids[p] for p in self._bids.irange(minimum=price)))
        if side == "ask":
            return float(sum(self._asks[p] for p in self._asks.irange(maximum=price)))
        raise ValueError("side doit être 'bid' ou 'ask'")

    def staleness_ms(self, now_ns: int | None = None) -> float:
        """Âge du dernier update appliqué, en millisecondes (horloge monotone)."""
        if self.last_update_ns == 0:
            return float("inf")
        now = time.perf_counter_ns() if now_ns is None else now_ns
        return (now - self.last_update_ns) / 1e6

    def __len__(self) -> int:
        return len(self._bids) + len(self._asks)

    def __repr__(self) -> str:
        return (
            f"OrderBook({self.symbol!r}, synced={self.synced}, "
            f"bid={self.best_bid}, ask={self.best_ask}, id={self.last_update_id})"
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_side(side: SortedDict, levels: Levels) -> None:
    for lvl in levels:
        q = float(lvl[1])
        if q > 0.0:
            side[float(lvl[0])] = q


def _norm(levels: Iterable[LevelInput]) -> tuple[tuple[float, float], ...]:
    return tuple((float(p), float(q)) for p, q, *_ in levels)


def parse_binance_depth_update(raw: bytes | str, recv_ts_ns: int | None = None) -> DepthDelta:
    """Parse un message ``depthUpdate`` Binance (spot) en ``DepthDelta``.

    Format : {"e":"depthUpdate","E":ms,"s":"BTCUSDT","U":first,"u":final,
              "b":[["px","qty"],...],"a":[["px","qty"],...]}
    Pour les futures USDⓈ-M, utiliser plutôt ``pu`` pour la continuité.
    """
    msg = orjson.loads(raw)
    return DepthDelta(
        first_update_id=int(msg["U"]),
        final_update_id=int(msg["u"]),
        bids=_norm(msg.get("b", ())),
        asks=_norm(msg.get("a", ())),
        exchange_ts_ms=int(msg.get("E", 0)),
        recv_ts_ns=recv_ts_ns if recv_ts_ns is not None else time.perf_counter_ns(),
    )


def make_delta(
    first_update_id: int,
    final_update_id: int,
    bids: Iterable[LevelInput] = (),
    asks: Iterable[LevelInput] = (),
    exchange_ts_ms: int = 0,
) -> DepthDelta:
    """Construit un ``DepthDelta`` normalisé (pratique pour tests et backtest)."""
    return DepthDelta(
        first_update_id=first_update_id,
        final_update_id=final_update_id,
        bids=_norm(bids),
        asks=_norm(asks),
        exchange_ts_ms=exchange_ts_ms,
        recv_ts_ns=time.perf_counter_ns(),
    )


def depth_is_informative(book: OrderBook, min_levels: int = 2) -> bool:
    """True si le carnet a une vraie profondeur (DOM) et pas seulement un L1 synthétique."""
    return len(book._bids) >= min_levels and len(book._asks) >= min_levels
