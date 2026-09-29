"""
Spike Agent — application entrypoint.

Run locally:   python -m app.main         (http://localhost:8000)
Run in Docker: the image runs the same command with uvicorn.

The agent auto-resumes on startup if it was switched on before — a cloud
redeploy or crash-restart picks up exactly where it left off, with the
learned model loaded from SQLite.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, store
from .api import router
from .engine.agent import hub
from . import notebook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("spike.app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Public URL last seen in browser traffic (free hosts wipe their filesystem
# on every redeploy, so we can't rely on a stored value alone). Used by the
# keep-alive loop when KEEPALIVE_URL isn't configured explicitly.
_public_url = ""


async def _keepalive_loop():
    """Ping ourselves so free hosts (Render: 15 min, Koyeb: 1 h of no
    incoming traffic) never spin the service down while the agent trades."""
    while True:
        await asyncio.sleep(max(60, config.KEEPALIVE_INTERVAL_S))
        target = config.KEEPALIVE_URL
        if not target:
            url = _public_url or str(store.load_settings().get("public_url") or "")
            target = f"{url}/api/health" if url else ""
        if not target:
            continue
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                await c.get(target)
        except Exception as e:
            logger.debug("keep-alive ping failed (%s): %s", target, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # permanent memory: pull the latest snapshot BEFORE the store opens
    notebook.restore_if_available()
    store.init()
    settings = store.load_settings()
    # lock in the profit settings on every boot (user never configures them)
    for k, v in config.FORCED_SETTINGS.items():
        if settings.get(k) != v:
            store.save_setting(k, v)
            settings[k] = v
            logger.info("forced setting %s = %s", k, v)
    hub.settings = settings
    if settings.get("agent_running") or config.AUTO_START:
        if not settings.get("agent_running"):
            store.save_setting("agent_running", True)
            hub.settings["agent_running"] = True
        # background task: the app must start serving immediately so free
        # hosts' health checks pass while symbols warm up (~60 s)
        logger.info("auto-resume: agent was ON (or AUTO_START), starting...")
        asyncio.create_task(hub.start())
    ka = asyncio.create_task(_keepalive_loop())
    yield
    # graceful shutdown — persist learning, leave live positions adoptable
    ka.cancel()
    try:
        await hub.stop()
    except Exception as e:
        logger.warning("shutdown error: %s", e)


app = FastAPI(title="Spike Agent", lifespan=lifespan)
app.include_router(router)


@app.middleware("http")
async def remember_public_url(request: Request, call_next):
    global _public_url
    host = request.headers.get("host", "")
    if host and "." in host and not host.startswith(("127.0.0.1", "localhost")):
        scheme = request.headers.get("x-forwarded-proto", "") or request.url.scheme
        url = f"{scheme}://{host}"
        if url != _public_url:
            _public_url = url
            try:
                store.save_setting("public_url", url)
                logger.info("public URL learned: %s", url)
            except Exception:
                pass
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# auth failures on /api/* return JSON instead of HTML
@app.exception_handler(401)
async def auth_handler(request: Request, exc: Exception):
    return JSONResponse({"error": "unauthorized"}, status_code=401)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
