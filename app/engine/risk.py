"""
Risk manager — protects the account while the model is still learning.

State is derived from the trades table (survives restarts) plus a
persisted pause timestamp, so a cloud redeploy doesn't reset circuit
breakers mid-drawdown.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from .. import store

MIN_STAKE = 1.0
MAX_STAKE = 500.0


class RiskManager:
    def __init__(self, settings: dict):
        self.settings = settings
        self._daily_start_balance: float | None = None
        self._today = self._utc_day()
        self._recalc()

    @staticmethod
    def _utc_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def update_settings(self, settings: dict) -> None:
        self.settings = settings

    # ---- state derived from persisted trades ------------------------------

    def _recalc(self) -> None:
        today = self._utc_day()
        live = store.get_trades(limit=500, live=True)
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        self.daily_trades = [t for t in live if t["ts"] >= day_start]
        self.daily_profit = float(sum(t["profit"] for t in self.daily_trades))
        # consecutive losses at the tail of live history
        streak = 0
        for t in live:
            if t["profit"] <= 0:
                streak += 1
            else:
                break
        self.consecutive_losses = streak
        self._today = today

    def refresh(self) -> None:
        if self._utc_day() != self._today:
            self._daily_start_balance = None
        self._recalc()

    # ---- gates -------------------------------------------------------------

    def can_trade_live(self, balance: float, live_enabled: bool) -> tuple[bool, str]:
        if not live_enabled:
            return False, "paper mode"
        if not balance:
            return False, "no live balance"
        if balance < float(self.settings.get("min_balance_usd", 2.0)):
            return False, f"balance {balance:.2f} below minimum"

        paused_until = float(store.load_settings().get("paused_until", 0) or 0)
        if paused_until > time.time():
            mins = int((paused_until - time.time()) / 60) + 1
            return False, f"paused after losses ({mins}m left)"

        self.refresh()
        max_loss = float(self.settings.get("max_daily_loss_usd", 20.0))
        if self.daily_profit <= -max_loss:
            return False, f"daily loss limit hit ({self.daily_profit:.2f})"

        daily_pct_cap = float(self.settings.get("daily_loss_pct", 10.0))
        if balance > 0 and self.daily_profit < 0:
            if abs(self.daily_profit) / balance * 100.0 >= daily_pct_cap:
                return False, f"daily % loss limit hit ({self.daily_profit:.2f})"

        if self.consecutive_losses >= int(self.settings.get("max_consecutive_losses", 5)):
            return False, f"{self.consecutive_losses} consecutive losses"
        return True, "ok"

    def record_live(self, profit: float) -> None:
        self.refresh()
        if profit <= 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= int(self.settings.get("max_consecutive_losses", 5)):
                mins = int(self.settings.get("pause_after_losses_min", 30))
                store.save_setting("paused_until",
                                   time.time() + mins * 60)
        else:
            self.consecutive_losses = 0

    # ---- sizing ------------------------------------------------------------

    def stake_for(self, balance: float) -> float:
        s = self.settings
        stake = float(s.get("stake_usd", 5.0))
        stake = max(MIN_STAKE, min(stake, MAX_STAKE))
        if balance:
            # never risk more than 5% of balance on one position
            stake = min(stake, max(MIN_STAKE, balance * 0.05))
            if balance - stake < float(s.get("min_balance_usd", 2.0)):
                return 0.0
        return round(stake, 2)
