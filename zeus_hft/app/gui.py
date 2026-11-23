"""Interface Windows de Zeus HFT (Tkinter).

Le moteur tourne dans un thread séparé avec sa propre boucle asyncio ; l'interface
lit un instantané de l'état toutes les 300 ms et n'interfère jamais avec le
chemin de décision.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from zeus_hft import __version__
from zeus_hft.config.settings import Mode, Settings
from zeus_hft.core.telemetry import setup_logging

BG, PANEL, FG, MUTED = "#0f1218", "#171c25", "#e8ecf3", "#8a94a6"
GOLD, GREEN, RED = "#f2b84b", "#3ccf91", "#ff5d6c"


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
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self._refresh)

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

        cfg = ttk.Frame(self)
        cfg.pack(fill="x", padx=16, pady=4)
        self.mode = tk.StringVar(value="LIVE")
        self.symbol = tk.StringVar(value="XAUUSD")
        self.lot = tk.StringVar(value="0.01")
        self.maxdd = tk.StringVar(value="300")
        base = Settings()
        self.maxspread = tk.StringVar(value=f"{base.max_spread_points:g}")
        self.tp = tk.StringVar(value=f"{base.take_profit_points:g}")
        self.sl = tk.StringVar(value=f"{base.stop_loss_points:g}")
        self.synthetic = tk.BooleanVar(value=False)
        self.use_model = tk.BooleanVar(value=True)
        self.confirm_live = tk.BooleanVar(value=False)
        for label, w in (
            ("Mode", ttk.Combobox(cfg, textvariable=self.mode, values=["DRY_RUN", "LIVE"],
                                  width=9, state="readonly")),
            ("Symbole", ttk.Entry(cfg, textvariable=self.symbol, width=10)),
            ("Lot", ttk.Entry(cfg, textvariable=self.lot, width=6)),
            ("Perte max/jour", ttk.Entry(cfg, textvariable=self.maxdd, width=7)),
        ):
            ttk.Label(cfg, text=label).pack(side="left", padx=(0, 4))
            w.pack(side="left", padx=(0, 12))

        cfg2 = ttk.Frame(self)
        cfg2.pack(fill="x", padx=16, pady=4)
        for label, var in (("Spread max (pts)", self.maxspread), ("Take-profit (pts)", self.tp),
                           ("Stop-loss (pts)", self.sl)):
            ttk.Label(cfg2, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(cfg2, textvariable=var, width=6).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(cfg2, text="IA adaptative", variable=self.use_model).pack(side="left", padx=4)
        ttk.Checkbutton(cfg2, text="Simulation sans MT5", variable=self.synthetic).pack(side="left", padx=4)
        ttk.Checkbutton(cfg2, text="Autoriser compte réel", variable=self.confirm_live).pack(side="left", padx=4)

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
                 ("equity", "Equity"), ("fills", "Exécutions"), ("lat", "Décision p50 / p99"),
                 ("ia", "IA : précision / poids"), ("score", "Score signal"), ("obi", "OBI / flux"),
                 ("spread", "Spread (pts)"), ("risk", "Risque"),
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

        self.log = tk.Text(self, height=12, bg="#0a0d12", fg="#b9c2d0", insertbackground=FG,
                           relief="flat", font=("Consolas", 9), wrap="none")
        self.log.pack(fill="both", expand=True, padx=16, pady=(6, 14))

    # ------------------------------------------------------------------ #
    def _settings(self) -> Settings:
        mode = Mode(self.mode.get())
        lot = float(self.lot.get().replace(",", "."))
        base = Settings()
        return base.model_copy(update={
            "mode": mode, "symbol": self.symbol.get().strip(), "order_qty": lot,
            "max_net_inventory": max(base.max_net_inventory, lot),
            "max_daily_drawdown": float(self.maxdd.get().replace(",", ".")),
            "max_spread_points": float(self.maxspread.get().replace(",", ".")),
            "take_profit_points": float(self.tp.get().replace(",", ".")),
            "stop_loss_points": float(self.sl.get().replace(",", ".")),
            "confirm_live": self.confirm_live.get(),
        })

    def _start(self) -> None:
        try:
            s = self._settings()
        except ValueError as exc:
            messagebox.showerror("Réglage invalide", str(exc))
            return
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
            if not messagebox.askyesno("Mode LIVE", msg, icon="warning"):
                return
        self.thread = EngineThread(s, self.synthetic.get(), self.use_model.get())
        self.thread.start()
        self.b_start.state(["disabled"])
        self.b_stop.state(["!disabled"])
        self.b_kill.state(["!disabled"])

    def _stop(self) -> None:
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
            sc = st.get("score", 0.0)
            self.vals["score"].config(text=f"{sc:+.2f}", foreground=GREEN if sc > 0 else RED if sc < 0 else FG)
            self.vals["obi"].config(text=f"{st.get('obi', 0):+.2f} / {st.get('flow', 0):+.2f}")
            self.vals["spread"].config(text=fmt(st.get("spread_pts"), 1))
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
            self.vals["risk"].config(text="KILL" if killed else "OK", foreground=RED if killed else GREEN)
            self.status.config(text="● en marche" if snap["running"] else "● arrêt…",
                               foreground=GREEN if snap["running"] else MUTED)
        if t is not None and not t.is_alive():
            if t.error:
                messagebox.showerror("Zeus HFT", t.error)
            self.thread = None
            self.status.config(text="● arrêté", foreground=MUTED)
            self.b_start.state(["!disabled"])
            self.b_stop.state(["disabled"])
            self.b_kill.state(["disabled"])
        self.after(300, self._refresh)


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
