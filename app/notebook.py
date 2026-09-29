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
    # Render provides the origin URL it deployed from
    for key in ("RENDER_GIT_REPO", "RENDER_GIT_REPO_URL", "GIT_REPO"):
        val = os.environ.get(key, "")
        if val:
            return val.replace("https://github.com/", "").replace(".git", "")
    return ""


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
    return base64.b64decode(r.json().get("content") or b"")


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


def push_snapshot() -> bool:
    """Commit the current database file to the memory branch (best effort)."""
    if not available():
        return False
    path = _db_path()
    if not path.exists():
        return False
    try:
        blob = path.read_bytes()
        with httpx.Client() as client:
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
