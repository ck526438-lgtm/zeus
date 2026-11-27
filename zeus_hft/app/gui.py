"""Interface Windows de Zeus HFT (Tkinter).

Le moteur tourne dans un thread séparé avec sa propre boucle asyncio ; l'interface
lit un instantané de l'état toutes les 300 ms et n'interfère jamais avec le
chemin de décision.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from zeus_hft import __version__
from zeus_hft.config.settings import Mode, Settings
from zeus_hft.core.telemetry import setup_logging

BG, PANEL, FG, MUTED = "#0f1218", "#171c25", "#e8ecf3", "#8a94a6"
GOLD, GREEN, RED = "#f2b84b", "#3ccf91", "#ff5d6c"
CONFIG = Path("data/zeus_hft_gui.json")
STRATEGIES = {"Auto (selon le marché)": "auto", "Momentum / cassure": "momentum",
              "Mean-reversion": "meanrev", "OBI + IA (v2)": "obi"}
RETRY_S = 30


def _startup_file() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup/ZeusHFT.cmd"


def _launch_command() -> tuple[str, str]:
    """(dossier de travail, commande) pour relancer l'appli au démarrage de Windows."""
    if getattr(sys, "frozen", False):                      # .exe PyInstaller
        exe = Path(sys.executable)
        return str(exe.parent), f'start "" "{exe}"'
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return os.getcwd(), f'start "" "{pyw if pyw.exists() else py}" -m zeus_hft gui'


class _QueueHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:
            pass


class EngineThread(threading.Thread):
    def __init__(self, s: Settings, synthetic: bool, use_model: bool) -> None:
        super().__init__(daemon=True, name="zeus-engine")
        self.s, self.synthetic, self.use_model = s, synthetic, use_model
        self.loop: asyncio.AbstractEventLoop | None = None
        self.engine: Any = None
        self.error: str | None = None

    def run(self) -> None:
        from zeus_hft.app.factory import build_engine

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        gw = None
        try:
            self.engine, gw = self.loop.run_until_complete(
                build_engine(self.s, synthetic=self.synthetic, use_model=self.use_model))
            self.loop.run_until_complete(self.engine.run())
        except Exception as exc:
            self.error = str(exc)
            logging.getLogger("zeus_hft").exception("le moteur s'est arrêté")
        finally:
            if gw is not None:
                gw.shutdown()
            self.loop.close()

    def stop(self) -> None:
        if self.loop is not None and self.engine is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.engine.stop)

    def kill(self) -> None:
        if self.loop is not None and self.engine is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.engine.risk.kill, "arrêt d'urgence manuel")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"ZEUS HFT {__version__} — MetaTrader 5")
        self.geometry("1060x780")
        self.minsize(860, 600)
        self.configure(bg=BG)
        self.logq: "queue.Queue[str]" = queue.Queue(maxsize=5000)
        setup_logging("INFO", "data/zeus_hft.log", extra_handlers=[_QueueHandler(self.logq)])
        self.thread: EngineThread | None = None
        self._user_stopped = False
        self._retry_at = 0.0
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self._refresh)
        if self.autostart.get():
            logging.getLogger("zeus_hft").info("démarrage automatique dans 3 s…")
            self.after(3000, lambda: self._start(auto=True))

    # ------------------------------------------------------------------ #
    def _style(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=("Segoe UI", 10))
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Panel.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        st.configure("Value.TLabel", background=PANEL, foreground=FG, font=("Consolas", 15, "bold"))
        st.configure("Title.TLabel", background=BG, foreground=GOLD, font=("Segoe UI", 18, "bold"))
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.configure("Start.TButton", background=GREEN, foreground="#06140d", font=("Segoe UI", 10, "bold"))
        st.configure("Stop.TButton", background=PANEL, foreground=FG)
        st.configure("Kill.TButton", background=RED, foreground="#ffffff", font=("Segoe UI", 10, "bold"))
        st.map("TButton", background=[("disabled", "#2a303b")])

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=16, pady=(14, 6))
        ttk.Label(head, text="⚡ ZEUS HFT", style="Title.TLabel").pack(side="left")
        self.status = ttk.Label(head, text="● arrêté", foreground=MUTED)
        self.status.pack(side="right")

        saved = self._load_config()
        base = Settings()
        cfg = ttk.Frame(self)
        cfg.pack(fill="x", padx=16, pady=4)
        self.mode = tk.StringVar(value=saved.get("mode", "LIVE"))
        self.symbol = tk.StringVar(value=saved.get("symbol", "XAUUSD"))
        self.lot = tk.StringVar(value=saved.get("lot", "0.01"))
        self.maxdd = tk.StringVar(value=saved.get("maxdd", "300"))
        self.maxspread = tk.StringVar(value=saved.get("maxspread", f"{base.max_spread_points:g}"))
        self.strategy = tk.StringVar(value=saved.get("strategy", "Auto (selon le marché)"))
        self.news = tk.StringVar(value=saved.get("news", base.news_times_utc))
        self.synthetic = tk.BooleanVar(value=saved.get("synthetic", False))
        self.use_model = tk.BooleanVar(value=saved.get("use_model", True))
        self.confirm_live = tk.BooleanVar(value=False)          # jamais mémorisé, par sécurité
        self.autostart = tk.BooleanVar(value=saved.get("autostart", False))
        for label, w in (
            ("Mode", ttk.Combobox(cfg, textvariable=self.mode, values=["DRY_RUN", "LIVE"],
                                  width=9, state="readonly")),
            ("Stratégie", ttk.Combobox(cfg, textvariable=self.strategy, values=list(STRATEGIES),
                                       width=22, state="readonly")),
            ("Symbole", ttk.Entry(cfg, textvariable=self.symbol, width=10)),
            ("Lot", ttk.Entry(cfg, textvariable=self.lot, width=6)),
            ("Perte max/jour", ttk.Entry(cfg, textvariable=self.maxdd, width=7)),
            ("Spread max (pts)", ttk.Entry(cfg, textvariable=self.maxspread, width=5)),
        ):
            ttk.Label(cfg, text=label).pack(side="left", padx=(0, 4))
            w.pack(side="left", padx=(0, 10))

        cfg2 = ttk.Frame(self)
        cfg2.pack(fill="x", padx=16, pady=4)
        ttk.Label(cfg2, text="News (UTC, ex. 13:30, 2026-12-04 13:30)").pack(side="left", padx=(0, 4))
        ttk.Entry(cfg2, textvariable=self.news, width=34).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(cfg2, text="IA adaptative", variable=self.use_model).pack(side="left", padx=4)
        ttk.Checkbutton(cfg2, text="Simulation sans MT5", variable=self.synthetic).pack(side="left", padx=4)
        ttk.Checkbutton(cfg2, text="Autoriser compte réel", variable=self.confirm_live).pack(side="left", padx=4)

        cfg3 = ttk.Frame(self)
        cfg3.pack(fill="x", padx=16, pady=4)
        ttk.Checkbutton(cfg3, text="Démarrage automatique (à l'ouverture + relance si MT5 se coupe)",
                        variable=self.autostart, command=self._save_config).pack(side="left", padx=(0, 12))
        self.b_boot = ttk.Button(cfg3, text="", command=self._toggle_windows_startup)
        self.b_boot.pack(side="left")
        self._update_boot_button()

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=16, pady=8)
        self.b_start = ttk.Button(btns, text="▶  Démarrer", style="Start.TButton", command=self._start)
        self.b_stop = ttk.Button(btns, text="■  Arrêter (ferme tout)", style="Stop.TButton",
                                 command=self._stop, state="disabled")
        self.b_kill = ttk.Button(btns, text="⛔  ARRÊT D'URGENCE", style="Kill.TButton",
                                 command=self._kill, state="disabled")
        for b in (self.b_start, self.b_stop, self.b_kill):
            b.pack(side="left", padx=(0, 8), ipady=4, ipadx=6)

        grid = ttk.Frame(self)
        grid.pack(fill="x", padx=16, pady=6)
        self.vals: dict[str, ttk.Label] = {}
        cards = [("bid", "Bid"), ("ask", "Ask"), ("position", "Position (lots)"), ("pnl", "PnL net"),
                 ("equity", "Equity"), ("fills", "Exécutions"), ("regime", "Régime de marché"),
                 ("strat", "Stratégie en position"),
                 ("atr", "ATR M1 (pts) / vol×"), ("spread", "Spread (pts)"),
                 ("ia", "IA : précision / poids"), ("risk", "Risque"),
                 ("jtrades", "Trades journal / gagnants"), ("jnet", "PnL cumulé (journal)"),
                 ("jpf", "Profit factor (100 derniers)"), ("samples", "IA : exemples appris")]
        for i, (key, title) in enumerate(cards):
            f = ttk.Frame(grid, style="Panel.TFrame", padding=10)
            f.grid(row=i // 4, column=i % 4, sticky="nsew", padx=4, pady=4)
            ttk.Label(f, text=title, style="Panel.TLabel").pack(anchor="w")
            v = ttk.Label(f, text="—", style="Value.TLabel")
            v.pack(anchor="w")
            self.vals[key] = v
        for c in range(4):
            grid.columnconfigure(c, weight=1)

        bar = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        bar.pack(fill="x", padx=20, pady=(2, 4))
        self.account = ttk.Label(bar, text="Compte : —", style="Panel.TLabel")
        self.account.pack(side="left")
        self.verdict = ttk.Label(bar, text="", style="Panel.TLabel", font=("Segoe UI", 11, "bold"))
        self.verdict.pack(side="right")
        bar2 = ttk.Frame(self, style="Panel.TFrame", padding=(12, 4))
        bar2.pack(fill="x", padx=20, pady=(0, 4))
        self.reason = ttk.Label(bar2, text="", style="Panel.TLabel")
        self.reason.pack(side="left")
        self.by_strat = ttk.Label(bar2, text="", style="Panel.TLabel")
        self.by_strat.pack(side="right")

        self.log = tk.Text(self, height=12, bg="#0a0d12", fg="#b9c2d0", insertbackground=FG,
                           relief="flat", font=("Consolas", 9), wrap="none")
        self.log.pack(fill="both", expand=True, padx=16, pady=(6, 14))

    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_config() -> dict:
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_config(self) -> None:
        data = {"mode": self.mode.get(), "symbol": self.symbol.get(), "lot": self.lot.get(),
                "maxdd": self.maxdd.get(), "maxspread": self.maxspread.get(),
                "strategy": self.strategy.get(), "news": self.news.get(),
                "synthetic": self.synthetic.get(), "use_model": self.use_model.get(),
                "autostart": self.autostart.get()}
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _update_boot_button(self) -> None:
        on = _startup_file().exists()
        self.b_boot.config(text="✓ Lancé avec Windows (retirer)" if on else "Lancer avec Windows")

    def _toggle_windows_startup(self) -> None:
        f = _startup_file()
        try:
            if f.exists():
                f.unlink()
            else:
                cwd, cmd = _launch_command()
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(f'@echo off\r\ncd /d "{cwd}"\r\n{cmd}\r\n', encoding="utf-8")
                if not self.autostart.get():
                    self.autostart.set(True)
                    self._save_config()
        except OSError as exc:
            messagebox.showerror("Lancer avec Windows", str(exc))
        self._update_boot_button()

    def _settings(self) -> Settings:
        mode = Mode(self.mode.get())
        lot = float(self.lot.get().replace(",", "."))
        base = Settings()
        return base.model_copy(update={
            "mode": mode, "symbol": self.symbol.get().strip(), "order_qty": lot,
            "max_net_inventory": max(base.max_net_inventory, lot),
            "max_daily_drawdown": float(self.maxdd.get().replace(",", ".")),
            "max_spread_points": float(self.maxspread.get().replace(",", ".")),
            "strategy": STRATEGIES.get(self.strategy.get(), "auto"),
            "news_times_utc": self.news.get().strip(),
            "confirm_live": self.confirm_live.get(),
        })

    def _start(self, auto: bool = False) -> None:
        if self.thread is not None:
            return
        try:
            s = self._settings()
            from zeus_hft.strategies.features import NewsCalendar
            NewsCalendar(s.news_times_utc, 0, 0)          # valide le format des heures de news
        except ValueError as exc:
            messagebox.showerror("Réglage invalide", str(exc))
            return
        self._save_config()
        self._user_stopped = False
        if s.mode is Mode.LIVE:
            if self.synthetic.get():
                messagebox.showerror("Mode LIVE", "Le mode LIVE exige MetaTrader 5 (décoche « Démo sans MT5 »).")
                return
            msg = ("Zeus HFT va placer de vrais ordres dans MetaTrader 5.\n\n"
                   "• Compte DÉMO : aucun argent réel en jeu, c'est l'endroit pour le laisser "
                   "apprendre.\n• Compte RÉEL : refusé tant que « Autoriser compte réel » "
                   "n'est pas coché.\n\nContinuer ?")
            if self.confirm_live.get():
                msg = ("⚠️ « Autoriser compte réel » est coché : si MT5 est connecté à un compte "
                       "RÉEL, de l'argent réel sera engagé.\n\n" + msg)
            # en démarrage automatique, pas de fenêtre bloquante : le compte réel reste
            # refusé tant que la case n'est pas cochée (et elle n'est jamais mémorisée)
            if not auto and not messagebox.askyesno("Mode LIVE", msg, icon="warning"):
                return
        self.thread = EngineThread(s, self.synthetic.get(), self.use_model.get())
        self.thread.start()
        self.b_start.state(["disabled"])
        self.b_stop.state(["!disabled"])
        self.b_kill.state(["!disabled"])

    def _stop(self) -> None:
        self._user_stopped = True
        if self.thread:
            self.thread.stop()

    def _kill(self) -> None:
        if self.thread:
            self.thread.kill()

    def _on_close(self) -> None:
        if self.thread and self.thread.is_alive():
            if not messagebox.askyesno("Quitter", "Le moteur tourne. Tout fermer et quitter ?"):
                return
            self.thread.stop()
            self.thread.join(5.0)
        self.destroy()

    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        while True:
            try:
                line = self.logq.get_nowait()
            except queue.Empty:
                break
            self.log.insert("end", line + "\n")
            if int(self.log.index("end-1c").split(".")[0]) > 2000:
                self.log.delete("1.0", "500.0")
            self.log.see("end")

        t = self.thread
        if t is not None and t.engine is not None:
            snap = t.engine.snapshot()
            st, rk = snap["strategy"], snap["risk"]
            fmt = lambda x, n=2: "—" if x is None else f"{x:,.{n}f}"  # noqa: E731
            self.vals["bid"].config(text=fmt(snap["bid"]))
            self.vals["ask"].config(text=fmt(snap["ask"]))
            self.vals["position"].config(text=f"{snap['position']:+.2f}")
            pnl = snap["pnl"]
            self.vals["pnl"].config(text=f"{pnl:+,.2f}", foreground=GREEN if pnl >= 0 else RED)
            self.vals["equity"].config(text=fmt(rk["equity"]))
            self.vals["fills"].config(text=f"{snap['fills']} / {snap['orders']}")
            self.vals["lat"].config(text=f"{fmt(snap['decision_p50_us'], 0)} / {fmt(snap['decision_p99_us'], 0)} µs")
            self.vals["ia"].config(text=f"{st.get('model_acc', 0):.1%} / {st.get('model_conf', 0):.2f}")
            reg = st.get("regime", "—")
            self.vals["regime"].config(text=reg, foreground=(GREEN if reg in ("TENDANCE", "NORMAL", "CALME")
                                                             else RED if reg in ("CHOC", "NEWS") else GOLD))
            self.vals["strat"].config(text=st.get("strategy", "—"))
            self.vals["atr"].config(text=f"{st.get('atr_pts', 0):.0f} / {st.get('vol_ratio', 0):.2f}")
            self.vals["spread"].config(text=fmt(st.get("spread_pts"), 1))
            halts = rk.get("halts") or []
            self.reason.config(text=("⏸ " + "; ".join(halts)) if halts else f"Décision : {st.get('reason', '')}")
            self.by_strat.config(text=snap.get("by_strategy", ""))
            j = snap.get("journal")
            if j is not None:
                self.vals["jtrades"].config(text=f"{j.trades} / {j.win_rate:.0%}")
                self.vals["jnet"].config(text=f"{j.net_pnl:+,.2f}",
                                         foreground=GREEN if j.net_pnl >= 0 else RED)
                pf = "—" if j.recent_trades == 0 else ("∞" if j.recent_pf == float("inf")
                                                       else f"{j.recent_pf:.2f}")
                self.vals["jpf"].config(text=pf)
                ok = j.verdict.startswith("RENTABLE")
                bad = j.verdict.startswith("perdant")
                self.verdict.config(text=j.verdict, foreground=GREEN if ok else RED if bad else GOLD)
            self.vals["samples"].config(text=f"{int(st.get('model_samples', 0)):,}")
            self.account.config(text=f"Compte : {snap.get('account') or '—'}")
            killed = rk["killed"]
            self.vals["risk"].config(text="KILL" if killed else ("PAUSE" if halts else "OK"),
                                     foreground=RED if killed else GOLD if halts else GREEN)
            self.status.config(text="● en marche" if snap["running"] else "● arrêt…",
                               foreground=GREEN if snap["running"] else MUTED)
        if t is not None and not t.is_alive():
            if t.error:
                if self.autostart.get() and not self._user_stopped:
                    # mode automatique : pas de fenêtre bloquante, on retente plus tard
                    logging.getLogger("zeus_hft").warning(
                        "le moteur s'est arrêté (%s) : nouvel essai dans %d s", t.error, RETRY_S)
                    self._retry_at = time.monotonic() + RETRY_S
                else:
                    messagebox.showerror("Zeus HFT", t.error)
            self.thread = None
            self.status.config(text="● arrêté", foreground=MUTED)
            self.b_start.state(["!disabled"])
            self.b_stop.state(["disabled"])
            self.b_kill.state(["disabled"])
        if (self.thread is None and self._retry_at and time.monotonic() >= self._retry_at
                and not self._user_stopped):
            self._retry_at = 0.0
            self._start(auto=True)
        self.after(300, self._refresh)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
