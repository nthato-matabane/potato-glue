"""
Spike Agent — configuration.
Environment variables (optionally via .env) provide credentials and
baseline options; everything the user changes at runtime (mode, stake,
symbols, risk limits) lives in the SQLite settings table instead.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional — env vars still work
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ---- Deriv API (new Options API) ------------------------------------------
DERIV_APP_ID: str = _env("DERIV_APP_ID", "")
DERIV_PAT: str = _env("DERIV_PAT", "")
DERIV_API_BASE: str = _env("DERIV_API_BASE", "https://api.derivws.com")
DERIV_WS_PUBLIC: str = _env(
    "DERIV_WS_PUBLIC", "wss://api.derivws.com/trading/v1/options/ws/public"
)

# ---- Free-host support (Render / Koyeb / Fly) ------------------------------
# Start the agent automatically at boot. Free hosts wipe the filesystem on
# every redeploy, which resets the "agent running" switch — AUTO_START keeps
# the agent on regardless (set to "true" in render.yaml).
AUTO_START: bool = _env("AUTO_START", "").lower() in ("1", "true", "yes")

# URL pinged every KEEPALIVE_INTERVAL_S seconds so a free host never puts
# the service to sleep after ~15 min without traffic. Leave empty to use
# the public URL the dashboard was last visited on (learned automatically).
KEEPALIVE_URL: str = _env("KEEPALIVE_URL", "")
KEEPALIVE_INTERVAL_S: int = int(_env("KEEPALIVE_INTERVAL_S", "300"))

# ---- App ------------------------------------------------------------------
ACCESS_PASSWORD: str = _env("ACCESS_PASSWORD", "changeme")
PAPER_BALANCE: float = float(_env("PAPER_BALANCE", "1000"))
HOST: str = _env("HOST", "0.0.0.0")
PORT: int = int(_env("PORT", "8000"))
DATA_DIR: Path = Path(_env("DATA_DIR", "./data"))
DB_PATH: Path = DATA_DIR / "spike_agent.db"

# ---- Tradable universe (user picked the fast pair + majors) --------------
DEFAULT_SYMBOLS: list[str] = ["BOOM1000", "CRASH1000", "BOOM500", "CRASH500",
                              "BOOM150N", "CRASH150N"]

SYMBOL_META: dict[str, dict] = {
    # average ticks between spikes is the documented name-number for most
    # symbols; 150N/300N are the newer generation (measured ~100-180 ticks).
    "BOOM1000":  {"label": "Boom 1000",  "avg_interval": 1000, "spike_dir": +1},
    "CRASH1000": {"label": "Crash 1000", "avg_interval": 1000, "spike_dir": -1},
    "BOOM500":   {"label": "Boom 500",   "avg_interval": 500,  "spike_dir": +1},
    "CRASH500":  {"label": "Crash 500",  "avg_interval": 500,  "spike_dir": -1},
    "BOOM150N":  {"label": "Boom 150",   "avg_interval": 130,  "spike_dir": +1},
    "CRASH150N": {"label": "Crash 150",  "avg_interval": 130,  "spike_dir": -1},
    "BOOM300N":  {"label": "Boom 300",   "avg_interval": 300,  "spike_dir": +1},
    "CRASH300N": {"label": "Crash 300",  "avg_interval": 300,  "spike_dir": -1},
    "BOOM50":    {"label": "Boom 50",    "avg_interval": 50,   "spike_dir": +1},
    "CRASH50":   {"label": "Crash 50",   "avg_interval": 50,   "spike_dir": -1},
    "BOOM600":   {"label": "Boom 600",   "avg_interval": 600,  "spike_dir": +1},
    "CRASH600":  {"label": "Crash 600",  "avg_interval": 600,  "spike_dir": -1},
    "BOOM900":   {"label": "Boom 900",   "avg_interval": 900,  "spike_dir": +1},
    "CRASH900":  {"label": "Crash 900",  "avg_interval": 900,  "spike_dir": -1},
}

# Contracts available on these symbols: multipliers only.
AVAILABLE_MULTIPLIERS = [100, 150, 200, 300, 400, 500]

# ---- Runtime defaults (overridable from the settings page) ---------------
DEFAULT_SETTINGS: dict = {
    "deriv_app_id": _env("DERIV_APP_ID", ""),   # settable from the settings page
    "deriv_pat": _env("DERIV_PAT", ""),          # Personal Access Token (secret)
    "agent_running": False,       # switched on from the dashboard
    "mode": "auto",               # auto | drift | spike
    "live_enabled": False,        # False = paper trades only
    "account_mode": "demo",       # demo | real
    "account_id": "",             # Options account (e.g. A1234...) — from OTP listing
    "symbols": DEFAULT_SYMBOLS,
    "stake_usd": 1.0,             # margin per trade (min $1, max $500)
    "multiplier": 500,            # x500 = max profit per $1 when the EV gate passes
    "max_daily_loss_usd": 20.0,   # halt live trading for the day past this
    "max_consecutive_losses": 5,  # pause live trading after N losses in a row
    "pause_after_losses_min": 30,
    "daily_loss_pct": 10.0,       # % of balance — secondary circuit breaker
    "stop_loss_pct": 60.0,        # exit position at -60% of margin (before stop-out)
    "take_profit_pct": 100.0,     # exit at +100% of margin (1:1 payoff at x500)
    "max_hold_ticks": 0,          # 0 = auto (derived from symbol interval)
    "entry_threshold": 0.50,      # min model confidence to enter (spike mode)
    "exit_threshold": 0.35,       # model confidence that forces exit (spike imminent)
    "auto_min_trades": 30,        # paper trades per mode before auto picks a side
    "auto_switch_margin": 0.20,   # switch modes only if other side is 20% better
    "warmup_ticks": 4000,         # historical ticks fetched at startup
    "min_balance_usd": 2.0,       # never trade below this balance
}

# Locked-in profit settings, re-applied on EVERY boot so the user never
# has to touch the dashboard: $1 per trade, x500 leverage (maximum profit
# per $1 whenever the EV gate approves), 1:1 payoff exits.
FORCED_SETTINGS: dict = {
    "stake_usd": 1.0,
    "multiplier": 500,
    "take_profit_pct": 100.0,
    "stop_loss_pct": 60.0,
}
