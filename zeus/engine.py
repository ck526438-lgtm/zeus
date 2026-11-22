"""Boucle de trading en direct : Zeus analyse, ouvre, gère et coupe les trades."""
from __future__ import annotations

import logging
import time

from .journal import Journal, Notifier
from .model import ZeusModel
from .mt5_client import MT5Client
from .risk import RiskManager, lot_size
from .strategy import Strategy

log = logging.getLogger("zeus.engine")


class ZeusEngine:
    def __init__(self, cfg: dict, allow_live: bool = False):
        self.cfg = cfg
        self.t = cfg["trading"]
        self.client = MT5Client(cfg)
        model = ZeusModel.load(cfg["strategy"]["model_path"]) if cfg["strategy"]["use_model"] else None
        if cfg["strategy"]["use_model"] and model is None:
            log.warning("Aucun modèle IA trouvé — Zeus utilise uniquement l'analyse technique. "
                        "Lance `python main.py train` pour l'entraîner.")
        self.strategy = Strategy(cfg["strategy"], model)
        self.risk = RiskManager(cfg["risk"], self.t.get("sessions_utc"))
        self.journal = Journal(cfg["logging"]["journal"])
        self.notify = Notifier(cfg["telegram"])
        self.allow_live = allow_live
        self.last_bar: dict[str, object] = {}
        self.atr: dict[str, float] = {}
        self.open_tickets: dict[int, dict] = {}
        self.running = False

    # ------------------------------------------------------------------
    def start(self):
        self.client.connect()
        if not self.client.is_demo():
            if self.cfg["mode"] != "live" or not self.allow_live:
                self.client.shutdown()
                raise SystemExit("⛔ Compte RÉEL détecté. Mets `mode: live` dans config.yaml ET lance "
                                 "avec --confirm-live si tu acceptes le risque.")
            log.warning("⚠️  MODE RÉEL — de l'argent réel est en jeu.")
        for s in self.t["symbols"]:
            self.client.ensure_symbol(s)
        for p in self.client.positions():
            self.open_tickets[p.ticket] = {"symbol": p.symbol, "bars": 0}
        self.notify.send(f"⚡ Zeus démarré sur {', '.join(self.t['symbols'])} ({self.t['timeframe']})")
        self.running = True
        log.info("Zeus en marche — Ctrl+C pour arrêter.")
        try:
            while self.running:
                t0 = time.time()
                try:
                    self.tick()
                except Exception as e:
                    log.exception("Erreur dans la boucle : %s", e)
                time.sleep(max(0.0, self.t["loop_seconds"] - (time.time() - t0)))
        except KeyboardInterrupt:
            log.info("Arrêt demandé.")
        finally:
            self.notify.send("🛑 Zeus arrêté")
            self.client.shutdown()

    # ------------------------------------------------------------------
    def tick(self):
        acc = self.client.account()
        now = self.client.now()
        self.risk.new_day_check(now, acc.balance)
        self._detect_closed(now)

        for symbol in self.t["symbols"]:
            df = self.client.rates(symbol, self.t["timeframe"], self.t["history_bars"])
            closed = df.iloc[:-1]                       # on ignore la bougie en formation
            bar_time = closed["time"].iloc[-1]
            new_bar = self.last_bar.get(symbol) != bar_time
            positions = self.client.positions(symbol)

            if new_bar:
                self.last_bar[symbol] = bar_time
                feat = self.strategy.prepare(closed)
                self.atr[symbol] = float(feat["atr"].iloc[-1])
                for p in positions:
                    self._manage_exit_on_bar(p, feat)
                positions = self.client.positions(symbol)
                self._maybe_enter(symbol, feat, positions, acc, now)

            # Gestion continue (chaque seconde) : break-even + stop suiveur
            a = self.atr.get(symbol)
            if a:
                for p in self.client.positions(symbol):
                    self._trail(p, a)

    # ------------------------------------------------------------------
    def _maybe_enter(self, symbol, feat, positions, acc, now):
        if len(positions) >= self.t["max_positions_per_symbol"]:
            return
        if len(self.client.positions()) >= self.t["max_open_positions"]:
            return
        sig = self.strategy.evaluate(feat)
        if not sig.is_trade:
            log.debug("%s : %s", symbol, sig.reason)
            return
        ok, why = self.risk.can_open(symbol, now, acc.equity, self.client.spread_points(symbol))
        if not ok:
            log.info("%s : signal %s bloqué — %s", symbol, sig.reason, why)
            return

        tick = self.client.tick(symbol)
        price = tick.ask if sig.direction == 1 else tick.bid
        sl, tp = self.risk.levels(sig.direction, price, sig.atr)
        info = self.client.ensure_symbol(symbol)
        vol = lot_size(acc.balance, self.cfg["risk"]["risk_per_trade_pct"], abs(price - sl),
                       info.trade_tick_value, info.trade_tick_size,
                       info.volume_min, info.volume_max, info.volume_step)
        if vol <= 0:
            log.info("%s : lot trop petit pour respecter le risque, trade ignoré", symbol)
            return
        res = self.client.open(symbol, sig.direction, vol, sl, tp, comment=f"ZEUS {sig.score:.2f}")
        if res is None:
            return
        self.risk.on_open(symbol, now)
        self.open_tickets[res.order] = {"symbol": symbol, "bars": 0}
        side = "ACHAT" if sig.direction == 1 else "VENTE"
        log.info("✅ %s %s %.2f lots @ %.5f SL %.5f TP %.5f (%s)", side, symbol, vol, price, sl, tp, sig.reason)
        self.journal.write(time=now.isoformat(), event="open", symbol=symbol, direction=side,
                           volume=vol, price=price, sl=sl, tp=tp, score=round(sig.score, 3),
                           proba=sig.proba, reason=sig.reason, ticket=res.order)
        self.notify.send(f"✅ {side} {symbol} {vol} lots @ {price}\nSL {sl:.5f} | TP {tp:.5f}\n{sig.reason}")

    def _manage_exit_on_bar(self, p, feat):
        meta = self.open_tickets.setdefault(p.ticket, {"symbol": p.symbol, "bars": 0})
        meta["bars"] += 1
        d = self.client.direction_of(p)
        exit_, why = self.strategy.should_exit(feat, d)
        if not exit_ and meta["bars"] >= self.cfg["strategy"]["max_hold_bars"]:
            exit_, why = True, "durée max atteinte"
        if exit_ and self.client.close(p, comment="ZEUS cut"):
            log.info("✂️  Coupure %s #%s : %s", p.symbol, p.ticket, why)
            meta["reason"] = why

    def _trail(self, p, a):
        d = self.client.direction_of(p)
        tick = self.client.tick(p.symbol)
        price = tick.bid if d == 1 else tick.ask
        new_sl = self.risk.trail(d, p.price_open, price, p.sl or (p.price_open - d * 1e9), a)
        if new_sl is not None and self.client.modify_sl(p, new_sl):
            log.debug("SL %s #%s → %.5f", p.symbol, p.ticket, new_sl)

    def _detect_closed(self, now):
        alive = {p.ticket for p in self.client.positions()}
        for ticket in list(self.open_tickets):
            if ticket in alive:
                continue
            meta = self.open_tickets.pop(ticket)
            pnl = self.client.closed_profit(ticket)
            self.risk.on_close(pnl, now)
            reason = meta.get("reason", "SL/TP")
            log.info("💰 Fermé %s #%s PnL %.2f (%s)", meta["symbol"], ticket, pnl, reason)
            self.journal.write(time=now.isoformat(), event="close", symbol=meta["symbol"],
                               pnl=round(pnl, 2), reason=reason, ticket=ticket)
            self.notify.send(f"{'🟢' if pnl >= 0 else '🔴'} Fermé {meta['symbol']} : {pnl:+.2f} ({reason})")
