"""
Spike labeler — turns raw ticks into labeled spike events.

Mechanic (verified against real Deriv data):
  * Between spikes, Boom/Crash drift in small ticks (median move ~ pip scale).
  * A spike is a single tick many times larger than the rolling median move.

The threshold adapts automatically: 6x the rolling median of |tick move|
(with a small absolute floor). No hardcoded point values per symbol, so it
works on all 14 Boom/Crash symbols and survives regime changes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass
class SpikeEvent:
    epoch: float
    price: float
    prev_price: float
    size: float
    direction: str      # UP | DOWN
    age_ticks: int      # ticks since previous spike (incl. this one)


class SpikeLabeler:
    def __init__(self, pip: float = 0.001, window: int = 200,
                 multiplier: float = 6.0, min_floor_pips: float = 5.0,
                 cooldown: int = 1):
        self.pip = pip
        self.multiplier = multiplier
        self.min_floor = min_floor_pips * pip
        self.window = window
        self.cooldown = cooldown
        self._abs_moves: deque[float] = deque(maxlen=window)
        self.last_price: float | None = None
        self.age = 0                    # ticks since last spike
        self.total_spikes = 0
        self._cool = 0
        self.threshold = 0.0

    def reset(self) -> None:
        self._abs_moves.clear()
        self.last_price = None
        self.age = 0
        self.total_spikes = 0
        self._cool = 0
        self.threshold = 0.0

    def update(self, price: float, epoch: float) -> SpikeEvent | None:
        """Feed one tick; returns a SpikeEvent when this tick IS a spike."""
        if self.last_price is None:
            self.last_price = price
            self.age = 0
            return None

        move = price - self.last_price
        self._abs_moves.append(abs(move))
        self.last_price = price
        self.age += 1

        # warm-up: no reliable median yet — never emit (noise would trip the floor)
        if len(self._abs_moves) < 20:
            return None

        median = float(np.median(self._abs_moves))
        self.threshold = max(self.multiplier * median, self.min_floor, 1e-12)

        if self._cool > 0:
            self._cool -= 1
            return None

        if abs(move) >= self.threshold:
            event = SpikeEvent(
                epoch=epoch,
                price=price,
                prev_price=price - move,
                size=abs(move),
                direction="UP" if move > 0 else "DOWN",
                age_ticks=self.age,
            )
            self.total_spikes += 1
            self.age = 0
            self._cool = self.cooldown
            return event
        return None

    @property
    def median_move(self) -> float:
        return float(np.median(self._abs_moves)) if self._abs_moves else 0.0
