"""
Feature extraction — a compact, scale-free view of the tick stream.

All features are normalized (roughly -2..2) so one logistic model works
across every Boom/Crash symbol without per-symbol tuning:

  0 age_norm          ticks since spike / mean interval   (cap 4)
  1 p_fast            P(spike within 3 ticks | age)      (hazard)
  2 p_slow            P(spike within 15 ticks | age)     (hazard)
  3 age_percentile    how deep into historical waits     (0..1)
  4 slope50           recent drift vs median tick move   (signed)
  5 vol_ratio         short-term vol / baseline vol      (~1.0 = calm)
  6 cv50              coeff. variation of last 50 |move| (compression)
  7 streak_norm       consecutive same-direction ticks   (-1..1)
  8 net20             net move last 20 ticks / (20*med)  (-1..1)
  9 hour_sin          UTC hour of day                    (seasonality probe)
 10 hour_cos
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

FEATURE_NAMES = ["age_norm", "p_fast", "p_slow", "age_percentile", "slope50",
                 "vol_ratio", "cv50", "streak_norm", "net20", "hour_sin", "hour_cos"]
D = len(FEATURE_NAMES)


class FeatureState:
    def __init__(self, mean_interval: float, spike_dir: int = +1):
        self.spike_dir = spike_dir          # +1 boom (up spikes), -1 crash
        self.mean_interval = max(float(mean_interval), 1.0)
        self.moves: deque[float] = deque(maxlen=200)
        self.pips_moves: deque[float] = deque(maxlen=60)  # recent raw moves
        self.last_price: float | None = None
        self.streak = 0                     # consecutive same-direction ticks
        self.last_sign = 0

    def update(self, price: float) -> None:
        if self.last_price is not None:
            move = price - self.last_price
            self.moves.append(move)
            self.pips_moves.append(move)
            sign = 1 if move > 0 else (-1 if move < 0 else self.last_sign)
            if sign and sign == self.last_sign:
                self.streak += 1
            else:
                self.streak = 1 if sign else 0
            self.last_sign = sign or self.last_sign
        self.last_price = price

    @property
    def median_move(self) -> float:
        if len(self.moves) < 20:
            return 1e-9
        return max(float(np.median(np.abs(np.asarray(self.moves)))), 1e-12)

    def build(self, age: int, p_fast: float, p_slow: float,
              age_pct: float, epoch: float) -> np.ndarray:
        med = self.median_move
        arr = np.asarray(self.moves, dtype=float)

        if len(arr) >= 50:
            slope50 = float(np.mean(arr[-50:])) / med
            short = float(np.mean(np.abs(arr[-50:])))
            long_ = float(np.mean(np.abs(arr))) if len(arr) >= 150 else short
            vol_ratio = short / max(long_, 1e-12)
            cv50 = float(np.std(np.abs(arr[-50:]))) / max(short, 1e-12)
        else:
            slope50 = vol_ratio = cv50 = 0.0

        recent20 = arr[-20:] if len(arr) >= 20 else arr
        net20 = float(np.sum(recent20)) / (med * max(len(recent20), 1))

        hour = (epoch % 86400) / 3600.0 if epoch else 0.0

        return np.array([
            min(age / self.mean_interval, 4.0),
            p_fast,
            p_slow,
            age_pct,
            np.clip(slope50, -3, 3),
            np.clip(vol_ratio, 0, 4),
            np.clip(cv50, 0, 5),
            np.clip(self.streak / 30.0, -1, 1) * (1 if self.spike_dir < 0 else -1),
            np.clip(net20, -2, 2),
            math.sin(2 * math.pi * hour / 24.0),
            math.cos(2 * math.pi * hour / 24.0),
        ], dtype=float)
