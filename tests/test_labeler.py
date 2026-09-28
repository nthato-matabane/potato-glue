"""Spike labeler tests on synthetic Boom-like and Crash-like series."""

import random

from app.engine.labeler import SpikeLabeler


def _series_with_spikes(n=3000, every=200, spike_size=5.0, drift=-0.01, seed=1):
    rng = random.Random(seed)
    price, out = 10000.0, []
    for i in range(n):
        if i > 0 and i % every == 0:
            price += spike_size                      # UP spike (boom)
        else:
            price += drift + rng.uniform(-0.005, 0.005)
        out.append((float(i), price))
    return out


def test_detects_up_spikes_and_resets_age():
    lab = SpikeLabeler(pip=0.001)
    events = []
    for epoch, px in _series_with_spikes():
        ev = lab.update(px, epoch)
        if ev:
            events.append(ev)
    assert len(events) >= 13                       # 3000/200 = 15 expected
    assert all(e.direction == "UP" for e in events)
    assert lab.total_spikes == len(events)
    # ages between events should be close to the periodic spacing
    ages = [e.age_ticks for e in events]
    assert 150 <= sum(ages) / len(ages) <= 250


def test_crash_series_gives_down_spikes():
    lab = SpikeLabeler(pip=0.001)
    directions = []
    price = 10000.0
    rng = random.Random(3)
    for i in range(3000):
        price += -4.0 if i % 150 == 0 and i else 0.01 + rng.uniform(-0.004, 0.004)
        ev = lab.update(price, float(i))
        if ev:
            directions.append(ev.direction)
    assert directions and all(d == "DOWN" for d in directions)


def test_threshold_adapts_to_scale():
    # same pattern at 100x price scale -> still detected, no hardcoded points
    lab_small = SpikeLabeler(pip=0.001)
    lab_big = SpikeLabeler(pip=0.1)
    small = _series_with_spikes(spike_size=5.0, seed=5)
    big = [(e, p * 100) for e, p in small]
    n_small = sum(1 for e, p in small if lab_small.update(p, e))
    n_big = sum(1 for e, p in big if lab_big.update(p, e))
    assert n_small > 5
    assert abs(n_small - n_big) <= 3


def test_flat_market_no_false_spikes():
    lab = SpikeLabeler(pip=0.001)
    count = 0
    price = 1000.0
    rng = random.Random(9)
    for i in range(2000):
        price += rng.uniform(-0.001, 0.001)
        if lab.update(price, float(i)):
            count += 1
    assert count == 0
