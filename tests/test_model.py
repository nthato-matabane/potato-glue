"""Online model tests: does it actually learn a real signal?"""

import numpy as np

from app.engine.model import OnlineSpikeModel


def _synthetic(n=6000, seed=11):
    """Spike probability strongly driven by feature 0 (age-like), noise elsewhere."""
    rng = np.random.default_rng(seed)
    samples = []
    for _ in range(n):
        x = rng.normal(0, 1, size=9)
        x[0] = rng.uniform(0, 4)
        p = 1.0 / (1.0 + np.exp(-1.8 * (x[0] - 2.4)))   # logistic in x[0]
        y = 1 if rng.random() < p else 0
        samples.append((x, y))
    return samples


def test_learns_separable_signal():
    m = OnlineSpikeModel(d=9, horizon=3, seed=3)
    data = _synthetic()
    for x, y in data[:4500]:
        m.observe(x, y, replay=False)
    m.run_replay(passes=6)

    held = data[4500:]
    for x, y in held:
        m.track(x, y)                          # held-out score tracking
    pos = [m.predict(x) for x, y in held if y == 1]
    neg = [m.predict(x) for x, y in held if y == 0]
    assert pos and neg
    assert np.mean(pos) > 2.5 * np.mean(neg)     # separates classes
    assert m.n_pos + m.n_neg == 4500
    assert m.stats()["brier_skill"] > 0.15       # beats base-rate guessing


def test_probabilities_are_calibrated_ish():
    m = OnlineSpikeModel(d=9, horizon=3, seed=5)
    data = _synthetic(n=9000)
    for x, y in data[:8000]:
        m.observe(x, y, replay=False)
    m.run_replay(passes=2)
    held = data[8000:]
    preds = np.array([m.predict(x) for x, _ in held])
    ys = np.array([y for _, y in held])
    # unweighted-mean prediction should be in the same ballpark as true rate
    base = ys.mean()
    assert abs(preds.mean() - base) < max(0.35 * base, 0.05)
    assert preds.min() >= 0 and preds.max() <= 1


def test_serialization_roundtrip():
    m = OnlineSpikeModel(d=9, horizon=15)
    for x, y in _synthetic(seed=7)[:2000]:
        m.observe(x, y, replay=False)
    blob = m.to_dict()
    m2 = OnlineSpikeModel.from_dict(blob)
    x = np.zeros(9)
    x[0] = 3.0
    assert abs(m.predict(x) - m2.predict(x)) < 1e-9
    assert m2.n_updates == m.n_updates
    assert m2.pos_weight == m.pos_weight


def test_no_signal_model_stays_uncertain():
    """Random labels: skill should stay ~0 (it must not hallucinate edge)."""
    rng = np.random.default_rng(2)
    m = OnlineSpikeModel(d=9, horizon=3, seed=2)
    for _ in range(3000):
        x = rng.normal(0, 1, size=9)
        y = int(rng.random() < 0.02)
        m.observe(x, y, replay=False)
    assert m.brier_skill < 0.15
