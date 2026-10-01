"""Tests for the learning-visibility + timing-memory fixes."""

import copy

from app import config, store
from app.engine import strategy as strat
from app.engine.agent import SymbolBrain, blend_weight


# ---- the learned model keeps its voice -------------------------------------

def test_blend_weight_full_trust_for_proven_model():
    assert blend_weight(20_000, 0.78) == 1.0     # skilled model -> full voice
    assert blend_weight(20_000, 0.30) == 1.0     # at the proven bar
    assert blend_weight(20_000, 0.15) < 0.85     # unproven -> prior still leads
    assert blend_weight(0, 0.99) == 0.0          # no updates -> no voice


def test_blend_weight_never_exceeds_one():
    assert blend_weight(99_999, 1.0) <= 1.0
    assert blend_weight(400, 0.5) < 1.0          # still ramping in


# ---- spike-timing memory survives restarts ---------------------------------

def _brain(symbol: str = "BOOM500") -> SymbolBrain:
    s = copy.deepcopy(config.DEFAULT_SETTINGS)
    for k, v in config.FORCED_SETTINGS.items():
        s[k] = copy.deepcopy(v)
    return SymbolBrain(symbol, 0.001, s)


def test_hazard_roundtrip(tmp_path):
    store.init(tmp_path / "mem.db")
    b = _brain()
    for t in (410, 392, 455, 501, 388, 470, 512, 433, 460, 447, 490):
        b.hazard.observe_interval(t)
    b.avg_spike_pct = 0.0055
    b.drift_per_tick_pct = 3.2e-6
    b.save_hazard()

    b2 = _brain()
    assert b2.load_hazard() is True
    assert len(b2.hazard.intervals) == 11
    assert abs(b2.hazard.mean_interval - b.hazard.mean_interval) < 1e-9
    assert abs(b2.avg_spike_pct - 0.0055) < 1e-12
    assert b2.hazard.entry_ready          # memory restored -> ready to trade


def test_hazard_load_without_snapshot(tmp_path):
    store.init(tmp_path / "mem2.db")
    assert _brain().load_hazard() is False


# ---- the drift gate listens to BOTH horizons -------------------------------

FEATS = {"slope50": -1.0, "drift_sign": -1, "age_percentile": 0.1, "vol_ratio": 1.0}
MEAN = 500.0


def test_drift_entry_blocks_when_slow_horizon_screams():
    # fast horizon calm (lift 1.0) but slow horizon at 4x baseline
    d = strat.entry_drift(FEATS, p_fast=0.006, exit_threshold=2.0,
                          mean_interval=MEAN, vol_ratio=1.0,
                          p_slow=0.048)
    assert d.action == "hold"


def test_drift_entry_allows_when_both_horizons_calm():
    d = strat.entry_drift(FEATS, p_fast=0.006, exit_threshold=2.0,
                          mean_interval=MEAN, vol_ratio=1.0,
                          p_slow=0.015)
    assert d.action == "enter_drift"


def test_drift_entry_backward_compatible_without_slow():
    d = strat.entry_drift(FEATS, p_fast=0.006, exit_threshold=2.0,
                          mean_interval=MEAN, vol_ratio=1.0)
    assert d.action == "enter_drift"


def test_pre_spike_exit_fires_on_slow_lift():
    exit_now, reason, _ = strat.should_exit(
        "drift", spike_now=False, pnl_pct=1.0, p_fast=0.006,
        mean_interval=MEAN, ticks_held=50, max_hold=400,
        exit_threshold=2.0, stop_loss_pct=0, take_profit_pct=0,
        lift_fast=1.0, lift_slow=2.5)          # only the slow horizon trips
    assert exit_now and reason == "pre_spike_exit"


def test_pre_spike_exit_ignores_slow_when_calm():
    exit_now, _, _ = strat.should_exit(
        "drift", spike_now=False, pnl_pct=1.0, p_fast=0.006,
        mean_interval=MEAN, ticks_held=50, max_hold=400,
        exit_threshold=2.0, stop_loss_pct=0, take_profit_pct=0,
        lift_fast=1.0, lift_slow=1.2)
    assert not exit_now
