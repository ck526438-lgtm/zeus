"""Connexion à MetaTrader 5 (Windows) : données, ordres, positions."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

log = logging.getLogger("zeus.mt5")

try:  # le paquet MetaTrader5 n'existe que sous Windows
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None

TIMEFRAMES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


class MT5Client:
    def __init__(self, cfg: dict):
        if mt5 is None:
            raise RuntimeError("Le paquet MetaTrader5 est introuvable. Installe-le sous Windows : "
                               "pip install MetaTrader5")
        self.cfg = cfg["mt5"]
        self.deviation = cfg["trading"]["deviation_points"]
        self.magic = int(self.cfg["magic"])

    # ---------- connexion ----------
    def connect(self):
        kw = {}
        if self.cfg.get("path"):
            kw["path"] = self.cfg["path"]
        if self.cfg.get("login"):
            kw.update(login=int(self.cfg["login"]), password=self.cfg["password"],
                      server=self.cfg["server"])
        if not mt5.initialize(**kw):
            raise RuntimeError(f"Connexion MT5 impossible : {mt5.last_error()}")
        info = mt5.account_info()
        log.info("Connecté : compte %s (%s) solde %.2f %s", info.login, info.server,
                 info.balance, info.currency)
        return info

    def shutdown(self):
        mt5.shutdown()

    def is_demo(self) -> bool:
        return mt5.account_info().trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO

    def account(self):
        return mt5.account_info()

    # ---------- marché ----------
    def tf(self, name: str):
        return getattr(mt5, f"TIMEFRAME_{name}")

    def ensure_symbol(self, symbol: str):
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Symbole indisponible : {symbol}")
        return mt5.symbol_info(symbol)

    def rates(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        r = mt5.copy_rates_from_pos(symbol, self.tf(timeframe), 0, count)
        if r is None or len(r) == 0:
            raise RuntimeError(f"Pas de données pour {symbol}: {mt5.last_error()}")
        df = pd.DataFrame(r)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df[["time", "open", "high", "low", "close", "tick_volume"]]

    def rates_range(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        r = mt5.copy_rates_range(symbol, self.tf(timeframe), start, end)
        df = pd.DataFrame(r)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df[["time", "open", "high", "low", "close", "tick_volume"]]

    def tick(self, symbol: str):
        return mt5.symbol_info_tick(symbol)

    def spread_points(self, symbol: str) -> float:
        info, t = mt5.symbol_info(symbol), mt5.symbol_info_tick(symbol)
        return (t.ask - t.bid) / info.point

    # ---------- positions ----------
    def positions(self, symbol: str | None = None):
        ps = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return [p for p in (ps or []) if p.magic == self.magic]

    def _filling(self, info):
        modes = info.filling_mode
        if modes & 1:
            return mt5.ORDER_FILLING_FOK
        if modes & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def open(self, symbol: str, direction: int, volume: float, sl: float, tp: float, comment="ZEUS"):
        info = mt5.symbol_info(symbol)
        t = mt5.symbol_info_tick(symbol)
        price = t.ask if direction == 1 else t.bid
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL,
            "price": price, "sl": round(sl, info.digits), "tp": round(tp, info.digits),
            "deviation": self.deviation, "magic": self.magic, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(info),
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("Ordre refusé %s : %s", symbol, getattr(res, "comment", mt5.last_error()))
            return None
        return res

    def close(self, position, comment="ZEUS exit"):
        info = mt5.symbol_info(position.symbol)
        t = mt5.symbol_info_tick(position.symbol)
        is_buy = position.type == mt5.POSITION_TYPE_BUY
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": position.symbol,
            "volume": position.volume, "position": position.ticket,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": t.bid if is_buy else t.ask, "deviation": self.deviation,
            "magic": self.magic, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(info),
        }
        res = mt5.order_send(req)
        if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("Fermeture refusée %s : %s", position.ticket, getattr(res, "comment", mt5.last_error()))
            return None
        return res

    def modify_sl(self, position, sl: float):
        info = mt5.symbol_info(position.symbol)
        req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": position.symbol,
               "position": position.ticket, "sl": round(sl, info.digits), "tp": position.tp}
        res = mt5.order_send(req)
        return res is not None and res.retcode == mt5.TRADE_RETCODE_DONE

    def direction_of(self, position) -> int:
        return 1 if position.type == mt5.POSITION_TYPE_BUY else -1

    def closed_profit(self, ticket: int) -> float:
        """Profit net d'une position fermée (avec swap et commission)."""
        deals = mt5.history_deals_get(position=ticket) or []
        return float(sum(d.profit + d.swap + d.commission for d in deals))

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
