"""
Hazard estimator — the statistical core of spike timing.

Models the inter-spike interval distribution online (rolling window of the
last N observed intervals) and answers:

  * P(spike within next k ticks | already waited `age` ticks)
    = 1 - S(age+k)/S(age)      where S = empirical survival function
  * percentile(age): how deep into the historical distribution the current
    wait already is (0..1)
  * mean/median interval, recent-vs-longterm drift for regime alerts

Because Deriv documents spikes as "average once every N ticks" the arrival
may be close to memoryless — the estimator measures the real hazard curve
instead of assuming it, and the strategy only takes trades the measured
curve justifies.
"""

from __future__ import annotations

from collections import deque

import numpy as np


class HazardEstimator:
    def __init__(self, avg_interval: int = 500, window: int = 500):
        self.prior_mean = float(avg_interval)
        self.window = window
        self.intervals: deque[int] = deque(maxlen=window)
        self.mean_interval = float(avg_interval)
        self.median_interval = float(avg_interval)
        self.recent_mean = float(avg_interval)
        self.total_spikes = 0
        # last-known probabilities (updated once per tick by the caller)
        self._last: dict[str, float] = {}

    # ---- training ---------------------------------------------------------

    def observe_interval(self, ticks: int) -> None:
        """Record the number of ticks between two spikes."""
        if ticks <= 0:
            return
        self.intervals.append(int(ticks))
        self.total_spikes += 1
        arr = np.asarray(self.intervals, dtype=float)
        self.mean_interval = float(arr.mean())
        self.median_interval = float(np.median(arr))
        tail = arr[-min(50, len(arr)):]
        self.recent_mean = float(tail.mean())

    @property
    def enough_data(self) -> bool:
        return len(self.intervals) >= 10

    @property
    def entry_ready(self) -> bool:
        """Enough intervals observed for EV/entry decisions to be trustworthy."""
        return len(self.intervals) >= 15

    @property
    def regime_shift(self) -> bool:
        """True when the recent interval mean deviates sharply from the
        earlier history (tail excluded from the baseline so a real shift
        isn't diluted by its own samples)."""
        if len(self.intervals) < 30:
            return False
        arr = np.asarray(self.intervals, dtype=float)
        tail = arr[-min(20, len(arr) // 2):]
        base = arr[: len(arr) - len(tail)]
        if len(base) < 20:
            return False
        return abs(float(tail.mean()) - float(base.mean())) > 0.35 * max(float(base.mean()), 1e-9)

    # ---- queries ----------------------------------------------------------

    def _survival(self, a: float) -> float:
        """S(a) = P(interval > a) — empirical CDF blended with an exponential
        prior. With few observed spikes the raw empirical CDF jumps wildly
        (one long interval makes every age look 'overdue'); the blend keeps
        early estimates honest and converges to empirical as n grows."""
        prior = float(np.exp(-a / max(self.prior_mean, 1.0)))
        if not self.intervals:
            return prior
        arr = np.asarray(self.intervals, dtype=float)
        gt = float(np.count_nonzero(arr > a))
        empirical = (gt + 0.5) / (len(arr) + 1.0)
        w = len(arr) / (len(arr) + 30.0)     # 0 early → →1 with data
        return w * empirical + (1.0 - w) * prior

    def prob_within(self, age: float, k: float) -> float:
        """P(spike in (age, age+k] | survived `age` ticks so far)."""
        if age < 0:
            age = 0.0
        if not self.intervals:
            # exponential prior: 1 - exp(-k/mean) regardless of age
            return 1.0 - float(np.exp(-k / max(self.prior_mean, 1.0)))
        s_age = self._survival(age)
        s_next = self._survival(age + k)
        if s_age <= 1e-9:
            return 1.0
        p = 1.0 - (s_next / s_age)
        return float(min(max(p, 0.0), 1.0))

    def percentile(self, age: float) -> float:
        """Fraction of historical intervals that were <= age."""
        if not self.intervals:
            return float(min(age / max(self.prior_mean, 1.0), 1.0))
        arr = np.asarray(self.intervals, dtype=float)
        return float(np.count_nonzero(arr <= age) / len(arr))

    def snapshot(self, age: int) -> dict:
        snap = {
            "mean_interval": round(self.mean_interval, 1),
            "median_interval": round(self.median_interval, 1),
            "recent_mean": round(self.recent_mean, 1),
            "observations": len(self.intervals),
            "regime_shift": self.regime_shift,
            "age": age,
            "age_percentile": round(self.percentile(age), 3),
            "p_next_3": round(self.prob_within(age, 3), 4),
            "p_next_15": round(self.prob_within(age, 15), 4),
        }
        self._last = snap
        return snap
