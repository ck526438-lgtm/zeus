"""Démarrage automatique MT5 : terminal, symbole du broker, autorisation Algo Trading."""
from __future__ import annotations

from types import SimpleNamespace as NS

from zeus_hft.config.settings import Mode, Settings
from zeus_hft.feeds.mt5_gateway import MT5Gateway
from zeus_hft.feeds.mt5_launch import find_terminal, resolve_symbol, trading_permission

from .test_mt5_gateway import FakeMT5


def test_find_terminal_prefers_official_then_most_recent() -> None:
    files = {r"C:\PF\Exness MT5\terminal64.exe": 300.0,
             r"C:\PF\MetaTrader 5\terminal64.exe": 100.0,
             r"C:\PF\IC Markets MT5\terminal64.exe": 200.0}

    def glob_(pattern: str) -> list[str]:
        return list(files) if pattern.endswith("terminal64.exe") and "MetaQuotes" not in pattern else []

    assert find_terminal([r"C:\PF"], glob_, files.__getitem__) == r"C:\PF\MetaTrader 5\terminal64.exe"
    del files[r"C:\PF\MetaTrader 5\terminal64.exe"]
    assert find_terminal([r"C:\PF"], glob_, files.__getitem__) == r"C:\PF\Exness MT5\terminal64.exe"
    assert find_terminal([r"C:\PF"], lambda p: []) == ""


def test_resolve_symbol_broker_names() -> None:
    assert resolve_symbol("XAUUSD", ["EURUSD", "XAUUSD"]) == "XAUUSD"
    assert resolve_symbol("XAUUSD", ["EURUSDm", "XAUUSDm", "XAUUSDm.micro"]) == "XAUUSDm"
    assert resolve_symbol("XAUUSD", ["GOLD", "SILVER"]) == "GOLD"
    assert resolve_symbol("xauusd", ["XAUUSD.r"]) == "XAUUSD.r"
    assert resolve_symbol("BTCUSD", ["EURUSD"]) is None


def test_trading_permission_messages() -> None:
    ok = NS(trade_allowed=True, trade_expert=True)
    assert trading_permission(NS(trade_allowed=True), ok) == (True, "")
    assert "Algo Trading" in trading_permission(NS(trade_allowed=False), ok)[1]
    assert "investisseur" in trading_permission(NS(trade_allowed=True), NS(trade_allowed=False, trade_expert=True))[1]
    assert "broker" in trading_permission(NS(trade_allowed=True), NS(trade_allowed=True, trade_expert=False))[1]


def test_gateway_resolves_symbol_and_tracks_algo_button() -> None:
    fake = FakeMT5()
    fake.symbol_select = lambda s, b: s == "XAUUSDm"  # type: ignore[method-assign]
    fake.symbols_get = lambda: (NS(name="EURUSDm"), NS(name="XAUUSDm"))  # type: ignore[attr-defined]
    term = NS(connected=True, ping_last=30_000, trade_allowed=False)
    fake.terminal_info = lambda: term  # type: ignore[method-assign]
    gw = MT5Gateway(Settings(_env_file=None, mode=Mode.LIVE), mt5=fake)  # type: ignore[call-arg]
    gw.connect_blocking()
    assert gw.symbol == "XAUUSDm" and gw.symbol_specs()["symbol"] == "XAUUSDm"
    assert not gw.algo_ok and "Algo Trading" in gw.algo_reason
    term.trade_allowed = True                       # l'utilisateur clique sur « Algo Trading »
    assert gw.heartbeat_blocking() == 30.0 and gw.algo_ok and gw.algo_reason == ""
