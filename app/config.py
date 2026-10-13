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

# ---- Tradable universe ----------------------------------------------------
# TWO symbols by default — one Boom + one Crash is exactly what the strategy
# needs (buys on Boom, sells on Crash). The free hosts (0.1 CPU) crash-loop
# with 6 symbols warming up; 2 keeps the same brain quality per symbol.
# You can add more symbols later from the settings page if you upgrade hosts.
DEFAULT_SYMBOLS: list[str] = ["BOOM500", "CRASH500", "BOOM150N", "1HZ100V"]

# Per-symbol metadata. Every tradable symbol carries a "class" (the market
# behaviour family) and a "template" (which strategy brain handles it):
#   class="spike"  template="spike"  -> SymbolBrain (spike-timing hazard model,
#                                       drift + spike modes, pre-spike exit). Fits
#                                       Boom/Crash (rare directional spikes).
#   class="volatility" template="volatility" -> VolWatch (observe-only for now:
#                                       subscribe + track price/interval, no entries,
#                                       no spike model). For Volatility 100/25/... (1s)
#                                       continuous-movement indices. A real volatility
#                                       template is deferred until we've watched them.
#   (more classes/templates can be added here as the universe grows)
SYMBOL_META: dict[str, dict] = {
    # ---- Boom / Crash (spike template; rare directional spikes) ----
    "BOOM1000":  {"label": "Boom 1000",  "class": "spike", "template": "spike", "avg_interval": 1000, "spike_dir": +1},
    "CRASH1000": {"label": "Crash 1000", "class": "spike", "template": "spike", "avg_interval": 1000, "spike_dir": -1},
    "BOOM500":   {"label": "Boom 500",   "class": "spike", "template": "spike", "avg_interval": 500,  "spike_dir": +1},
    "CRASH500":  {"label": "Crash 500",  "class": "spike", "template": "spike", "avg_interval": 500,  "spike_dir": -1},
    "BOOM150N":  {"label": "Boom 150",   "class": "spike", "template": "spike", "avg_interval": 130,  "spike_dir": +1},
    "CRASH150N": {"label": "Crash 150",  "class": "spike", "template": "spike", "avg_interval": 130,  "spike_dir": -1},
    "BOOM300N":  {"label": "Boom 300",   "class": "spike", "template": "spike", "avg_interval": 300,  "spike_dir": +1},
    "CRASH300N": {"label": "Crash 300",  "class": "spike", "template": "spike", "avg_interval": 300,  "spike_dir": -1},
    "BOOM50":    {"label": "Boom 50",    "class": "spike", "template": "spike", "avg_interval": 50,   "spike_dir": +1},
    "CRASH50":   {"label": "Crash 50",   "class": "spike", "template": "spike", "avg_interval": 50,   "spike_dir": -1},
    "BOOM600":   {"label": "Boom 600",   "class": "spike", "template": "spike", "avg_interval": 600,  "spike_dir": +1},
    "CRASH600":  {"label": "Crash 600",  "class": "spike", "template": "spike", "avg_interval": 600,  "spike_dir": -1},
    "BOOM900":   {"label": "Boom 900",   "class": "spike", "template": "spike", "avg_interval": 900,  "spike_dir": +1},
    "CRASH900":  {"label": "Crash 900",  "class": "spike", "template": "spike", "avg_interval": 900,  "spike_dir": -1},

    # ---- Volatility indices (observe-only for now; continuous movement,
    #      NOT rare spikes — the spike template does NOT fit these) ----
    #      Real Deriv API symbol names (underlying_symbol) confirmed via the
    #      public active_symbols endpoint: Volatility 100/25/... (1s) Index.
    #      Display names: "Volatility 100 (1s) Index", etc.
    "1HZ10V":  {"label": "Volatility 10", "class": "volatility", "template": "volatility", "avg_interval": 1, "spike_dir": 0},
    "1HZ15V":  {"label": "Volatility 15", "class": "volatility", "template": "volatility", "avg_interval": 1, "spike_dir": 0},
    "1HZ25V":  {"label": "Volatility 25", "class": "volatility", "template": "volatility", "avg_interval": 1, "spike_dir": 0},
    "1HZ30V":  {"label": "Volatility 30", "class": "volatility", "template": "volatility", "avg_interval": 1, "spike_dir": 0},
    "1HZ50V":  {"label": "Volatility 50", "class": "volatility", "template": "volatility", "avg_interval": 1, "spike_dir": 0},
    "1HZ75V":  {"label": "Volatility 75", "class": "volatility", "template": "volatility", "avg_interval": 1, "spike_dir": 0},
    "1HZ90V":  {"label": "Volatility 90", "class": "volatility", "template": "volatility", "avg_interval": 1, "spike_dir": 0},
    "1HZ100V": {"label": "Volatility 100", "class": "volatility", "template": "volatility", "avg_interval": 1, "spike_dir": 0},
}

SYMBOL_TEMPLATE = {k: v.get("template", "spike") for k, v in SYMBOL_META.items()}
SYMBOL_CLASS = {k: v.get("class", "spike") for k, v in SYMBOL_META.items()}

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
    "stake_usd": 1.0,             # margin per trade (works on a $5 account)
    "multiplier": 500,            # x500 = max profit per $1 when the EV gate passes
    "max_daily_loss_usd": 20.0,   # absolute-dollar halt (safety net only)
    "max_consecutive_losses": 0,  # 0 = off — the brain's gates manage risk
    "pause_after_losses_min": 30,
    "daily_loss_pct": 20.0,       # % of balance — drawdown breaker
    "stop_loss_pct": 60.0,        # (forced to 0 below — no per-trade stop)
    "take_profit_pct": 100.0,     # (forced to 0 below — trailing ride)
    "max_hold_ticks": 0,          # 0 = auto (derived from symbol interval)
    "entry_threshold": 0.50,      # min model confidence to enter (spike mode)
    "exit_threshold": 2.0,        # pre-spike exit: bail when spike risk is 2x baseline
    "auto_min_trades": 15,        # paper trades per mode before auto picks a side
    "auto_switch_margin": 0.20,   # switch modes only if other side is 20% better
    "warmup_ticks": 4000,         # historical ticks fetched at startup
    "min_balance_usd": 1.0,       # never trade below this balance
    # staggered boot: how many symbols to warm + start trading immediately
    # (the rest warm in the background with pauses so the free host never
    # saturates). Tunable via the settings page or DERIV_* env.
    "warmup_front_batch": 2,      # symbols started immediately on boot
    "warmup_concurrent": 2,        # max concurrent history downloads during warm-up
    "study_ticks": 20000,          # ticks mass-studied per symbol (direct from Deriv)
}

# Locked-in profit settings, re-applied on EVERY boot so the user never
# has to touch the dashboard: $1 per trade, x500 leverage (maximum profit
# per $1 whenever the EV gate approves), 1:1 payoff exits.
FORCED_SETTINGS: dict = {
    "stake_usd": 1.0,              # $1 drift candles; works on a $5 account
    "multiplier": 500,
    "take_profit_pct": 0.0,        # 0 = TRAILING profit ride (milks the drift)
    "stop_loss_pct": 0.0,          # 0 = NO stop-loss
    "daily_loss_pct": 20.0,        # stop trading for the day at -20% of balance
    "entry_threshold": 0.50,       # selective, high-conviction entries
    "exit_threshold": 2.0,         # pre-spike exit: bail at 2x baseline risk
    "max_consecutive_losses": 0,   # 0 = off — brain's gates manage risk
    "symbols": list(DEFAULT_SYMBOLS),
}
