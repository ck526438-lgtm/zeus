"""Endpoint Prometheus ``/metrics`` (format texte d'exposition), sans dépendance.

Servi par un thread HTTP séparé : la collecte lit un instantané du moteur et ne
touche jamais au chemin critique. Activer avec ``ZEUS_HFT_METRICS_PORT=9108``, puis
ajouter ``localhost:9108`` comme cible dans Prometheus (tableau de bord Grafana).
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


def render_metrics(snap: dict[str, Any], symbol: str) -> str:
    lab = f'symbol="{symbol}"'
    rk = snap.get("risk") or {}
    st = snap.get("strategy") or {}
    lines: list[str] = []

    def g(name: str, value: Any, help_: str, extra: str = "", kind: str = "gauge") -> None:
        if value is None:
            return
        if f"# HELP {name} " not in "\n".join(lines):
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {kind}")
        labels = lab + (f",{extra}" if extra else "")
        lines.append(f"zeus_{name.removeprefix('zeus_')}{{{labels}}} {float(value):.6g}")

    g("zeus_up", 1 if snap.get("running") else 0, "1 si le moteur tourne")
    g("zeus_ticks_total", snap.get("ticks"), "ticks traités", kind="counter")
    g("zeus_orders_total", snap.get("orders"), "ordres envoyés", kind="counter")
    g("zeus_fills_total", snap.get("fills"), "exécutions", kind="counter")
    g("zeus_position_lots", snap.get("position"), "position nette (lots)")
    g("zeus_pnl", snap.get("pnl"), "PnL net de la session")
    g("zeus_equity", rk.get("equity"), "equity suivie par le risk manager")
    g("zeus_daily_drawdown", rk.get("daily_drawdown"), "drawdown du jour")
    g("zeus_killed", 1 if rk.get("killed") else 0, "1 si le kill-switch est actif")
    g("zeus_halted", 1 if rk.get("halts") else 0, "1 si une pause (réduction seule) est active")
    g("zeus_network_latency_ms", rk.get("latency_ms"), "ping MT5 / aller-retour ordre")
    g("zeus_spread_points", st.get("spread_pts"), "spread courant")
    g("zeus_atr_points", st.get("atr_pts"), "ATR M1")
    for stage, key in (("tick_decision", "decision"), ("tick_order", "tick_to_order"),
                       ("send_ack", "ack")):
        for q in ("p50", "p99"):
            v = snap.get(f"{key}_{q}_us")
            if v is not None:
                g("zeus_latency_us", v, "latence par étape (µs)",
                  f'stage="{stage}",quantile="{q.replace("p", "0.")}"')
    return "\n".join(lines) + "\n"


class MetricsServer:
    def __init__(self, port: int, snapshot: Callable[[], dict[str, Any]], symbol: str) -> None:
        snap_fn, sym = snapshot, symbol

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.split("?")[0] not in ("/metrics", "/"):
                    self.send_error(404)
                    return
                try:
                    body = render_metrics(snap_fn(), sym).encode()
                except Exception as exc:  # pragma: no cover - défensif
                    self.send_error(500, str(exc))
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, name="metrics", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
