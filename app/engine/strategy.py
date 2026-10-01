"""
Strategy — pure decision functions (unit-testable, no I/O).

Two evidence-based modes on every Boom/Crash symbol:

  DRIFT ("exit just before the spike")
      Boom  -> MULTDOWN (short the slow down-drift)
      Crash -> MULTUP   (long  the slow up-drift)
      Enter while the model says a spike is NOT imminent, exit the moment
      spike risk rises above exit_threshold — i.e. just before the spike.

  SPIKE ("catch the spike")
      Boom  -> MULTUP   (long into the up-spike)
      Crash -> MULTDOWN (short into the down-spike)
      Enter when the learned spike probability lifts well above baseline,
      exit the tick the spike lands (or on risk stops).

AUTO picks the side per symbol from measured paper expectancy — with
hysteresis so it can't flip-flop — which is how the agent "learns which
side pays" instead of being told.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

# Paper commission model: observed on live proposals as ~0.009% of notional
# (stake x multiplier) per side. Used for paper fills and expectancy math.
PAPER_COMMISSION_RATE = 0.00009

# Trailing take-profit for drift rides: once the open profit crosses the
# arm level, give back at most this fraction of the PEAK before exiting.
# 0.35 means a peak of +100% exits at +65%; a peak of +1000% exits at +650%.
# This is what lets a $1 drift ride make $10+ instead of capping at a
# fixed target — the position follows the drift until just before the spike.
DRIFT_TRAIL_GIVEBACK = 0.35
DRIFT_TRAIL_ARM_PCT = 40.0     # profit % the ride must reach before trailing arms


# ---------------------------------------------------------------------------
# expected-value gate — never take a trade the measured maths can't pay for
# ---------------------------------------------------------------------------

def round_trip_comm_pct(multiplier: int) -> float:
    """Commission as % of stake for entry+exit (rate x mult x 2 x 100)."""
    return PAPER_COMMISSION_RATE * multiplier * 2 * 100.0


def entry_ev(mode: str, p_spike_in_hold: float, multiplier: int,
             avg_spike_pct: float, drift_per_tick_pct: float,
             expected_hold: int, exit_threshold: float) -> tuple[bool, float, str]:
    """
    Estimated edge of an entry, in % of stake, using the brain's OWN
    measured statistics (average spike size, drift speed, learned spike
    probability) and real commission costs.

    SPIKE mode: win when the spike lands inside the planned hold; otherwise
    pay commission + drift bleed.
    DRIFT mode: collect drift for the expected safe window; pay dearly if
    the spike lands against us first.

    Entries require EV above an estimation buffer (not merely > 0): with
    noisy early statistics a +0.1% model EV is indistinguishable from zero,
    and realized expectancy came in about 2% below naive model estimates.
    """
    mult = max(int(multiplier), 1)
    comm = round_trip_comm_pct(mult)
    margin = max(0.5, 0.4 * comm)        # edge must clear estimation error

    if mode == "spike":
        win = mult * avg_spike_pct * 100.0 - comm        # catch the spike
        loss = comm + mult * abs(drift_per_tick_pct) * 100.0 * min(expected_hold, 30)
        p = min(max(p_spike_in_hold, 0.0), 1.0)
        ev = p * win - (1.0 - p) * max(loss, 0.05)
        ok = ev >= margin and win > 0                    # win must beat commission
        return ok, ev, f"EV={ev:.2f}% (win {win:.1f}% @ p={p*100:.1f}%, loss {loss:.1f}%)"

    # drift: capture = drift speed x time until spike risk forces exit.
    # Risk is priced honestly: the MEASURED probability a spike lands
    # somewhere in the window x the full spike cost (that's what a
    # spike_hit actually costs — verified against paper outcomes).
    capture = mult * abs(drift_per_tick_pct) * 100.0 * max(expected_hold, 1)
    spike_cost = mult * avg_spike_pct * 100.0             # adverse spike = full size
    p_hit = min(max(p_spike_in_hold, 0.0), 1.0)
    ev = capture - comm - p_hit * spike_cost
    ok = ev >= margin and capture > comm                 # drift must beat commission
    return ok, ev, (f"EV={ev:.2f}% (capture {capture:.1f}% vs comm {comm:.1f}% "
                    f"+ p_hit {p_hit*100:.0f}% x {spike_cost:.1f}%)")


@dataclass
class Decision:
    action: str          # enter_drift | enter_spike | exit | hold
    confidence: float
    reason: str


# ---------------------------------------------------------------------------
# probability helpers
# ---------------------------------------------------------------------------

def base_prob(horizon: int, mean_interval: float) -> float:
    """Baseline P(spike within `horizon` ticks) under uniform arrival."""
    return min(horizon / max(mean_interval, 1.0), 1.0)


def lift(p: float, horizon: int, mean_interval: float) -> float:
    """Learned probability relative to baseline (>1 = more likely than usual)."""
    b = base_prob(horizon, mean_interval)
    if b <= 1e-9:
        return 1.0
    return p / b


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------

def entry_drift(features: dict, p_fast: float, exit_threshold: float,
                mean_interval: float, vol_ratio: float,
                threshold: float = 0.30,
                p_slow: float | None = None) -> Decision:
    """Enter a drift trade only when a spike looks safely far away.

    RUNWAY rule (learned from 50k-tick studies): only enter in the FRESH
    part of the spike cycle. A position opened mid-cycle gets surprised
    by early spikes — the single biggest loss source. age_percentile is
    0 right after a spike and 1 as the next one approaches; runway
    declines linearly to zero at 50% of the typical interval.

    p_slow (optional): the 15-tick learned probability. The slow horizon
    is where the model's real skill lives (spikes announce themselves
    ~15 ticks out on most cycles), so BOTH horizons must agree the coast
    is clear — checking only the 3-tick window let spikes that were
    already loading hit freshly-opened positions.
    """
    lf = lift(p_fast, 3, mean_interval)
    ls = lift(p_slow, 15, mean_interval) if p_slow is not None else lf
    # hard block: EITHER horizon screaming => not safe (exit_threshold is
    # a LIFT multiple, e.g. 2.0 = spike risk twice the baseline)
    if exit_threshold > 1.0 and (lf >= exit_threshold or ls >= exit_threshold):
        return Decision("hold", 0.0,
                        f"spike risk high (lift fast={lf:.2f} slow={ls:.2f})")
    safe = 1.0 - min(max(lf, ls) / 2.0, 1.0)   # worst horizon sets safety
    slope = features.get("slope50", 0.0)
    # drift direction: boom drifts DOWN (slope<0), crash drifts UP (slope>0)
    aligned = min(abs(slope) / 0.5, 1.0) if slope * features.get("drift_sign", 1) > 0 else 0.0
    vol_ok = 1.0 if vol_ratio <= 2.0 else max(0.0, 1.0 - (vol_ratio - 2.0))
    age_pct = features.get("age_percentile", 0.0)
    runway = max(0.0, 1.0 - age_pct / 0.5)   # fresh cycle = full runway
    conf = safe * aligned * vol_ok * runway
    if conf >= threshold:
        return Decision("enter_drift", round(conf, 3),
                        f"drift aligned, fresh cycle (runway={runway:.2f}, "
                        f"lift fast={lf:.2f} slow={ls:.2f})")
    return Decision("hold", round(conf, 3), "drift conditions not met")


def entry_spike(p_fast: float, p_slow: float, mean_interval: float,
                threshold: float = 1e-9) -> Decision:
    """Enter a spike trade when learned odds clearly beat baseline."""
    lf = lift(p_fast, 3, mean_interval)
    ls = lift(p_slow, 15, mean_interval)
    blend = 0.5 * lf + 0.5 * ls
    conf = max(0.0, min((blend - 1.0) / 3.0, 1.0))
    if conf >= threshold:
        return Decision("enter_spike", round(conf, 3),
                        f"spike lift fast={lf:.2f} slow={ls:.2f}")
    return Decision("hold", round(conf, 3), "no spike edge")


# ---------------------------------------------------------------------------
# exits — the money-multiplying rule set
# ---------------------------------------------------------------------------

def should_exit(pos_mode: str, *, spike_now: bool, pnl_pct: float,
                p_fast: float, mean_interval: float, ticks_held: int,
                max_hold: int, exit_threshold: float,
                stop_loss_pct: float, take_profit_pct: float,
                lift_fast: float,
                pnl_peak_pct: float = 0.0,
                lift_slow: float | None = None) -> tuple[bool, str, str]:
    """
    Returns (exit?, reason, urgency) where urgency is
    'now' (sell immediately) or 'next' (normal close).

    stop_loss_pct <= 0 disables the stop-loss entirely (user preference:
    entries are only taken when the EV gate proves spike risk is priced —
    the account-level 20% drawdown breaker is the real protection).

    take_profit_pct <= 0 disables the fixed profit cap for drift rides:
    instead a TRAILING exit follows the drift — once the open profit peaks
    above DRIFT_TRAIL_ARM_PCT, the position only exits when it has given
    back DRIFT_TRAIL_GIVEBACK of that peak (or when spike risk says go).
    A $1 drift ride can therefore bank 5x, 10x, 20x its stake when the
    drift runs long, instead of settling for a fraction.

    pnl_peak_pct: highest open profit % seen since entry (caller tracks).
    """
    if pos_mode == "spike":
        if spike_now:
            return True, "spike_caught", "now"
        if stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct:
            return True, "stop_loss", "now"
        if pnl_pct >= take_profit_pct:
            return True, "take_profit", "now"
        # bail out when the predicted spike failed to materialise —
        # but not so early that we cut winners before they can land
        if ticks_held >= max_hold:
            return True, "max_hold", "next"
        if ticks_held > 20 and lift_fast < 0.5:
            return True, "edge_faded", "next"
        return False, "", ""

    # drift position: getting out BEFORE the spike is the whole point
    if spike_now:
        return True, "spike_hit", "now"          # late — minimise damage
    if stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct:
        return True, "stop_loss", "now"
    # pre-spike exit: exit_threshold is a LIFT multiple (spike risk N× the
    # baseline). A raw probability can never reach 0.35 on a 3-tick window
    # (base rate ~0.006), which silently disabled this rule forever.
    # The slow horizon fires ~15 ticks before the fast one on most cycles,
    # so either horizon tripping the line is reason to bail.
    if exit_threshold > 1.0 and \
            (lift_fast >= exit_threshold or
             (lift_slow is not None and lift_slow >= exit_threshold)):
        return True, "pre_spike_exit", "now"     # <-- the core rule
    # profit taking: fixed cap (legacy, tp>0) or trailing ride (tp<=0)
    if take_profit_pct > 0:
        if pnl_pct >= take_profit_pct:
            return True, "take_profit", "now"
    else:
        if pnl_peak_pct >= DRIFT_TRAIL_ARM_PCT and \
                pnl_pct <= pnl_peak_pct * (1.0 - DRIFT_TRAIL_GIVEBACK):
            return True, "trail_exit", "now"     # milked the move, bank it
    if ticks_held >= max_hold:
        return True, "max_hold", "next"
    return False, "", ""
# ---------------------------------------------------------------------------
# AUTO mode — pick the side that actually pays
# ---------------------------------------------------------------------------

def select_auto_mode(profits_drift: list[float], profits_spike: list[float],
                     settings: dict, current: str) -> tuple[str, str]:
    """
    Returns (mode, reason). mode is 'drift' | 'spike' | 'observe'.

    Each side is judged INDEPENDENTLY on its own recent expectancy: a side
    starts trading as soon as IT has enough samples and they are net
    positive — it must not wait for the other side to mature too (spike
    entries are rare by nature, so pairing them stalls a paying side).
    Switching between two eligible sides uses net expectancy with a margin.
    """
    min_n = int(settings.get("auto_min_trades", 15))
    margin = float(settings.get("auto_switch_margin", 0.20))
    min_start = max(6, min_n // 2)           # samples to activate a side

    d = list(profits_drift)[-min_n:]
    s = list(profits_spike)[-min_n:]
    d_mean = mean(d) if d else 0.0
    s_mean = mean(s) if s else 0.0
    d_ok = len(d) >= min_start and d_mean > 0
    s_ok = len(s) >= min_start and s_mean > 0

    if d_ok and s_ok:
        if current in ("drift", "spike"):
            cur, other = (d_mean, s_mean) if current == "drift" else (s_mean, d_mean)
            if other > cur * (1.0 + margin) and other > 0:
                new = "spike" if current == "drift" else "drift"
                return new, f"switching: {new} pays better ({other:.4f} vs {cur:.4f})"
            return current, f"holding {current} ({cur:.4f} vs {other:.4f})"
        pick = "drift" if d_mean >= s_mean else "spike"
        return pick, f"selected {pick} (drift {d_mean:.4f} vs spike {s_mean:.4f})"
    if d_ok:
        return "drift", f"drift pays (avg {d_mean:+.4f}/trade over {len(d)})"
    if s_ok:
        return "spike", f"spike pays (avg {s_mean:+.4f}/trade over {len(s)})"
    return "observe", (f"gathering data (drift {len(d)}/{min_start}, "
                       f"spike {len(s)}/{min_start})")


def multiplier_for(symbol_side: str) -> str:
    """Contract type for a direction."""
    return "MULTUP" if symbol_side == "UP" else "MULTDOWN"
