"""
Online self-learning predictor.

One model per symbol per horizon (fast = spike within 3 ticks, slow =
within 15). Pure numpy online logistic regression with:

  * RMSProp per-coordinate scaling — adapts to feature scales without
    AdaGrad's ever-growing denominators that stall long-running agents
  * class-balanced gradients (spikes are rare) with an exact calibration
    correction so probabilities stay honest
  * experience replay: recent labeled samples are periodically re-trained,
      which is what turns the first hours of ticks into a usable model
  * rolling Brier skill tracking vs the base-rate baseline
  * full serialization (weights + optimizer + counts) so learning survives
    process restarts / cloud redeploys

No training pipelines, no GPU, no heavyweight ML deps — it updates in
microseconds per tick, which is what "runs on its own, 24/7" requires.
"""

from __future__ import annotations

from collections import deque

import numpy as np


def _sigmoid(z: np.ndarray | float):
    z = np.clip(z, -30, 30)
    return 1.0 / (1.0 + np.exp(-z))


class OnlineSpikeModel:
    def __init__(self, d: int, horizon: int, lr: float = 0.09,
                 l2: float = 1e-4, pos_clip: tuple[float, float] = (1.0, 50.0),
                 replay_size: int = 4000, replay_every: int = 250,
                 replay_batch: int = 1500, seed: int = 7):
        self.d = d
        self.horizon = horizon
        self.lr = lr
        self.l2 = l2
        self.pos_clip = pos_clip
        self.w = np.zeros(d + 1, dtype=float)       # last coord = bias
        self.g2 = np.zeros(d + 1, dtype=float)      # AdaGrad accumulators
        self.pos_w = 50.0                            # smoothed positive count
        self.neg_w = 500.0                           # smoothed negative count
        self.replay: deque[tuple[np.ndarray, int]] = deque(maxlen=replay_size)
        self.replay_every = replay_every
        self.replay_batch = replay_batch
        self.rng = np.random.default_rng(seed)
        self.n_updates = 0
        self.n_pos = 0
        self.n_neg = 0
        # rolling Brier tracking (last 500 predictions)
        self._brier: deque[float] = deque(maxlen=500)
        self._brier_base: deque[float] = deque(maxlen=500)

    # ---- inference --------------------------------------------------------

    def _calib(self, p_raw: float) -> float:
        """Invert class-weighted training so output is a true probability."""
        pw = self.pos_weight
        if pw <= 1.0:
            return p_raw
        # weighted training converges to odds_weighted = pw * odds_true
        odds = p_raw / max(1.0 - p_raw, 1e-12) / pw
        return odds / (1.0 + odds)

    @property
    def pos_weight(self) -> float:
        return float(np.clip(self.neg_w / max(self.pos_w, 1.0),
                             self.pos_clip[0], self.pos_clip[1]))

    def predict(self, x: np.ndarray) -> float:
        z = float(np.dot(self.w[:-1], x) + self.w[-1])
        return float(self._calib(_sigmoid(z)))

    # ---- learning ---------------------------------------------------------

    def _grad_step(self, x: np.ndarray, y: int, lr: float) -> None:
        xb = np.concatenate([x, [1.0]])
        p = _sigmoid(float(np.dot(self.w, xb)))
        wgt = self.pos_weight if y == 1 else 1.0
        err = (p - y) * wgt
        g = err * xb + self.l2 * self.w
        # RMSProp: exponential decay keeps step sizes healthy over days of uptime
        self.g2 = 0.99 * self.g2 + 0.01 * g * g
        self.w -= lr * g / (np.sqrt(self.g2) + 1e-8)

    def observe(self, x: np.ndarray, y: int, replay: bool = True) -> None:
        """Train on one labeled sample; y=1 means a spike lands within horizon."""
        self.replay.append((x.copy(), int(y)))
        self._grad_step(x, y, self.lr)
        self.n_updates += 1
        # decayed class counts (drives pos_weight + calibration)
        self.pos_w = 0.997 * self.pos_w + y
        self.neg_w = 0.997 * self.neg_w + (1 - y)
        if y:
            self.n_pos += 1
        else:
            self.n_neg += 1
        if replay and self.replay_every and self.n_updates % self.replay_every == 0:
            self.run_replay()

    def run_replay(self, passes: int = 1) -> int:
        """Re-train on recent experience (this is where generalization comes from)."""
        if len(self.replay) < 100:
            return 0
        n = min(self.replay_batch, len(self.replay))
        idx = self.rng.choice(len(self.replay), size=n, replace=False)
        count = 0
        for _ in range(passes):
            for i in idx:
                x, y = self.replay[int(i)]
                self._grad_step(x, y, self.lr * 0.5)
                count += 1
        return count

    # ---- metrics ----------------------------------------------------------

    def track(self, x: np.ndarray, y: int) -> None:
        """Rolling proper-score tracking (call with held-out/live outcomes)."""
        p = self.predict(x)
        base = (self.pos_w / max(self.pos_w + self.neg_w, 1.0))
        self._brier.append((p - y) ** 2)
        self._brier_base.append((base - y) ** 2)

    @property
    def brier_skill(self) -> float:
        """1.0 = perfect, 0 = no better than always guessing base rate."""
        if len(self._brier) < 50:
            return 0.0
        mb = float(np.mean(self._brier))
        bb = float(np.mean(self._brier_base))
        if bb <= 1e-12:
            return 0.0
        return float(np.clip(1.0 - mb / bb, -2.0, 1.0))

    @property
    def base_rate(self) -> float:
        return float(self.pos_w / max(self.pos_w + self.neg_w, 1e-9))

    def stats(self) -> dict:
        return {
            "horizon": self.horizon,
            "updates": self.n_updates,
            "pos_samples": self.n_pos,
            "neg_samples": self.n_neg,
            "pos_weight": round(self.pos_weight, 2),
            "base_rate": round(self.base_rate, 5),
            "brier_skill": round(self.brier_skill, 3),
            "replay_size": len(self.replay),
            "weight_norm": round(float(np.linalg.norm(self.w)), 3),
        }

    # ---- persistence ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "d": self.d,
            "horizon": self.horizon,
            "lr": self.lr,
            "l2": self.l2,
            "w": self.w.tolist(),
            "g2": self.g2.tolist(),
            "pos_w": self.pos_w,
            "neg_w": self.neg_w,
            "n_updates": self.n_updates,
            "n_pos": self.n_pos,
            "n_neg": self.n_neg,
        }

    @classmethod
    def from_dict(cls, blob: dict) -> "OnlineSpikeModel":
        m = cls(d=int(blob["d"]), horizon=int(blob["horizon"]),
                lr=float(blob.get("lr", 0.09)), l2=float(blob.get("l2", 1e-4)))
        w = np.asarray(blob["w"], dtype=float)
        if len(w) == m.d + 1:
            m.w = w
        g2 = np.asarray(blob.get("g2", []), dtype=float)
        if len(g2) == m.d + 1:
            m.g2 = g2
        m.pos_w = float(blob.get("pos_w", 50.0))
        m.neg_w = float(blob.get("neg_w", 500.0))
        m.n_updates = int(blob.get("n_updates", 0))
        m.n_pos = int(blob.get("n_pos", 0))
        m.n_neg = int(blob.get("n_neg", 0))
        return m
