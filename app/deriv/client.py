"""
Deriv WebSocket client (new Options API).

Two channels share one implementation:
  * Public  — wss://api.derivws.com/.../ws/public  (ticks, history, no auth)
  * Authed  — OTP URL from REST                    (balance, proposal, buy, sell)

Design:
  * rpc(): request/response correlated by req_id, ticks keep flowing while
    waiting (routing happens in the reader loop, never blocked by rpc).
  * Streams: first response carries subscription.id → registered to a
    handler; later frames are routed there (or by msg_type fallback).
  * Auto-reconnect with backoff; authed channel re-fetches an OTP on every
    reconnect (they are single-use and expire in 120s).
"""

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Optional

import websockets

logger = logging.getLogger("spike.deriv")

Handler = Callable[[dict], Awaitable[None]]


class Channel:
    def __init__(self, name: str, url_provider, on_reconnect: Optional[Callable] = None):
        self.name = name
        self._url_provider = url_provider          # async () -> str
        self._on_reconnect = on_reconnect          # async () -> None (resubscribe)
        self.ws = None
        self.connected = False
        self.last_error: str = ""
        self._pending: dict[int, asyncio.Future] = {}
        self._sub_handlers: dict[str, Handler] = {}
        self._symbol_handlers: dict[str, Handler] = {}
        self._type_routes: dict[str, Handler] = {}
        self._req_seq = 0
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=f"ws-{self.name}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        ws, self.ws = self.ws, None
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
        self.connected = False
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result({"error": {"code": "Closed", "message": "channel closed"}})
        self._pending.clear()

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                url = await self._url_provider()
                async with websockets.connect(url, open_timeout=20, close_timeout=5) as ws:
                    self.ws = ws
                    self.connected = True
                    attempt = 0
                    logger.info("[%s] connected", self.name)
                    if self._on_reconnect:
                        try:
                            await self._on_reconnect()
                        except Exception as e:
                            logger.warning("[%s] resubscribe failed: %s", self.name, e)
                    async for raw in ws:
                        if self._stopping:
                            break
                        self._route(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                logger.warning("[%s] disconnected: %s", self.name, e)
            finally:
                self.connected = False
                self.ws = None

            if self._stopping:
                break
            attempt += 1
            delay = min(2 ** min(attempt, 5), 60)
            # surface pending rpcs so callers don't hang
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_result({"error": {"code": "Closed",
                                              "message": f"connection lost ({self.last_error})"}})
            self._pending.clear()
            logger.info("[%s] reconnect in %ss", self.name, delay)
            await asyncio.sleep(delay)

    # ---- request/response -------------------------------------------------

    def _next_id(self) -> int:
        self._req_seq += 1
        return self._req_seq

    async def rpc(self, payload: dict, timeout: float = 15.0) -> dict:
        if not self.connected or not self.ws:
            return {"error": {"code": "NotConnected", "message": f"{self.name} channel not connected"}}
        req_id = self._next_id()
        payload = dict(payload)
        payload["req_id"] = req_id
        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self.ws.send(json.dumps(payload))
        except Exception as e:
            self._pending.pop(req_id, None)
            return {"error": {"code": "SendFailed", "message": str(e)}}
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            return {"error": {"code": "Timeout", "message": f"no response in {timeout}s"}}
        finally:
            self._pending.pop(req_id, None)

    # ---- subscriptions ----------------------------------------------------

    async def subscribe(self, payload: dict, handler: Handler,
                        symbol_key: Optional[str] = None,
                        timeout: float = 15.0) -> bool:
        """Send a subscribe request; route its stream to handler."""
        resp = await self.rpc(payload, timeout=timeout)
        if resp.get("error"):
            logger.error("[%s] subscribe failed: %s", self.name, resp["error"])
            return False
        sub_id = ((resp.get("subscription") or {}).get("id"))
        if not sub_id:
            tick = resp.get("tick") or {}
            sub_id = tick.get("id")
        if sub_id:
            self._sub_handlers[str(sub_id)] = handler
        if symbol_key:
            self._symbol_handlers[symbol_key] = handler
        # deliver the first frame immediately (it may carry live data)
        try:
            await handler(resp)
        except Exception as e:
            logger.error("[%s] handler error: %s", self.name, e)
        return True

    def route_type(self, msg_type: str, handler: Handler) -> None:
        """Register a fallback handler for a msg_type (e.g. balance updates)."""
        self._type_routes[msg_type] = handler

    # ---- routing ----------------------------------------------------------

    def _route(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        req_id = msg.get("req_id")
        sub = (msg.get("subscription") or {}).get("id")

        if req_id and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if not fut.done():
                fut.set_result(msg)
            # also bind stream handler if this was a subscribe
            if sub:
                # handler registered by subscribe() right after rpc returns
                pass
            return

        handler = None
        if sub:
            handler = self._sub_handlers.get(str(sub))
        if handler is None:
            msg_type = msg.get("msg_type", "")
            if msg_type == "tick":
                tick = msg.get("tick") or {}
                handler = (self._sub_handlers.get(str(tick.get("id")))
                           or self._symbol_handlers.get(tick.get("symbol"), None))
            if handler is None:
                handler = self._type_routes.get(msg.get("msg_type", ""))
        if handler is None:
            return

        async def _run() -> None:
            try:
                await handler(msg)
            except Exception as e:
                logger.error("[%s] handler error: %s", self.name, e)

        asyncio.ensure_future(_run())


# ---- channel factories ----------------------------------------------------

def public_channel() -> Channel:
    from .. import config

    async def url() -> str:
        return config.DERIV_WS_PUBLIC

    return Channel("public", url)


def authed_channel(on_reconnect: Optional[Callable] = None) -> Channel:
    """Authenticated channel — URL comes from a fresh OTP on every (re)connect."""

    async def url() -> str:
        from . import auth as auth_mod

        app_id, pat = auth_mod.credentials()
        if not pat or not app_id:
            raise RuntimeError("Deriv credentials not configured")
        account_id = _current_account_id()
        if not account_id:
            raise RuntimeError("no Options account selected")
        return await auth_mod.get_otp_ws_url(account_id)

    return Channel("authed", url, on_reconnect=on_reconnect)


# account id is runtime state set by the agent from settings
_account_id = ""


def set_account_id(account_id: str) -> None:
    global _account_id
    _account_id = account_id or ""


def _current_account_id() -> str:
    return _account_id


# ---- helpers --------------------------------------------------------------

async def fetch_history(channel: Channel, symbol: str, count: int = 4000,
                        page_pause: float = 0.25) -> list[tuple[float, float]]:
    """Paginated tick history, oldest-first. Server pages cap around 1000."""
    out: list[tuple[float, float]] = []
    end: object = "latest"
    pages = 0
    while len(out) < count and pages < max(1, count // 1000 + 2):
        pages += 1
        resp = await channel.rpc({
            "ticks_history": symbol,
            "count": min(1000, count - len(out)),
            "end": end,
            "style": "ticks",
        }, timeout=20)
        if resp.get("error"):
            break
        hist = (resp.get("history") or {})
        prices = hist.get("prices") or []
        times = hist.get("times") or []
        if not prices:
            break
        page = list(zip(times, prices))
        out = page + out                      # older pages prepend
        end = int(min(times)) - 1
        if len(prices) < 500:                 # ran out of history
            break
        await asyncio.sleep(page_pause)
    return out[-count:]
