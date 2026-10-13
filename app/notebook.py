"""
Permanent notebook — backs the agent's brain and results up to GitHub.

Free hosts wipe the filesystem on every restart, which would erase the
learned models, trade history and equity curve. This module mirrors the
SQLite database to the project's own GitHub repo (a separate branch, so
it never triggers a redeploy):

  * restore_if_available(): on boot, before the store opens, pull the
    latest snapshot so the agent continues exactly where it left off.
  * push_snapshot(): after each trading session writes, commit the DB.

Uses only the GitHub contents API with the repo's own token (GITHUB_TOKEN
env var, auto-provided when Render deploys from GitHub). Silent no-op when
the token is missing (local runs) — never crashes the agent.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger("spike.notebook")

BRANCH = "agent-memory"
API = "https://api.github.com"


def _repo_slug() -> str:
    # Render sets RENDER_GIT_REPO_SLUG = "username/reponame" on every deploy
    val = os.environ.get("RENDER_GIT_REPO_SLUG", "").strip().strip("/")
    if val:
        return val.replace(".git", "")
    # Manual override, e.g. GITHUB_REPO=nthato-matabane/potato-glue
    val = os.environ.get("GITHUB_REPO", "").strip().strip("/")
    if val:
        return val.replace(".git", "")
    # Older Render variables / other hosts provide the full URL
    for key in ("RENDER_GIT_REPO", "RENDER_GIT_REPO_URL", "GIT_REPO"):
        val = os.environ.get(key, "")
        if val:
            return (val.replace("https://github.com/", "")
                       .replace("git@github.com:", "")
                       .replace(".git", "")
                       .strip("/"))
    # Last resort: the repo this agent deploys from (keeps memory working
    # even if Render renames its env vars)
    return "nthato-matabane/potato-glue"


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")


def _db_path() -> Path:
    from . import config

    return config.DB_PATH


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
    }


def available() -> bool:
    return bool(_token() and _repo_slug())


def _get_ref(client: httpx.Client) -> str | None:
    r = client.get(f"{API}/repos/{_repo_slug()}/git/ref/heads/{BRANCH}",
                   headers=_headers(), timeout=15)
    if r.status_code == 200:
        return r.json()["object"]["sha"]
    return None


def _create_branch(client: httpx.Client, from_sha: str) -> bool:
    r = client.post(f"{API}/repos/{_repo_slug()}/git/refs",
                    headers=_headers(), timeout=15,
                    json={"ref": f"refs/heads/{BRANCH}", "sha": from_sha})
    return r.status_code in (201, 422)   # 422 = already exists


def _read_blob(client: httpx.Client) -> bytes | None:
    r = client.get(
        f"{API}/repos/{_repo_slug()}/contents/spike_agent.db?ref={BRANCH}",
        headers=_headers(), timeout=30)
    if r.status_code != 200:
        return None
    j = r.json()
    content = j.get("content") or ""
    if content:
        return base64.b64decode(content)
    # Large files (like a 3 MB SQLite brain) have empty inline content on
    # the Contents API but a working download_url. Fall back so a valid
    # brain on GitHub is always restorable (not silently treated as "no
    # snapshot").
    download_url = j.get("download_url") or ""
    if download_url:
        try:
            d = client.get(download_url, headers={"Authorization": f"Bearer {_token()}"}, timeout=90)
            if d.status_code == 200 and d.content:
                return d.content
        except Exception as e:
            logger.debug("notebook: download_url fallback failed (%s) — will retry on next boot", e)
    return None


def restore_if_available() -> bool:
    """Download the latest memory snapshot into place BEFORE the store opens."""
    if not available():
        return False
    try:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client() as client:
            blob = _read_blob(client)
        if not blob:
            logger.info("notebook: no snapshot in %s yet — fresh start", BRANCH)
            return False
        path.write_bytes(blob)
        logger.info("notebook: restored %d KB of memory from GitHub",
                    len(blob) // 1024)
        return True
    except Exception as e:
        logger.warning("notebook: restore failed (%s) — fresh start", e)
        return False


def _learnedness() -> dict:
    """How much the local brain has learned, for the push gate.

    We gate pushes on this so a half-studied (or freshly-booted) local DB
    never overwrites a more-learned brain already on GitHub. Progression,
    not a wipe loop.
    """
    from . import store
    from .engine.agent import hub
    trades = store.get_trades(limit=100000, live=False)
    paper = sum(1 for t in trades if not t.get("live"))
    live = sum(1 for t in trades if t.get("live"))
    total_updates = 0
    for b in hub.brains.values():
        total_updates += int(getattr(b.fast, "n_updates", 0) or 0)
        total_updates += int(getattr(b.slow, "n_updates", 0) or 0)
    return {
        "trade_count": paper + live,
        "paper_trades": paper,
        "live_trades": live,
        "total_updates": total_updates,
        "brains": len(list(hub.brains.keys())),
    }


def push_snapshot() -> bool:
    """Commit the current database file to the memory branch — but only if
    the local brain is at least as learned as the one already on GitHub.

    This is what turns the backup from a wipe loop into progression: a
    freshly-booted (or half-studied) local DB will NOT overwrite a more-
    learned brain on GitHub. The agent keeps building on the best-known
    brain instead of replacing it with a lesser one.
    """
    if not available():
        return False
    path = _db_path()
    if not path.exists():
        return False
    local = _learnedness()
    try:
        blob = path.read_bytes()
        with httpx.Client() as client:
            remote_meta = _meta_get(client)
            remote_learned = (remote_meta or {}).get("decoded", {}) if remote_meta else {}
            # If GitHub has a brain and local is behind on BOTH trade count
            # and model updates, skip the push — keep the better brain.
            if remote_learned:
                rc = int(remote_learned.get("trade_count", 0) or 0)
                ru = int(remote_learned.get("total_updates", 0) or 0)
                if (local["trade_count"] < rc and
                        local["total_updates"] < ru):
                    logger.info(
                        "notebook: skip push — GitHub brain ahead "
                        "(local %d trades / %d updates vs remote %d / %d)",
                        local["trade_count"], local["total_updates"], rc, ru)
                    return False
            sha = _get_ref(client)
            if not sha:
                main = client.get(f"{API}/repos/{_repo_slug()}/git/ref/heads/main",
                                  headers=_headers(), timeout=15)
                if main.status_code != 200:
                    logger.warning("notebook: cannot seed memory branch")
                    return False
                if not _create_branch(client, main.json()["object"]["sha"]):
                    return False
                sha = _get_ref(client)
            # Seed a learnedness meta on the fresh branch so the gate has a
            # baseline (empty) even before the first real push.
            if sha and not _meta_get(client):
                _meta_save(client, {"trade_count": 0, "paper_trades": 0,
                                     "live_trades": 0, "total_updates": 0,
                                     "brains": 0, "updated": __import__("time").time()},
                            None)
            existing = _read_blob(client)
            if existing == blob:
                return True                      # nothing new
            r = client.put(
                f"{API}/repos/{_repo_slug()}/contents/spike_agent.db",
                headers=_headers(), timeout=60,
                json={
                    "message": "agent memory snapshot",
                    "content": base64.b64encode(blob).decode(),
                    "branch": BRANCH,
                    "sha": _file_sha(client),
                })
            if r.status_code in (200, 201):
                logger.info("notebook: memory pushed to GitHub (%d KB)",
                            len(blob) // 1024)
                # refresh the learnedness meta so the gate tracks the
                # best-known brain (progression, not a loop).
                _meta_save(client, {
                    **local,
                    "updated": __import__("time").time(),
                    "db_sha": r.json().get("content", {}).get("sha")
                            if isinstance(r.json().get("content"), dict) else None,
                }, None)
                return True
            logger.warning("notebook: push failed (%s)", r.text[:200])
            return False
    except Exception as e:
        logger.warning("notebook: push error (%s)", e)
        return False


def _file_sha(client: httpx.Client) -> str | None:
    r = client.get(
        f"{API}/repos/{_repo_slug()}/contents/spike_agent.db?ref={BRANCH}",
        headers=_headers(), timeout=30)
    if r.status_code == 200:
        return r.json().get("sha")
    return None


def _meta_get(client: httpx.Client) -> dict | None:
    """Best-known learnedness on the memory branch (trade_count,
    total_model_updates). Used by the push gate so a half-studied local
    DB never overwrites a more-learned GitHub brain."""
    r = client.get(
        f"{API}/repos/{_repo_slug()}/contents/memory.meta.json?ref={BRANCH}",
        headers=_headers(), timeout=20)
    if r.status_code != 200:
        return None
    try:
        return r.json().get("content") and {
            **r.json(),
            "decoded": __import__("json").loads(
                base64.b64decode(r.json().get("content") or "[]") or {}),
        }
    except Exception:
        return None


def _meta_save(client: httpx.Client, meta: dict, sha: str | None) -> bool:
    import json as _json
    body = _json.dumps(meta, separators=(",", ":")).encode()
    r = client.put(
        f"{API}/repos/{_repo_slug()}/contents/memory.meta.json",
        headers=_headers(), timeout=30,
        json={
            "message": "agent memory: learnedness meta",
            "content": base64.b64encode(body).decode(),
            "branch": BRANCH,
            "sha": sha,
        })
    return r.status_code in (200, 201)
