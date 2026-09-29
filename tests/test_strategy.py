"""Strategy decision tests — the rules that protect capital."""

from app.engine import strategy as strat


MEAN = 1000.0


def test_base_prob_and_lift():
    assert abs(strat.base_prob(15, 1000) - 0.015) < 1e-9
    assert abs(strat.lift(0.03, 15, 1000) - 2.0) < 1e-9
    assert strat.lift(0.0, 15, 1000) == 0.0


def test_drift_entry_requires_aligned_slope():
    aligned = {"slope50": -1.2, "drift_sign": -1, "age_percentile": 0.3,
               "vol_ratio": 1.0}
    wrong_way = {"slope50": +1.2, "drift_sign": -1, "age_percentile": 0.3,
                 "vol_ratio": 1.0}
    d1 = strat.entry_drift(aligned, p_fast=0.001, exit_threshold=0.35,
                           mean_interval=MEAN, vol_ratio=1.0)
    d2 = strat.entry_drift(wrong_way, p_fast=0.001, exit_threshold=0.35,
                           mean_interval=MEAN, vol_ratio=1.0)
    assert d1.action == "enter_drift"
    assert d1.confidence > 0.3
    assert d2.action == "hold"


def test_drift_entry_blocked_when_spike_looming():
    ok = {"slope50": -1.0, "drift_sign": -1, "age_percentile": 0.2}
    d = strat.entry_drift(ok, p_fast=0.05, exit_threshold=0.35,
                          mean_interval=MEAN, vol_ratio=1.0)
    assert d.action == "hold"          # lift ~ 6.6 -> no safety


def test_spike_entry_needs_lift():
    d_low = strat.entry_spike(0.001, 0.01, MEAN)        # ~baseline
    d_high = strat.entry_spike(0.02, 0.08, MEAN)        # 25x/8x baseline
    assert d_low.action == "hold"
    assert d_high.action == "enter_spike"
    assert 0 < d_high.confidence <= 1


def test_pre_spike_exit_fires_first_for_drift_position():
    # exit_threshold is a LIFT multiple: bail when spike risk is 2x baseline
    exit_now, reason, urg = strat.should_exit(
        "drift", spike_now=False, pnl_pct=1.0, p_fast=0.40,
        mean_interval=MEAN, ticks_held=50, max_hold=800,
        exit_threshold=2.0, stop_loss_pct=40, take_profit_pct=60,
        lift_fast=2.5)
    assert exit_now and reason == "pre_spike_exit" and urg == "now"


def test_spike_position_exits_on_spike():
    exit_now, reason, urg = strat.should_exit(
        "spike", spike_now=True, pnl_pct=35.0, p_fast=0.9,
        mean_interval=MEAN, ticks_held=70, max_hold=200,
        exit_threshold=0.35, stop_loss_pct=40, take_profit_pct=60,
        lift_fast=9.0)
    assert exit_now and reason == "spike_caught" and urg == "now"


def test_stop_loss_and_take_profit():
    exit_now, reason, _ = strat.should_exit(
        "spike", spike_now=False, pnl_pct=-45, p_fast=0.0,
        mean_interval=MEAN, ticks_held=5, max_hold=200,
        exit_threshold=0.35, stop_loss_pct=40, take_profit_pct=60,
        lift_fast=0.5)
    assert exit_now and reason == "stop_loss"
    exit_now, reason, _ = strat.should_exit(
        "drift", spike_now=False, pnl_pct=+65, p_fast=0.01,
        mean_interval=MEAN, ticks_held=5, max_hold=800,
        exit_threshold=0.35, stop_loss_pct=40, take_profit_pct=60,
        lift_fast=0.5)
    assert exit_now and reason == "take_profit"


def test_max_hold_exits_eventually():
    exit_now, reason, _ = strat.should_exit(
        "drift", spike_now=False, pnl_pct=0.5, p_fast=0.02,
        mean_interval=MEAN, ticks_held=801, max_hold=800,
        exit_threshold=0.35, stop_loss_pct=40, take_profit_pct=60,
        lift_fast=0.8)
    assert exit_now and reason == "max_hold"


def test_no_premature_exit():
    exit_now, _, _ = strat.should_exit(
        "drift", spike_now=False, pnl_pct=2.0, p_fast=0.01,
        mean_interval=MEAN, ticks_held=100, max_hold=800,
        exit_threshold=0.35, stop_loss_pct=40, take_profit_pct=60,
        lift_fast=0.6)
    assert not exit_now


def test_auto_mode_gathers_then_selects():
    settings = {"auto_min_trades": 10, "auto_switch_margin": 0.2}
    # insufficient data
    m, _ = strat.select_auto_mode([0.1] * 4, [0.2] * 4, settings, "observe")
    assert m == "observe"
    # clear winner
    d = [0.05] * 12
    s = [0.5] * 12
    m, _ = strat.select_auto_mode(d, s, settings, "observe")
    assert m == "spike"
    # hysteresis: incumbent stays unless challenger is 20% better
    m, _ = strat.select_auto_mode([0.4] * 12, [0.45] * 12, settings, "drift")
    assert m == "drift"
    m, _ = strat.select_auto_mode([0.4] * 12, [0.6] * 12, settings, "drift")
    assert m == "spike"
    # both negative -> observe
    m, _ = strat.select_auto_mode([-0.4] * 12, [-0.6] * 12, settings, "drift")
    assert m == "observe"


def test_multiplier_mapping():
    assert strat.multiplier_for("UP") == "MULTUP"
    assert strat.multiplier_for("DOWN") == "MULTDOWN"


def test_ev_gate_spike_needs_real_odds():
    # a big spike (0.11%) at x100 beats 1.8% round-trip commission easily...
    ok, ev, _ = strat.entry_ev("spike", p_spike_in_hold=0.30, multiplier=100,
                               avg_spike_pct=0.0011, drift_per_tick_pct=2e-6,
                               expected_hold=45, exit_threshold=0.35)
    assert ok and ev > 0
    # ...but only when the model's odds justify it
    ok, ev, _ = strat.entry_ev("spike", p_spike_in_hold=0.04, multiplier=100,
                               avg_spike_pct=0.0011, drift_per_tick_pct=2e-6,
                               expected_hold=45, exit_threshold=0.35)
    assert not ok and ev < 0


def test_ev_gate_rejects_structurally_untradeable_symbol():
    # tiny spikes (0.008%) can never pay a round trip at any realistic mult
    ok, ev, _ = strat.entry_ev("spike", p_spike_in_hold=0.25, multiplier=100,
                               avg_spike_pct=0.00008, drift_per_tick_pct=7e-7,
                               expected_hold=45, exit_threshold=0.35)
    assert not ok and ev <= 0


def test_ev_gate_drift_needs_capture_to_beat_commission():
    # fast drift, long safe window -> tradeable
    ok, ev, _ = strat.entry_ev("drift", p_spike_in_hold=0.2, multiplier=100,
                               avg_spike_pct=0.0011, drift_per_tick_pct=3e-6,
                               expected_hold=300, exit_threshold=0.35)
    assert ok and ev > 0
    # crawl-slow drift over a short window can't pay 1.8% round trip
    ok, ev, _ = strat.entry_ev("drift", p_spike_in_hold=0.2, multiplier=100,
                               avg_spike_pct=0.0011, drift_per_tick_pct=3e-6,
                               expected_hold=40, exit_threshold=0.35)
    assert not ok
