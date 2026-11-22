"""ZEUS ⚡ — point d'entrée.

  python main.py live                  # trading auto (compte démo par défaut)
  python main.py live --confirm-live   # compte réel (+ mode: live dans config.yaml)
  python main.py download --symbol XAUUSD --days 60
  python main.py train --csv data/XAUUSD_M1.csv
  python main.py backtest --csv data/XAUUSD_M1.csv
  python main.py backtest --synthetic  # test sans MT5
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zeus.config import load_config

BANNER = r"""
 ███████╗███████╗██╗   ██╗███████╗
 ╚══███╔╝██╔════╝██║   ██║██╔════╝
   ███╔╝ █████╗  ██║   ██║███████╗
  ███╔╝  ██╔══╝  ██║   ██║╚════██║
 ███████╗███████╗╚██████╔╝███████║   ⚡ IA de trading MT5
 ╚══════╝╚══════╝ ╚═════╝ ╚══════╝
"""


def setup_logging(level: str):
    Path("data").mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler("data/zeus.log", encoding="utf-8")],
    )


def get_data(args, cfg):
    from zeus.data import load_csv, synthetic
    if getattr(args, "synthetic", False):
        return synthetic()
    if args.csv:
        return load_csv(args.csv)
    from zeus.mt5_client import MT5Client
    c = MT5Client(cfg)
    c.connect()
    try:
        c.ensure_symbol(args.symbol)
        end = datetime.now(timezone.utc)
        return c.rates_range(args.symbol, cfg["trading"]["timeframe"], end - timedelta(days=args.days), end)
    finally:
        c.shutdown()


def cmd_live(args, cfg):
    from zeus.engine import ZeusEngine
    ZeusEngine(cfg, allow_live=args.confirm_live).start()


def cmd_download(args, cfg):
    df = get_data(args, cfg)
    out = Path(args.out or f"data/{args.symbol}_{cfg['trading']['timeframe']}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"✅ {len(df)} bougies enregistrées dans {out}")


def cmd_train(args, cfg):
    from zeus.model import ZeusModel
    df = get_data(args, cfg)
    print(f"Entraînement sur {len(df)} bougies…")
    m = ZeusModel()
    report = m.fit(df, cfg["strategy"], cfg["risk"], horizon=cfg["strategy"]["max_hold_bars"],
                   holdout=args.holdout)
    print(f"Données d'entraînement jusqu'au {m.train_end} — le reste ({args.holdout:.0%}) est réservé au backtest.")
    for side, folds in report.items():
        print(f"\n— {side.upper()} (validation walk-forward)")
        for k, f in enumerate(folds, 1):
            print(f"  période {k}: AUC {f['auc']}  précision {f['precision@0.58']}  "
                  f"(base {f['base_winrate']})  signaux {f['trades']}")
    m.save(cfg["strategy"]["model_path"])
    print(f"\n✅ Modèle sauvegardé : {cfg['strategy']['model_path']}")
    print("Règle d'or : si la précision n'est pas nettement au-dessus de la base, le modèle n'apporte rien.")


def cmd_backtest(args, cfg):
    from zeus.backtest import run_backtest
    from zeus.model import ZeusModel
    df = get_data(args, cfg)
    model = None
    if cfg["strategy"]["use_model"] and not args.no_model:
        model = ZeusModel.load(cfg["strategy"]["model_path"])
    if model is not None and model.train_end is not None and not args.in_sample:
        import pandas as pd
        df = df[pd.to_datetime(df["time"], utc=True) > model.train_end].reset_index(drop=True)
        print(f"Backtest HORS échantillon : uniquement après {model.train_end} (jamais vu par l'IA).")
        if len(df) < 500:
            sys.exit("Pas assez de données après la période d'entraînement. Télécharge plus d'historique.")
    res = run_backtest(df, cfg, model)
    print(f"\n📊 Backtest sur {len(df)} bougies  (IA : {'oui' if model else 'non'})")
    for k in ("trades", "winrate_%", "profit_factor", "net_profit", "final_balance",
              "return_%", "max_drawdown_%", "avg_trade"):
        print(f"  {k:<16} {res[k]}")
    if args.export:
        import pandas as pd
        pd.DataFrame([t.__dict__ for t in res["trade_list"]]).to_csv(args.export, index=False)
        print(f"  trades exportés → {args.export}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Zeus — IA de trading pour MetaTrader 5")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("live", help="lancer le trading automatique")
    p.add_argument("--confirm-live", action="store_true", help="autoriser un compte réel")

    for name in ("download", "train", "backtest"):
        p = sub.add_parser(name)
        p.add_argument("--symbol", default="XAUUSD")
        p.add_argument("--days", type=int, default=60)
        p.add_argument("--csv", help="utiliser un CSV au lieu de MT5")
        p.add_argument("--synthetic", action="store_true", help="données artificielles (test)")
        if name == "download":
            p.add_argument("--out")
        if name == "train":
            p.add_argument("--holdout", type=float, default=0.3,
                           help="part finale gardée pour le backtest (défaut 0.3)")
        if name == "backtest":
            p.add_argument("--no-model", action="store_true")
            p.add_argument("--in-sample", action="store_true",
                           help="inclure la période d'entraînement (résultats trompeurs)")
            p.add_argument("--export", help="CSV des trades")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg["logging"]["level"])
    print(BANNER)
    {"live": cmd_live, "download": cmd_download, "train": cmd_train,
     "backtest": cmd_backtest}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
