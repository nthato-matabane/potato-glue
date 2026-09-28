"""
Deriv REST helpers for the new Options API.

Flow (see developers.deriv.com/docs/workflows):
  1. PAT (personal access token) + Deriv-App-ID headers authenticate REST.
  2. POST .../accounts/{accountId}/otp returns a one-time WebSocket URL
     (valid 120s, single use) scoped to demo or real endpoint.
"""

from typing import Optional

import httpx

from .. import config


class AuthError(Exception):
    pass


def credentials() -> tuple[str, str]:
    """(app_id, pat) — DB settings win over env so the user can paste
    credentials on the Settings page of the running app."""
    app_id = str(config.DERIV_APP_ID or "")
    pat = str(config.DERIV_PAT or "")
    try:
        from .. import store

        s = store.load_settings()
        app_id = str(s.get("deriv_app_id") or app_id)
        pat = str(s.get("deriv_pat") or pat)
    except Exception:
        pass
    return app_id, pat


def _headers() -> dict:
    app_id, pat = credentials()
    if not pat:
        raise AuthError("No Deriv Personal Access Token yet — create one at "
                        "developers.deriv.com (see README) and paste it in Settings.")
    if not app_id:
        raise AuthError("No Deriv App ID yet — register a PAT-type app at "
                        "developers.deriv.com and paste its App ID in Settings.")
    return {
        "Authorization": f"Bearer {pat}",
        "Deriv-App-ID": str(app_id),
        "Content-Type": "application/json",
    }


async def list_accounts() -> list[dict]:
    """GET /trading/v1/options/accounts — returns Options accounts (demo+real)."""
    url = f"{config.DERIV_API_BASE}/trading/v1/options/accounts"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=_headers())
    if r.status_code != 200:
        raise AuthError(f"list accounts failed (HTTP {r.status_code}): {r.text[:300]}")
    data = r.json()
    # response shape: {"data": {"accounts": [...]}} or a bare list
    if isinstance(data, dict):
        inner = data.get("data", data)
        if isinstance(inner, dict):
            return inner.get("accounts", inner.get("account", []) if isinstance(inner.get("account"), list) else [inner])
        if isinstance(inner, list):
            return inner
    return data if isinstance(data, list) else []


async def get_otp_ws_url(account_id: str) -> str:
    """Exchange PAT for a one-time authenticated WebSocket URL (120s validity)."""
    if not account_id:
        raise AuthError("No Options account selected — pick one in Settings.")
    url = f"{config.DERIV_API_BASE}/trading/v1/options/accounts/{account_id}/otp"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=_headers())
    if r.status_code != 200:
        raise AuthError(f"OTP request failed (HTTP {r.status_code}): {r.text[:300]}")
    body = r.json()
    ws_url = None
    if isinstance(body, dict):
        ws_url = (body.get("data") or {}).get("url") or body.get("url")
    if not ws_url:
        raise AuthError(f"OTP response missing URL: {str(body)[:300]}")
    return ws_url
