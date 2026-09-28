"""
REST API for the dashboard — session-cookie auth, agent control, settings,
and read endpoints for charts/logs. Credentials are write-only through the
API (never echoed back).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from . import config, store
from .engine.agent import hub

router = APIRouter(prefix="/api")

COOKIE = "sa_session"
SESSION_TTL = 30 * 86400


def _sign(payload: str) -> str:
    key = hashlib.sha256(config.ACCESS_PASSWORD.encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts)}"


def token_valid(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    ts, sig = token.split(".", 1)
    if not hmac.compare_digest(sig, _sign(ts)):
        return False
    try:
        return (time.time() - int(ts)) < SESSION_TTL
    except ValueError:
        return False


async def require_auth(request: Request) -> bool:
    token = request.cookies.get(COOKIE)
    hdr = request.headers.get("x-session", "")
    if not token_valid(token) and not token_valid(hdr):
        raise HTTPException(status_code=401, detail="unauthorized")
    return True


class LoginBody(BaseModel):
    password: str


class SettingsBody(BaseModel):
    settings: dict


@router.post("/login")
def login(body: LoginBody, response: Response):
    # modest brute-force dampening
    if body.password != config.ACCESS_PASSWORD:
        time.sleep(0.6)
        return {"ok": False, "error": "wrong password"}
    response.set_cookie(COOKIE, make_token(), max_age=SESSION_TTL,
                        httponly=True, samesite="strict")
    store.log("info", "dashboard login")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/health")
def health():
    return {"ok": True, "ts": time.time()}


def _masked_settings() -> dict:
    s = store.load_settings()
    s["deriv_pat"] = "***" if s.get("deriv_pat") else ""
    s["has_pat"] = bool(store.load_settings().get("deriv_pat"))
    return s


@router.get("/status", dependencies=[Depends(require_auth)])
def status():
    snap = hub.snapshot()
    snap["settings"] = _masked_settings()
    snap["last_signal"] = {s: b.last_signal for s, b in dict(hub.brains).items()}
    return snap


@router.get("/settings", dependencies=[Depends(require_auth)])
def get_settings():
    return {"settings": _masked_settings()}


@router.post("/settings", dependencies=[Depends(require_auth)])
def set_settings(body: SettingsBody):
    allowed = set(config.DEFAULT_SETTINGS) | {"paused_until", "deriv_app_id",
                                              "deriv_pat", "derivation_note"}
    updates = {k: v for k, v in body.settings.items() if k in allowed}
    # basic type sanity per key
    defaults = config.DEFAULT_SETTINGS
    for k, v in list(updates.items()):
        if k in defaults and not isinstance(v, type(defaults[k])) and v is not None:
            try:
                updates[k] = type(defaults[k])(v)
            except Exception:
                updates.pop(k)
    # redact masked secret back out
    if updates.get("deriv_pat") in ("***", ""):
        updates.pop("deriv_pat", None)
    for k, v in updates.items():
        store.save_setting(k, v)
    settings = store.load_settings()
    hub.apply_settings(settings)
    store.log("info", f"settings updated: {', '.join(updates.keys())}")
    # live-credential changes take effect immediately
    if hub.running:
        import asyncio
        asyncio.ensure_future(hub.sync_auth())
    return {"ok": True, "settings": _masked_settings()}


@router.post("/agent/start", dependencies=[Depends(require_auth)])
async def agent_start():
    if hub.running:
        return {"ok": True, "already": True}
    store.save_setting("agent_running", True)
    hub.settings = store.load_settings()
    import asyncio
    asyncio.ensure_future(hub.start())
    return {"ok": True}


@router.post("/agent/stop", dependencies=[Depends(require_auth)])
async def agent_stop():
    store.save_setting("agent_running", False)
    await hub.stop()
    return {"ok": True}


@router.get("/trades", dependencies=[Depends(require_auth)])
def trades(limit: int = 100, live: Optional[int] = None):
    lv = None if live is None else bool(live)
    return {"trades": store.get_trades(limit=min(limit, 1000), live=lv)}


@router.get("/equity", dependencies=[Depends(require_auth)])
def equity():
    return {
        "paper": store.get_equity(live=False, limit=2000),
        "live": store.get_equity(live=True, limit=2000),
        "paper_balance": hub.paper_balance if hasattr(hub, "paper_balance") else None,
    }


@router.get("/spikes", dependencies=[Depends(require_auth)])
def spikes(symbol: Optional[str] = None, limit: int = 200):
    return {"spikes": store.get_spikes(symbol, min(limit, 1000))}


@router.get("/signals", dependencies=[Depends(require_auth)])
def signals(limit: int = 100):
    return {"signals": store.get_signals(min(limit, 500))}


@router.get("/logs", dependencies=[Depends(require_auth)])
def logs(limit: int = 200):
    return {"logs": store.get_logs(min(limit, 1000))}


@router.get("/accounts", dependencies=[Depends(require_auth)])
async def accounts():
    """List Deriv Options accounts (demo/real) so the user can pick one."""
    from .deriv import auth as auth_mod

    try:
        accts = await auth_mod.list_accounts()
        return {"accounts": accts, "ok": True}
    except Exception as e:
        return {"accounts": [], "ok": False, "error": str(e)}
