"""Hazard estimator tests."""

import random

from app.engine.hazard import HazardEstimator


def test_uniform_intervals_give_flat_hazard():
    h = HazardEstimator(avg_interval=100)
    for i in range(1, 101):
        h.observe_interval(100)              # perfectly regular arrivals
    # mid-wait the conditional probability of "within 3" is small,
    # and higher near the end of the interval
    p_mid = h.prob_within(50, 3)
    p_late = h.prob_within(97, 3)
    assert p_mid <= p_late + 0.05
    assert p_late > 0.3


def test_prob_within_grows_with_k():
    h = HazardEstimator(avg_interval=200)
    rng = random.Random(4)
    for _ in range(300):
        h.observe_interval(max(1, int(rng.gauss(200, 60))))
    p3 = h.prob_within(100, 3)
    p15 = h.prob_within(100, 15)
    p60 = h.prob_within(100, 60)
    assert p3 <= p15 <= p60


def test_percentile_bounds_and_growth():
    h = HazardEstimator(avg_interval=120)
    for _ in range(100):
        h.observe_interval(120)
    assert h.percentile(0) <= 0.2
    assert h.percentile(1000) == 1.0
    assert h.percentile(110) < h.percentile(121)   # all intervals == 120


def test_prior_without_data():
    h = HazardEstimator(avg_interval=500)
    assert not h.enough_data
    p = h.prob_within(0, 15)
    assert 0 < p < 0.2                 # ~3% for exponential mean 500
    snap = h.snapshot(0)
    assert snap["observations"] == 0


def test_regime_shift_flag():
    h = HazardEstimator(avg_interval=100)
    for _ in range(60):
        h.observe_interval(100)
    assert not h.regime_shift
    for _ in range(20):
        h.observe_interval(300)        # recent mean jumps
    assert h.regime_shift
