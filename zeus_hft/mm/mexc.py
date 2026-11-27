"""Adaptateur MEXC Futures (perpétuels USDT) : flux public, ordres post-only, flux privé.

Sources (documentation officielle MEXC, api-docs/futures) :
- WebSocket : wss://contract.mexc.com/edge ; ping ``{"method":"ping"}`` ;
  ``sub.depth`` -> ``push.depth`` (quantités absolues, ``version`` +1 à chaque message) ;
  ``sub.deal`` -> ``push.deal``.
- Snapshot : GET /api/v1/contract/depth/{symbol} -> ``asks``, ``bids``, ``version``.
- Contrat : GET /api/v1/contract/detail?symbol= -> priceUnit, contractSize, minVol, volUnit,
  makerFeeRate, takerFeeRate.
- Ordre : POST /api/v1/private/order/create ; type 2 = Post Only ; side 1 ouvrir long,
  2 fermer short, 3 ouvrir short, 4 fermer long ; openType 1 = isolé ; externalOid = notre id.
- Signature : HMAC-SHA256(secret, accessKey + Request-Time + paramètres) ; POST : le JSON
  tel quel ; en-têtes ApiKey, Request-Time, Signature, Content-Type: application/json.
- Flux privé : login puis ``push.personal.order`` (état) et ``push.personal.order.deal`` (fills).

⚠ Cette session de développement n'a pas d'accès réseau à MEXC : l'adaptateur est testé
contre un faux exchange. Valider d'abord en DRY_RUN, puis en LIVE avec 1 contrat minimum.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from zeus_hft.feeds.base import backoff_delays
from zeus_hft.mm.config import MMSettings

log = logging.getLogger("zeus_hft.mm.mexc")
BUY, SELL = 1, -1
STATE_CANCELED, STATE_INVALID = 4, 5


# ---------------------------------------------------------------------- parsing (pur, testé)
def _levels(raw: Any) -> list[tuple[float, float]]:
    """[[prix, quantité, nb_ordres], ...] -> [(prix, quantité)]"""
    return [(float(x[0]), float(x[1])) for x in raw or ()]


def parse_ws(msg: dict[str, Any]) -> list[tuple]:
    ch = msg.get("channel")
    data = msg.get("data")
    ts = int(msg.get("ts") or 0)
    if ch == "push.depth" and isinstance(data, dict):
        return [("depth", ts, _levels(data.get("bids")), _levels(data.get("asks")),
                 int(data["version"]))]
    if ch == "push.deal" and data is not None:
        rows = data if isinstance(data, list) else [data]
        out = []
        for d in rows:
            side = BUY if int(d.get("T", 1)) == 1 else SELL
            out.append(("trade", int(d.get("t") or ts), float(d["p"]), float(d["v"]), side))
        return out
    return []


def parse_snapshot(body: dict[str, Any], ts: int) -> tuple:
    d = body.get("data") or {}
    return ("snap", int(d.get("timestamp") or ts), _levels(d.get("bids")), _levels(d.get("asks")),
            int(d["version"]))


def parse_contract(body: dict[str, Any]) -> dict[str, float]:
    d = body.get("data") or {}
    if isinstance(d, list):
        d = d[0] if d else {}
    return {"tick": float(d["priceUnit"]), "contract_size": float(d["contractSize"]),
            "min_vol": float(d.get("minVol", 1)), "vol_step": float(d.get("volUnit", 1)),
            "maker_fee": float(d.get("makerFeeRate", 0.0)), "taker_fee": float(d.get("takerFeeRate", 0.0))}


def order_side_code(side: int, closing: bool) -> int:
    if side == BUY:
        return 2 if closing else 1       # fermer short / ouvrir long
    return 4 if closing else 3           # fermer long / ouvrir short


def sign(secret: str, key: str, req_time: str, payload: str) -> str:
    return hmac.new(secret.encode(), (key + req_time + payload).encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------- REST
class MexcRest:
    def __init__(self, s: MMSettings,
                 transport: Callable[[str, str, dict[str, str], bytes | None], dict[str, Any]] | None = None) -> None:
        self.s = s
        self.base = s.rest_url.rstrip("/")
        self.key = s.api_key.get_secret_value()
        self.secret = s.api_secret.get_secret_value()
        self._transport = transport or _urllib_transport

    def public(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self._transport("GET", self.base + path + q, {}, None)

    def private(self, method: str, path: str, params: dict[str, Any] | None = None,
                body: Any = None) -> dict[str, Any]:
        t = str(int(time.time() * 1000))
        if method == "GET":
            qs = "&".join(f"{k}={params[k]}" for k in sorted(params or {}))
            payload, url, data = qs, self.base + path + (("?" + qs) if qs else ""), None
        else:
            payload = json.dumps(body, separators=(",", ":"))
            url, data = self.base + path, payload.encode()
        headers = {"ApiKey": self.key, "Request-Time": t, "Signature": sign(self.secret, self.key, t, payload),
                   "Content-Type": "application/json", "Recv-Window": "10"}
        return self._transport(method, url, headers, data)


def _urllib_transport(method: str, url: str, headers: dict[str, str], data: bytes | None) -> dict[str, Any]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "{}")
        except ValueError:
            return {"success": False, "code": e.code, "message": str(e)}


# ---------------------------------------------------------------------- flux public
class MexcFeed:
    def __init__(self, s: MMSettings, rest: MexcRest | None = None) -> None:
        self.s = s
        self.rest = rest or MexcRest(s)
        self.symbol = s.mexc_symbol()
        self.connected = False
        self._resync = asyncio.Event()

    def request_resync(self) -> None:
        self._resync.set()

    async def snapshot(self) -> tuple:
        body = await asyncio.to_thread(self.rest.public, f"/api/v1/contract/depth/{self.symbol}", {"limit": 1000})
        return parse_snapshot(body, int(time.time() * 1000))

    async def run(self, out: asyncio.Queue, stop: asyncio.Event) -> None:
        import websockets

        for delay in backoff_delays(0.5, 30.0):
            if stop.is_set():
                return
            try:
                async with websockets.connect(self.s.ws_url, ping_interval=None, max_size=2**23) as ws:
                    for ch in ("sub.depth", "sub.deal"):
                        await ws.send(json.dumps({"method": ch, "param": {"symbol": self.symbol, "compress": False}}))
                    self.connected = True
                    log.info("flux MEXC connecté (%s)", self.symbol)
                    await out.put(await self.snapshot())
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        while not stop.is_set():
                            if self._resync.is_set():
                                self._resync.clear()
                                await out.put(await self.snapshot())
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=self.s.max_feed_silence_s)
                            except asyncio.TimeoutError:
                                raise ConnectionError("aucun message : flux figé")
                            for ev in parse_ws(json.loads(raw)):
                                await out.put(ev)
                    finally:
                        pinger.cancel()
            except Exception as exc:  # réseau, protocole, flux figé : on reconnecte
                self.connected = False
                await out.put(("down", int(time.time() * 1000), str(exc)))
                log.warning("flux MEXC interrompu (%s) : reconnexion dans %.1f s", exc, delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    continue

    @staticmethod
    async def _ping(ws: Any) -> None:
        while True:
            await asyncio.sleep(15)
            await ws.send(json.dumps({"method": "ping"}))


# ---------------------------------------------------------------------- exécution
class MexcGateway:
    """Interface QuoteManager : place / cancel / cancel_all ; événements via ``on_event``."""

    def __init__(self, s: MMSettings, rest: MexcRest | None = None) -> None:
        self.s = s
        self.rest = rest or MexcRest(s)
        self.symbol = s.mexc_symbol()
        self.on_event: Callable[..., None] = lambda *a, **k: None
        self.order_ids: dict[str, str] = {}          # externalOid -> orderId
        self.by_order_id: dict[str, str] = {}

    def fetch_specs(self) -> dict[str, float]:
        return parse_contract(self.rest.public("/api/v1/contract/detail", {"symbol": self.symbol}))

    def fetch_inventory(self) -> float:
        body = self.rest.private("GET", "/api/v1/private/position/open_positions", {"symbol": self.symbol})
        inv = 0.0
        for p in body.get("data") or []:
            vol = float(p.get("holdVol", 0))
            inv += vol if int(p.get("positionType", 1)) == 1 else -vol
        return inv

    async def place(self, cid: str, side: int, price: float, qty: float, closing: bool) -> None:
        body = {"symbol": self.symbol, "price": price, "vol": qty, "side": order_side_code(side, closing),
                "type": 2, "openType": 1, "externalOid": cid}
        if not closing:
            body["leverage"] = self.s.leverage
        res = await asyncio.to_thread(self.rest.private, "POST", "/api/v1/private/order/create", None, body)
        if res.get("success") and res.get("data"):
            oid = str(res["data"].get("orderId") if isinstance(res["data"], dict) else res["data"])
            self.order_ids[cid], self.by_order_id[oid] = oid, cid
            self.on_event("ack", cid)
        else:
            log.warning("ordre %s refusé par MEXC : %s %s", cid, res.get("code"), res.get("message"))
            self.on_event("rejected", cid)

    async def cancel(self, cid: str) -> None:
        oid = self.order_ids.get(cid)
        if oid is None:
            res = await asyncio.to_thread(self.rest.private, "POST", "/api/v1/private/order/cancel_with_external",
                                          None, {"symbol": self.symbol, "externalOid": cid})
        else:
            res = await asyncio.to_thread(self.rest.private, "POST", "/api/v1/private/order/cancel", None, [oid])
        ok = bool(res.get("success"))
        if ok and isinstance(res.get("data"), list) and res["data"]:
            ok = int(res["data"][0].get("errorCode", 0) or 0) == 0
        self.on_event("canceled" if ok else "cancel_rejected", cid)

    async def cancel_all(self) -> None:
        await asyncio.to_thread(self.rest.private, "POST", "/api/v1/private/order/cancel_all", None,
                                {"symbol": self.symbol})

    # --- flux privé : exécutions et états ---------------------------------- #
    def handle_private(self, msg: dict[str, Any]) -> None:
        ch, d = msg.get("channel"), msg.get("data") or {}
        if not isinstance(d, dict) or (d.get("symbol") and d.get("symbol") != self.symbol):
            return
        cid = d.get("externalOid") or self.by_order_id.get(str(d.get("orderId")))
        if not cid:
            return
        if ch == "push.personal.order.deal":
            self.on_event("fill", cid, float(d.get("vol", 0)), float(d.get("price", 0)), float(d.get("fee", 0) or 0))
        elif ch == "push.personal.order":
            st = int(d.get("state", 0))
            if st == STATE_CANCELED:
                self.on_event("canceled", cid)
            elif st == STATE_INVALID:
                self.on_event("rejected", cid)

    async def run_private(self, stop: asyncio.Event) -> None:
        import websockets

        key, secret = self.rest.key, self.rest.secret
        for delay in backoff_delays(0.5, 30.0):
            if stop.is_set():
                return
            try:
                async with websockets.connect(self.s.ws_url, ping_interval=None) as ws:
                    t = str(int(time.time() * 1000))
                    await ws.send(json.dumps({"method": "login", "param": {
                        "apiKey": key, "reqTime": t, "signature": sign(secret, key, t, "")}}))
                    await ws.send(json.dumps({"method": "personal.filter", "param": {"filters": [
                        {"filter": "order", "rules": [self.symbol]},
                        {"filter": "order.deal", "rules": [self.symbol]}]}}))
                    pinger = asyncio.create_task(MexcFeed._ping(ws))
                    try:
                        while not stop.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=60)
                            msg = json.loads(raw)
                            if msg.get("channel") == "rs.login" and msg.get("data") not in ("success", True):
                                raise PermissionError(f"login MEXC refusé : {msg.get('data')}")
                            self.handle_private(msg)
                    finally:
                        pinger.cancel()
            except PermissionError:
                raise
            except Exception as exc:
                log.warning("flux privé MEXC interrompu (%s) : reconnexion", exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    continue
