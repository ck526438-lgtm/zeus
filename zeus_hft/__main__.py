"""Ligne de commande Zeus HFT.

    python -m zeus_hft run --synthetic                 # démo paper trading sans MT5
    python -m zeus_hft run                             # DRY_RUN sur le flux MT5 réel
    python -m zeus_hft run --mode LIVE                 # ordres réels (compte démo conseillé)
    python -m zeus_hft backtest --synthetic 200000
    python -m zeus_hft backtest --csv data/XAUUSD_ticks.csv
    python -m zeus_hft download --symbol XAUUSD --days 60
    python -m zeus_hft walkforward --csv data/XAUUSD_ticks.csv --train-days 20 --test-days 5
    python -m zeus_hft montecarlo --trades data/zeus_hft_trades.csv
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
    for k in ("symbol", "order_qty", "max_daily_drawdown", "max_net_inventory", "strategy",
              "news_times_utc", "max_spread_points", "metrics_port"):
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
    if a.utc_offset is not None:
        s = s.model_copy(update={"server_utc_offset_hours": a.utc_offset})
    seed: dict[str, list[tuple[int, float, float, float, float]]] = {}
    if a.csv:
        from zeus_hft.backtest.data import bars_path, load_bars_csv

        updates = load_ticks_csv(a.csv, s.symbol)
        seed = {tf: load_bars_csv(bars_path(a.csv, s.symbol, tf)) for tf in ("M15", "H1")}
        if s.trend_filter and not all(seed.values()):
            print("⚠ historique M15/H1 absent (relance « download ») : le filtre de tendance "
                  "attendra ~2 jours de ticks avant d'autoriser des trades")
    else:
        updates = synthetic_updates(a.synthetic or 200_000, symbol=s.symbol, seed=a.seed, point=s.point)
        if s.trend_filter:
            s = s.model_copy(update={"trend_filter": False})
            print("ℹ données synthétiques : filtre de tendance H1/M15 désactivé (pas d'historique)")
    rep = run_async(run_backtest(s, updates, use_model=not a.no_model, touch_fill=a.touch_fill,
                                 export_equity=a.export, seed_bars=seed))
    shutdown_logging()
    if a.json:
        print(json.dumps(report_dict(rep), default=str, indent=2))
    else:
        print(rep.text())
    if a.montecarlo and rep.trade_pnls:
        from zeus_hft.backtest.montecarlo import monte_carlo

        print()
        print(monte_carlo(rep.trade_pnls, sims=a.montecarlo, equity=s.starting_equity,
                          extra_cost_mean=a.mc_cost).text())
    return 0


def _settings_from(a: argparse.Namespace) -> Settings:
    s = _settings(a, Mode.BACKTEST)
    if getattr(a, "utc_offset", None) is not None:
        s = s.model_copy(update={"server_utc_offset_hours": a.utc_offset})
    return s


def cmd_walkforward(a: argparse.Namespace) -> int:
    from zeus_hft.app.factory import run_async
    from zeus_hft.backtest.data import bars_path, load_bars_csv, load_ticks_csv
    from zeus_hft.backtest.walkforward import run_walkforward

    s = _settings_from(a)
    setup_logging("WARNING", None)
    print(f"chargement de {a.csv}…")
    ups = list(load_ticks_csv(a.csv, s.symbol))
    seed = {tf: load_bars_csv(bars_path(a.csv, s.symbol, tf)) for tf in ("M15", "H1")}
    grid = json.loads(a.grid) if a.grid else None
    rep = run_async(run_walkforward(s, ups, train_days=a.train_days, test_days=a.test_days,
                                    grid=grid, min_trades=a.min_trades, seed_bars=seed,
                                    progress=print))
    shutdown_logging()
    print(rep.text())
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    from zeus_hft.execution.journal import verify_journal

    ok, n, bad = verify_journal(a.trades)
    if ok:
        print(f"✓ journal intègre : {n} trades, chaîne de hachage valide")
        return 0
    print(f"✗ journal ALTÉRÉ ou ancien format : 1re ligne invalide n°{bad} (sur {n} lues)")
    return 3


def cmd_montecarlo(a: argparse.Namespace) -> int:
    from zeus_hft.backtest.montecarlo import load_journal_pnls, monte_carlo

    pnls = load_journal_pnls(a.trades, a.mode, a.strategy)
    if not pnls:
        print("aucun trade correspondant dans le journal")
        return 1
    print(monte_carlo(pnls, sims=a.sims, skip_prob=a.skip, extra_cost_mean=a.cost,
                      equity=a.equity, ruin_pct=a.ruin_pct).text())
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
        sp.add_argument("--strategy", choices=["auto", "momentum", "meanrev", "obi"],
                        help="auto (défaut) = choix selon le régime de marché")
        sp.add_argument("--news", dest="news_times_utc",
                        help="heures de news UTC : '13:30' (chaque jour) ou '2026-12-04 13:30'")
        sp.add_argument("--max-spread", dest="max_spread_points", type=float)
        sp.add_argument("--metrics-port", dest="metrics_port", type=int,
                        help="expose /metrics pour Prometheus sur ce port")

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
    b.add_argument("--utc-offset", type=float,
                   help="décalage heure serveur MT5 - UTC en heures (souvent 2 ou 3)")
    b.add_argument("--montecarlo", type=int, default=0, metavar="N",
                   help="ajoute N simulations de Monte Carlo sur les trades du backtest")
    b.add_argument("--mc-cost", type=float, default=0.0,
                   help="coût défavorable moyen ajouté par trade (devise du compte)")

    w = sub.add_parser("walkforward", help="optimisation glissante + validation hors échantillon")
    common(w)
    w.add_argument("--csv", required=True)
    w.add_argument("--train-days", type=float, default=20.0)
    w.add_argument("--test-days", type=float, default=5.0)
    w.add_argument("--min-trades", type=int, default=10)
    w.add_argument("--grid", help='JSON, ex. \'{"velocity_entry_atr": [0.35, 0.5]}\'')
    w.add_argument("--utc-offset", type=float)
    w.set_defaults(fn=cmd_walkforward)

    m = sub.add_parser("montecarlo", help="robustesse : Monte Carlo sur le journal des trades")
    m.add_argument("--trades", default="data/zeus_hft_trades.csv")
    m.add_argument("--mode", default="LIVE", help="LIVE, DRY_RUN ou '' pour tout")
    m.add_argument("--strategy", help="momentum, meanrev, obi…")
    m.add_argument("--sims", type=int, default=5000)
    m.add_argument("--skip", type=float, default=0.05, help="part de trades manqués")
    m.add_argument("--cost", type=float, default=0.0, help="coût défavorable moyen par trade")
    m.add_argument("--equity", type=float, default=10_000.0)
    m.add_argument("--ruin-pct", type=float, default=20.0)
    m.set_defaults(fn=cmd_montecarlo)

    v = sub.add_parser("verify-journal", help="vérifie que le journal des trades n'a pas été modifié")
    v.add_argument("--trades", default="data/zeus_hft_trades.csv")
    v.set_defaults(fn=cmd_verify)
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
