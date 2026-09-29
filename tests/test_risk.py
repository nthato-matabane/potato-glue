"""Risk manager tests (uses a temp SQLite file)."""

import time

from app import store
from app.engine.risk import RiskManager

BASE = {
    "live_enabled": True,
    "max_daily_loss_usd": 10.0,
    "max_consecutive_losses": 3,
    "pause_after_losses_min": 30,
    "daily_loss_pct": 50.0,
    "stake_usd": 5.0,
    "min_balance_usd": 2.0,
}


def _fresh(tmp_path, **over):
    store.init(tmp_path / "risk_test.db")
    s = {**BASE, **over}
    return RiskManager(s)


def test_paper_mode_blocks(tmp_path):
    rm = _fresh(tmp_path, live_enabled=False)
    ok, why = rm.can_trade_live(1000.0, False)
    assert not ok and "paper" in why


def test_daily_loss_limit(tmp_path):
    rm = _fresh(tmp_path)
    ok, _ = rm.can_trade_live(1000.0, True)
    assert ok
    store.record_trade("BOOM1000", "spike", "UP", True, 5, 100,
                       100.0, 101.0, -15.0, 0.1, 10, "stop_loss", 985.0)
    rm.refresh()
    ok, why = rm.can_trade_live(985.0, True)
    assert not ok and "daily loss" in why


def test_consecutive_loss_pause_persists(tmp_path):
    rm = _fresh(tmp_path)
    for _ in range(3):
        store.record_trade("CRASH500", "spike", "DOWN", True, 5, 100,
                           100.0, 99.0, -2.0, 0.1, 8, "stop_loss", 998.0)
        time.sleep(0.01)
    rm.refresh()
    ok, why = rm.can_trade_live(998.0, True)
    assert not ok
    # pause flag persisted -> even a fresh manager blocks
    rm2 = RiskManager(dict(BASE))
    ok, why = rm2.can_trade_live(998.0, True)
    assert not ok and ("pause" in why or "consecutive" in why)


def test_stake_sizing(tmp_path):
    rm = _fresh(tmp_path)
    assert rm.stake_for(1000.0) == 5.0            # configured stake
    assert rm.stake_for(5.0) == 5.0               # works on a $5 account
    assert rm.stake_for(2.0) == 0.0               # would break min balance
    big = _fresh(tmp_path, stake_usd=5000.0)
    assert big.stake_for(10_000.0) == 500.0       # exchange max


def test_low_balance_gate(tmp_path):
    rm = _fresh(tmp_path)
    ok, why = rm.can_trade_live(1.5, True)
    assert not ok and "minimum" in why
