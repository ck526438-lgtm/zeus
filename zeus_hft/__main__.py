"""Ligne de commande Zeus HFT.

    python -m zeus_hft run --synthetic                 # démo paper trading sans MT5
    python -m zeus_hft run                             # DRY_RUN sur le flux MT5 réel
    python -m zeus_hft run --mode LIVE                 # ordres réels (compte démo conseillé)
    python -m zeus_hft backtest --synthetic 200000
    python -m zeus_hft backtest --csv data/XAUUSD_ticks.csv
    python -m zeus_hft download --symbol XAUUSD --days 5
    python -m zeus_hft gui
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from typing import Any

from zeus_hft import __version__
from zeus_hft.config.settings import Mode, Settings
from zeus_hft.core.telemetry import setup_logging, shutdown_logging

log = logging.getLogger("zeus_hft")


def _settings(a: argparse.Namespace, mode: Mode | None = None) -> Settings:
    over: dict[str, Any] = {}
    if mode is not None:
        over["mode"] = mode
    for k in ("symbol", "order_qty", "max_daily_drawdown", "max_net_inventory"):
        v = getattr(a, k, None)
        if v is not None:
            over[k] = v
    if getattr(a, "confirm_live", False):
        over["confirm_live"] = True
    base = Settings()
    return base.model_copy(update=over) if over else base


async def _run(s: Settings, synthetic: bool, use_model: bool) -> None:
    from zeus_hft.app.factory import build_engine

    engine, gw = await build_engine(s, synthetic=synthetic, use_model=use_model)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.stop)
        except (NotImplementedError, RuntimeError):   # Windows : Ctrl+C -> KeyboardInterrupt
            pass
    try:
        await engine.run()
    finally:
        if gw is not None:
            gw.shutdown()


def cmd_run(a: argparse.Namespace) -> int:
    from zeus_hft.app.factory import LiveAccountRefused, run_async

    s = _settings(a, Mode(a.mode))
    setup_logging(s.log_level, s.log_file)
    if s.mode is Mode.LIVE:
        log.warning("MODE LIVE : des ordres RÉELS vont être envoyés à MT5")
    try:
        run_async(_run(s, a.synthetic, not a.no_model))
    except KeyboardInterrupt:
        log.info("interruption clavier")
    except LiveAccountRefused as exc:
        log.error("%s", exc)
        return 2
    finally:
        shutdown_logging()
    return 0


def cmd_backtest(a: argparse.Namespace) -> int:
    from zeus_hft.app.factory import run_async
    from zeus_hft.backtest.data import load_ticks_csv
    from zeus_hft.backtest.simulator import report_dict, run_backtest
    from zeus_hft.feeds.synthetic import synthetic_updates

    s = _settings(a, Mode.BACKTEST)
    if a.point:
        s = s.model_copy(update={"point": a.point})
    if a.contract_size:
        s = s.model_copy(update={"contract_size": a.contract_size})
    setup_logging("WARNING", None)
    if a.csv:
        updates = load_ticks_csv(a.csv, s.symbol)
    else:
        updates = synthetic_updates(a.synthetic or 200_000, symbol=s.symbol, seed=a.seed, point=s.point)
    rep = run_async(run_backtest(s, updates, use_model=not a.no_model, touch_fill=a.touch_fill,
                                 export_equity=a.export))
    shutdown_logging()
    if a.json:
        print(json.dumps(report_dict(rep), default=str, indent=2))
    else:
        print(rep.text())
    return 0


def cmd_download(a: argparse.Namespace) -> int:
    from zeus_hft.backtest.data import download_ticks_mt5

    out = a.out or f"data/{a.symbol}_ticks.csv"
    n = download_ticks_mt5(a.symbol, a.days, out)
    print(f"{n:,} ticks enregistrés dans {out}")
    return 0


def cmd_gui(_: argparse.Namespace) -> int:
    from zeus_hft.app.gui import main as gui_main

    gui_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zeus-hft", description="Zeus HFT — scalping haute fréquence MT5")
    p.add_argument("--version", action="version", version=f"Zeus HFT {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--symbol")
        sp.add_argument("--lot", dest="order_qty", type=float)
        sp.add_argument("--max-dd", dest="max_daily_drawdown", type=float)
        sp.add_argument("--max-inventory", dest="max_net_inventory", type=float)
        sp.add_argument("--no-model", action="store_true", help="désactive l'IA en ligne")

    r = sub.add_parser("run", help="temps réel (DRY_RUN ou LIVE)")
    common(r)
    r.add_argument("--mode", choices=["DRY_RUN", "LIVE"], default="DRY_RUN")
    r.add_argument("--synthetic", action="store_true", help="flux synthétique (sans MT5)")
    r.add_argument("--confirm-live", action="store_true", help="autorise un compte RÉEL")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("backtest", help="simulation tick par tick")
    common(b)
    b.add_argument("--csv", help="ticks MT5 (time_msc,bid,ask,...)")
    b.add_argument("--synthetic", type=int, metavar="N", help="N ticks synthétiques")
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--point", type=float)
    b.add_argument("--contract-size", type=float)
    b.add_argument("--touch-fill", type=float, default=0.0,
                   help="fraction exécutée quand le prix touche l'ordre (0 = prudent)")
    b.add_argument("--export", help="CSV de la courbe d'equity")
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=cmd_backtest)

    d = sub.add_parser("download", help="télécharge les ticks depuis MT5")
    d.add_argument("--symbol", default="XAUUSD")
    d.add_argument("--days", type=int, default=5)
    d.add_argument("--out")
    d.set_defaults(fn=cmd_download)

    g = sub.add_parser("gui", help="interface graphique")
    g.set_defaults(fn=cmd_gui)
    return p


def _utf8_console() -> None:
    """La console Windows est en cp1252 par défaut : on force l'UTF-8 (µ, ═, accents)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
