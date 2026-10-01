"""
The self-learning trading agent.

SymbolBrain — per symbol:
  ticks -> spike labeler -> hazard estimator -> features -> two online
  models (fast/slow horizons) -> strategy decisions -> paper books always,
  live orders only when enabled + gated by risk.

AgentHub — process-wide:
  owns the Deriv channels, warm-up, subscriptions, persistence, balance,
  portfolio adoption and the status snapshot served to the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .. import config, store
from ..deriv import auth as auth_mod
from ..deriv import client as dclient
from ..deriv.client import Channel
from . import risk as risk_mod
from . import strategy as strat
from .features import D, FeatureState
from .hazard import HazardEstimator
from .labeler import SpikeEvent, SpikeLabeler
from .model import OnlineSpikeModel

logger = logging.getLogger("spike.agent")

H_FAST = 3
H_SLOW = 15
TRADING_FEE_RATE = strat.PAPER_COMMISSION_RATE


def blend_weight(n_updates: int, brier_skill: float) -> float:
    """How much say the learned model gets vs the statistical prior (0..1).

    Ramps in over the first ~800 labeled updates, then scales with the
    model's demonstrated skill (Brier). A PROVEN model (skill >= ~0.3)
    keeps the leading voice: capping it below ~0.75 rounds its timing
    back to the baseline at every decision gate, which looked like
    "the agent never learns". Weak models keep the prior in charge.
    """
    skill = min(max(brier_skill, 0.0) / 0.3, 1.0)
    return min(n_updates / 800.0, 1.0) * (0.55 + 0.45 * skill)


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

@dataclass
class PaperPosition:
    mode: str                 # drift | spike
    side: str                 # UP | DOWN
    stake: float
    multiplier: int
    entry_px: float
    entry_idx: int
    entry_epoch: float
    commission: float          # entry commission (usd)
    pnl_peak_pct: float = 0.0  # highest open profit % since entry (trailing TP)

    def pnl_usd(self, px: float, exit_commission: bool = True) -> float:
        if self.side == "UP":
            ret = (px / self.entry_px) - 1.0
        else:
            ret = 1.0 - (px / self.entry_px)
        gross = self.stake * self.multiplier * ret
        fee = self.commission * (2 if exit_commission else 1)
        return gross - fee

    def pnl_pct(self, px: float) -> float:
        p = self.pnl_usd(px, exit_commission=False)
        return (p / max(self.stake, 1e-9)) * 100.0


@dataclass
class LivePosition:
    mode: str
    side: str                 # UP | DOWN
    contract_type: str        # MULTUP | MULTDOWN
    contract_id: int
    stake: float
    multiplier: int
    entry_px: float
    entry_idx: int
    entry_epoch: float
    commission: float = 0.0
    last_poc: dict = field(default_factory=dict)
    pnl_peak_pct: float = 0.0  # highest open profit % since entry (trailing TP)

    @property
    def profit(self) -> float:
        return float(self.last_poc.get("profit", 0.0) or 0.0)

    @property
    def sell_price(self) -> float:
        return float(self.last_poc.get("sell_price", 0.0) or 0.0)

    @property
    def is_valid_to_sell(self) -> bool:
        return bool(self.last_poc.get("is_valid_to_sell", True))


# ---------------------------------------------------------------------------
# pending-label pipeline (assigns y = spike within H ticks, H ticks later)
# ---------------------------------------------------------------------------

class _LabelPipe:
    def __init__(self, model: OnlineSpikeModel, horizon: int):
        self.model = model
        self.horizon = horizon
        self.pending: deque[tuple[int, np.ndarray]] = deque()

    def push(self, idx: int, x: np.ndarray) -> None:
        self.pending.append((idx, x))

    def settle(self, idx_now: int, spiked: bool) -> int:
        """Consume finished samples; returns how many were learned from."""
        H = self.horizon
        learned = 0
        if spiked:
            while self.pending and self.pending[0][0] < idx_now - H:
                _, x = self.pending.popleft()
                self.model.track(x, 0)
                self.model.observe(x, 0)
                learned += 1
            while self.pending:
                _, x = self.pending.popleft()
                self.model.track(x, 1)
                self.model.observe(x, 1)
                learned += 1
        else:
            while self.pending and self.pending[0][0] <= idx_now - H:
                _, x = self.pending.popleft()
                self.model.track(x, 0)
                self.model.observe(x, 0)
                learned += 1
        return learned


# ---------------------------------------------------------------------------
# per-symbol brain
# ---------------------------------------------------------------------------

class SymbolBrain:
    def __init__(self, symbol: str, pip_size: float, settings: dict):
        meta = config.SYMBOL_META.get(symbol, {"label": symbol, "avg_interval": 500,
                                               "spike_dir": +1})
        self.symbol = symbol
        self.label = meta["label"]
        self.avg_interval = int(meta["avg_interval"])
        self.spike_dir = int(meta["spike_dir"])      # +1 up spikes, -1 down spikes
        self.pip = pip_size
        self.settings = settings

        self.labeler = SpikeLabeler(pip=pip_size)
        self.hazard = HazardEstimator(self.avg_interval)
        self.feats = FeatureState(self.avg_interval, self.spike_dir)
        self.fast = OnlineSpikeModel(D, H_FAST)
        self.slow = OnlineSpikeModel(D, H_SLOW)
        self.pipe_fast = _LabelPipe(self.fast, H_FAST)
        self.pipe_slow = _LabelPipe(self.slow, H_SLOW)

        self.tick_idx = 0
        self.last_price: float = 0.0
        self.last_epoch: float = 0.0
        self.recent: deque[tuple[float, float]] = deque(maxlen=8000)  # (epoch, px)

        self.paper: dict[str, Optional[PaperPosition]] = {"drift": None, "spike": None}
        self.paper_profits: dict[str, deque[float]] = {
            "drift": deque(maxlen=400), "spike": deque(maxlen=400)}
        self.cooldown_until: dict[str, int] = {"drift": -1, "spike": -1}
        self.auto_mode = "observe"
        self.auto_reason = "starting"

        self.live: Optional[LivePosition] = None

        # events feed for the dashboard
        self.last_signal: dict = {}
        self.p_fast: float = 0.0
        self.p_slow: float = 0.0
        self.flip_intent: Optional[str] = None   # spike↔drift flip (one tick)
        self.p_model: tuple[float, float] = (0.0, 0.0)
        # cost/economics model — measured live from this symbol's own ticks
        self.avg_spike_pct: float = 0.001        # EWMA of spike size / price
        self.drift_per_tick_pct: float = 1e-6    # EWMA of non-spike |move| / price
        self.last_spike_pct: float = 0.001       # size of the most recent spike
        self.counters = {"spikes": 0, "paper_trades": 0, "live_trades": 0}

    # ---- configuration derived from learned stats -------------------------

    @property
    def mean_interval(self) -> float:
        return self.hazard.mean_interval if self.hazard.enough_data else float(self.avg_interval)

    @property
    def drift_sign(self) -> int:
        return -1 if self.spike_dir > 0 else +1     # boom drifts down, crash drifts up

    @property
    def contract_side_for_drift(self) -> str:
        return "DOWN" if self.spike_dir > 0 else "UP"

    @property
    def contract_side_for_spike(self) -> str:
        return "UP" if self.spike_dir > 0 else "DOWN"

    def max_hold_for(self, mode: str) -> int:
        custom = int(self.settings.get("max_hold_ticks", 0) or 0)
        if custom > 0:
            return custom
        if mode == "drift":
            return max(30, int(0.8 * self.mean_interval))
        return max(20, min(int(0.25 * self.mean_interval), 120))

    def update_settings(self, settings: dict) -> None:
        self.settings = settings

    # ---- hazard persistence ----------------------------------------------

    def hazard_to_dict(self) -> dict:
        """Serialize the spike-timing statistics (the hazard estimator)."""
        return {
            "intervals": list(self.hazard.intervals),
            "total_spikes": self.hazard.total_spikes,
            "avg_spike_pct": self.avg_spike_pct,
            "drift_per_tick_pct": self.drift_per_tick_pct,
            "last_spike_pct": self.last_spike_pct,
        }

    def load_hazard(self) -> bool:
        """Restore spike-timing stats from the store (True when restored)."""
        blob = store.load_json(f"hazard:{self.symbol}")
        if not isinstance(blob, dict):
            return False
        try:
            iv = [int(t) for t in blob.get("intervals", []) if int(t) > 0]
            self.hazard.intervals.clear()
            for t in iv:                 # observe_interval appends + recomputes means
                self.hazard.observe_interval(t)
            self.hazard.total_spikes = int(blob.get("total_spikes", 0) or 0)
            self.avg_spike_pct = float(blob.get("avg_spike_pct", 0.001) or 0.001)
            self.drift_per_tick_pct = float(blob.get("drift_per_tick_pct", 1e-6) or 1e-6)
            self.last_spike_pct = float(blob.get("last_spike_pct", 0.001) or 0.001)
            return True
        except Exception as e:
            logger.warning("%s: hazard restore failed: %s", self.symbol, e)
            return False

    def save_hazard(self) -> None:
        store.save_json(f"hazard:{self.symbol}", self.hazard_to_dict())

    def load_models(self) -> None:
        for key, model in (("fast", self.fast), ("slow", self.slow)):
            blob = store.load_model(f"{self.symbol}:{key}")
            if blob:
                try:
                    loaded = OnlineSpikeModel.from_dict(blob)
                    if loaded.d == model.d and loaded.horizon == model.horizon:
                        setattr(self, key, loaded)
                        # keep pipes pointing at the loaded instances
                        if key == "fast":
                            self.pipe_fast.model = self.fast
                        else:
                            self.pipe_slow.model = self.slow
                except Exception as e:
                    logger.warning("%s: model load failed: %s", self.symbol, e)

    def save_models(self) -> None:
        store.save_model(f"{self.symbol}:fast", self.fast.to_dict())
        store.save_model(f"{self.symbol}:slow", self.slow.to_dict())

    # ---- core pipeline ----------------------------------------------------

    def warm(self, ticks: list[tuple[float, float]]) -> None:
        """Feed history through the full pipeline (learning + paper trading).

        Synchronous — fine for tools/backtests. The server uses warm_async.
        """
        for epoch, price in ticks:
            self._step(price, epoch)

    async def warm_async(self, ticks: list[tuple[float, float]],
                         chunk: int = 800) -> None:
        """Same as warm(), but yields to the event loop every `chunk` ticks.

        On tiny free hosts (0.1 CPU) a synchronous 4,000-tick warm-up
        blocks the web server long enough for the platform health check
        to fail and the container to restart — a crash loop. Chunking
        keeps health checks green while learning happens.
        """
        for i in range(0, len(ticks), chunk):
            for epoch, price in ticks[i:i + chunk]:
                self._step(price, epoch)
            await asyncio.sleep(0)

    async def on_tick(self, price: float, epoch: float, hub: "AgentHub") -> None:
        await self._step_async(price, epoch, hub)

    async def _step_async(self, price: float, epoch: float, hub: "AgentHub") -> None:
        self._step(price, epoch)                    # learn + paper first
        # live actions after local state is updated
        await self._live_manage(hub)

    def _step(self, price: float, epoch: float) -> Optional[SpikeEvent]:
        self.tick_idx += 1
        t = self.tick_idx
        prev_price = self.last_price
        self.last_price, self.last_epoch = price, epoch
        self.recent.append((epoch, price))

        event = self.labeler.update(price, epoch)
        spiked = event is not None
        # measured economics: how big are spikes, how fast is the drift
        if event and price:
            a = 0.15
            self.avg_spike_pct = (1 - a) * self.avg_spike_pct + a * (event.size / price)
            self.last_spike_pct = event.size / price
        elif prev_price and price:
            m = abs(price - prev_price) / price
            a = 0.02
            self.drift_per_tick_pct = (1 - a) * self.drift_per_tick_pct + a * m
        if event:
            spiked = True
            self.counters["spikes"] += 1
            self.hazard.observe_interval(event.age_ticks)
            store.record_spike(self.symbol, event.epoch, event.age_ticks,
                               event.size, event.direction, price)
            # reset streak bookkeeping on regime-changing events
        self.feats.update(price)

        age = self.labeler.age
        snap = self.hazard.snapshot(age)
        h_fast = snap["p_next_3"]
        h_slow = snap["p_next_15"]
        age_pct = snap["age_percentile"]

        x = self.feats.build(age, h_fast, h_slow, age_pct, epoch)
        self.pipe_fast.settle(t, spiked)
        self.pipe_slow.settle(t, spiked)

        # learned probabilities, blended with the statistical hazard prior.
        # Weight scales with demonstrated skill, then TRUSTS the model:
        # a skillful model (Brier skill >= ~0.2 on held-out-style rolling
        # scores) gets the leading voice; the prior only fills the gaps and
        # covers the model's early, unproven ticks. Capping a skillful
        # model at 70% made learned timing arrive at every gate rounded
        # down to the baseline — the visible symptom was "it never learns".
        m_fast = self.fast.predict(x)
        m_slow = self.slow.predict(x)
        wf = blend_weight(self.fast.n_updates, self.fast.brier_skill)
        ws = blend_weight(self.slow.n_updates, self.slow.brier_skill)
        p_fast = wf * m_fast + (1.0 - wf) * h_fast
        p_slow = ws * m_slow + (1.0 - ws) * h_slow
        self.p_fast, self.p_slow = p_fast, p_slow
        self.p_model = (m_fast, m_slow)

        self.pipe_fast.push(t, x)
        self.pipe_slow.push(t, x)

        self._paper_manage(price, epoch, spiked, p_fast, p_slow, snap, x)

        self.last_signal = {
            "symbol": self.symbol, "age": age,
            "p_fast": round(p_fast, 5), "p_slow": round(p_slow, 5),
            "model_fast": round(m_fast, 5), "model_slow": round(m_slow, 5),
            "age_pct": round(age_pct, 3),
            "mean_interval": round(self.mean_interval, 1),
            "auto_mode": self.auto_mode,
        }
        return event

    # ---- paper trading ----------------------------------------------------

    def _feature_dict(self) -> dict:
        arr = self.feats.build(self.labeler.age, 0.0, 0.0, 0.0, self.last_epoch)
        return {
            "slope50": float(arr[4]),
            "vol_ratio": float(arr[5]),
            "age_percentile": float(self.hazard.percentile(self.labeler.age)),
            "drift_sign": self.drift_sign,
        }

    def _ev_check(self, mode: str, exit_threshold: float) -> tuple[bool, float, str]:
        """Expected-value gate using this symbol's measured economics."""
        age = self.labeler.age
        hold = self.max_hold_for(mode)
        if mode == "spike":
            # realistic catch window: fade/max-hold exits bound the hold, so
            # price the probability over the window we actually sit in
            p = self.hazard.prob_within(age, min(hold, 45))
            expected_hold = min(hold, 45)
        else:
            # safe window: how many ticks until spike risk forces the exit
            window = 10
            step = max(5, int(self.mean_interval / 40))
            for k in range(step, int(self.mean_interval) + step, step):
                if self.hazard.prob_within(age + k, 3) >= exit_threshold:
                    window = k
                    break
                window = k
            p = self.hazard.prob_within(age, window)
            # the position's max_hold caps how long we can actually ride
            expected_hold = min(window, self.max_hold_for(mode))
            p = self.hazard.prob_within(age, expected_hold)
        return strat.entry_ev(
            mode=mode, p_spike_in_hold=p, multiplier=int(self.settings.get("multiplier", 100)),
            avg_spike_pct=self.avg_spike_pct,
            drift_per_tick_pct=self.drift_per_tick_pct,
            expected_hold=expected_hold, exit_threshold=exit_threshold)

    def _paper_manage(self, price: float, epoch: float, spiked: bool,
                      p_fast: float, p_slow: float, snap: dict, x: np.ndarray) -> None:
        settings = self.settings
        self.flip_intent = None                  # flip intent lives one tick
        sl = float(settings.get("stop_loss_pct", 40.0))
        tp = float(settings.get("take_profit_pct", 60.0))
        exit_th = float(settings.get("exit_threshold", 0.35))
        lf = strat.lift(p_fast, H_FAST, self.mean_interval)
        ls = strat.lift(p_slow, H_SLOW, self.mean_interval)

        # ---- exits ----
        # cross-mode flip signals: computed ONCE per tick, they drive BOTH
        # books. A spike entry signal is an EXIT signal for the drift ride
        # (and vice versa) — the flip frees the margin for the spike leg.
        fdict = self._feature_dict()
        thr = float(settings.get("entry_threshold", 0.50))
        ready = self.hazard.entry_ready
        spike_dec = (strat.entry_spike(p_fast, p_slow, self.mean_interval,
                                       threshold=thr) if ready
                     else strat.Decision("hold", 0.0, "warming"))
        drift_dec = (strat.entry_drift(fdict, p_fast, exit_th,
                                       self.mean_interval, fdict["vol_ratio"],
                                       threshold=thr, p_slow=p_slow) if ready
                     else strat.Decision("hold", 0.0, "warming"))
        for mode, pos in list(self.paper.items()):
            if not pos:
                continue
            peak = max(pos.pnl_peak_pct, pos.pnl_pct(price))
            pos.pnl_peak_pct = peak
            exit_now, reason, urgency = strat.should_exit(
                mode,
                spike_now=spiked,
                pnl_pct=pos.pnl_pct(price),
                p_fast=p_fast,
                mean_interval=self.mean_interval,
                ticks_held=self.tick_idx - pos.entry_idx,
                max_hold=self.max_hold_for(mode),
                exit_threshold=exit_th,
                stop_loss_pct=sl,
                take_profit_pct=tp,
                lift_fast=lf,
                pnl_peak_pct=peak,
                lift_slow=ls,
            )
            # spike-mode outcomes depend on which way the spike went
            if mode == "spike" and spiked:
                ours = ((pos.side == "UP" and self.spike_dir > 0) or
                        (pos.side == "DOWN" and self.spike_dir < 0))
                exit_now, reason, urgency = (True, "spike_caught", "now") if ours \
                    else (True, "spike_against", "now")
            # THE FLIP: the other book's entry signal closes this book —
            # and frees the slot for that leg in the same tick
            if not exit_now and ready:
                if mode == "drift" and spike_dec.action.startswith("enter"):
                    if self._ev_check("spike", exit_th)[0]:
                        exit_now, reason, urgency = True, "flip_to_spike", "now"
                        self.flip_intent = "spike"
                elif mode == "spike" and drift_dec.action.startswith("enter"):
                    if self._ev_check("drift", exit_th)[0]:
                        exit_now, reason, urgency = True, "flip_to_drift", "now"
                        self.flip_intent = "drift"
            if exit_now:
                self._close_paper(mode, pos, price, reason)

        # ---- entries ----
        if not self.hazard.entry_ready:
            return                              # not enough spikes observed yet
        for mode in ("drift", "spike"):
            if self.paper[mode] is not None:
                continue
            flipping = (self.flip_intent == mode)
            if not flipping and self.tick_idx < self.cooldown_until[mode]:
                continue
            dec = spike_dec if mode == "spike" else drift_dec
            if dec.action.startswith("enter"):
                ok_ev, ev_val, ev_why = self._ev_check(mode, exit_th)
                if not ok_ev:
                    if self.tick_idx % 500 == 0:
                        store.record_signal(self.symbol, self.labeler.age,
                                            round(p_fast, 5), round(p_slow, 5), 0,
                                            f"ev_block_{mode}", round(ev_val, 3))
                    continue
                stake = float(self.settings.get("stake_usd", 5.0))
                mult = int(self.settings.get("multiplier", 100))
                side = (self.contract_side_for_drift if mode == "drift"
                        else self.contract_side_for_spike)
                fee = stake * mult * TRADING_FEE_RATE
                self.paper[mode] = PaperPosition(
                    mode=mode, side=side, stake=stake, multiplier=mult,
                    entry_px=price, entry_idx=self.tick_idx, entry_epoch=epoch,
                    commission=fee)
                self.flip_intent = None         # flip leg consumed
                store.record_signal(self.symbol, self.labeler.age,
                                    round(p_fast, 5), round(p_slow, 5),
                                    round(snap.get("p_next_15", 0), 5),
                                    f"paper_enter_{mode}", dec.confidence)
                logger.info("[%s] PAPER enter %s %s conf=%.2f (%s)",
                            self.symbol, mode, side, dec.confidence, dec.reason)

    def _close_paper(self, mode: str, pos: PaperPosition, price: float,
                     reason: str) -> None:
        profit = pos.pnl_usd(price)
        self.paper[mode] = None
        self.paper_profits[mode].append(profit)
        self.counters["paper_trades"] += 1
        self.cooldown_until[mode] = self.tick_idx + max(10, int(self.mean_interval * 0.05))
        paper_bal = hub_paper_balance() + profit
        store.record_trade(
            symbol=self.symbol, mode=mode, side=pos.side, live=False,
            stake=pos.stake, multiplier=pos.multiplier,
            entry_px=pos.entry_px, exit_px=price, profit=profit,
            commission=pos.commission * 2,
            ticks_held=self.tick_idx - pos.entry_idx, reason=reason,
            balance_after=paper_bal)
        self._update_auto()
        logger.info("[%s] PAPER close %s %s %.4f (%s)",
                    self.symbol, mode, pos.side, profit, reason)

    def _update_auto(self) -> None:
        mode, reason = strat.select_auto_mode(
            list(self.paper_profits["drift"]),
            list(self.paper_profits["spike"]),
            self.settings, self.auto_mode)
        self.auto_mode, self.auto_reason = mode, reason

    # ---- live trading -----------------------------------------------------

    async def _live_manage(self, hub: "AgentHub") -> None:
        if not hub.running or not hub.settings.get("live_enabled"):
            return
        self.flip_intent = None                 # flip intent lives one tick
        if self.live is not None:
            await self._live_exit_check(hub)
            # THE FLIP: the exit opened the door — take the other leg NOW,
            # same tick, so the spike move is captured from its first tick
            if self.live is None and self.flip_intent:
                await self._live_entry_check(hub)
        else:
            await self._live_entry_check(hub)
        self.flip_intent = None

    def _other_mode_signal(self, mode: str, exit_th: float) -> strat.Decision:
        """The other book's entry decision — a flip signal for this book."""
        thr = float(self.settings.get("entry_threshold", 0.50))
        if mode == "drift":
            return strat.entry_spike(self.p_fast_used, self.p_slow_used,
                                     self.mean_interval, threshold=thr)
        fdict = self._feature_dict()
        return strat.entry_drift(fdict, self.p_fast_used, exit_th,
                                 self.mean_interval, fdict["vol_ratio"],
                                 threshold=thr, p_slow=self.p_slow_used)

    @property
    def p_fast_used(self) -> float:
        return getattr(self, "p_fast", 0.0)

    @property
    def p_slow_used(self) -> float:
        return getattr(self, "p_slow", 0.0)

    def _live_allowed_mode(self, hub: "AgentHub") -> Optional[str]:
        configured = str(hub.settings.get("mode", "auto"))
        if configured in ("drift", "spike"):
            return configured
        return None if self.auto_mode == "observe" else self.auto_mode

    async def _live_entry_check(self, hub: "AgentHub") -> None:
        mode = self._live_allowed_mode(hub)
        if not mode:
            return
        if not self.hazard.entry_ready:
            return
        if self.tick_idx < self.cooldown_until[mode]:
            return
        p_fast, p_slow = self.p_fast_used, self.p_slow_used
        fdict = self._feature_dict()
        thr = float(self.settings.get("entry_threshold", 0.50))
        if mode == "drift":
            dec = strat.entry_drift(fdict, p_fast,
                                    float(self.settings.get("exit_threshold", 0.35)),
                                    self.mean_interval, fdict["vol_ratio"],
                                    threshold=thr, p_slow=p_slow)
        else:
            dec = strat.entry_spike(p_fast, p_slow, self.mean_interval,
                                    threshold=thr)
        if not dec.action.startswith("enter"):
            return
        ok_ev, ev_val, ev_why = self._ev_check(
            mode, float(self.settings.get("exit_threshold", 0.35)))
        if not ok_ev:
            logger.info("[%s] live entry skipped (EV): %s", self.symbol, ev_why)
            return
        ok, why = hub.risk.can_trade_live(hub.live_balance,
                                          bool(self.settings.get("live_enabled")))
        if not ok:
            logger.info("[%s] live entry blocked: %s", self.symbol, why)
            return
        stake = hub.risk.stake_for(hub.live_balance)
        if stake <= 0:
            return
        await self._live_enter(hub, mode, stake, dec)

    async def _live_enter(self, hub: "AgentHub", mode: str, stake: float,
                          dec: strat.Decision) -> None:
        ch: Optional[Channel] = hub.authed
        if not ch or not ch.connected:
            return
        side = (self.contract_side_for_drift if mode == "drift"
                else self.contract_side_for_spike)
        ct = strat.multiplier_for(side)
        mult = int(self.settings.get("multiplier", 100))
        prop = await ch.rpc({
            "proposal": 1, "amount": stake, "basis": "stake",
            "contract_type": ct, "currency": hub.currency,
            "multiplier": mult, "underlying_symbol": self.symbol,
        })
        if prop.get("error"):
            logger.error("[%s] proposal failed: %s", self.symbol, prop["error"])
            return
        pr = prop.get("proposal", {})
        pid = pr.get("id")
        commission = float(pr.get("commission", 0.0) or 0.0)
        if not pid:
            return
        buy = await ch.rpc({
            "buy": pid, "price": stake,
            "parameters": {
                "contract_type": ct, "currency": hub.currency,
                "underlying_symbol": self.symbol, "amount": stake,
                "basis": "stake", "multiplier": mult,
            },
        })
        if buy.get("error"):
            logger.error("[%s] buy failed: %s", self.symbol, buy["error"])
            store.log("error", f"{self.symbol} buy failed: {buy['error']}")
            return
        b = buy.get("buy", {})
        cid = int(b.get("contract_id", 0))
        if not cid:
            return
        entry_px = float(pr.get("spot", self.last_price) or self.last_price)
        self.live = LivePosition(
            mode=mode, side=side, contract_type=ct, contract_id=cid,
            stake=stake, multiplier=mult, entry_px=entry_px,
            entry_idx=self.tick_idx, entry_epoch=self.last_epoch,
            commission=commission)
        hub.register_contract(cid, self)
        await ch.rpc({"proposal_open_contract": 1, "contract_id": cid,
                      "subscribe": 1}, timeout=10)
        store.record_signal(self.symbol, self.labeler.age, 0, 0, 0,
                            f"live_enter_{mode}", dec.confidence)
        logger.warning("[%s] LIVE enter %s %s x%d stake=%.2f cid=%s (%s)",
                       self.symbol, mode, ct, mult, stake, cid, dec.reason)
        store.log("trade", f"LIVE BUY {self.symbol} {ct} x{mult} ${stake:.2f} ({dec.reason})")

    async def _live_exit_check(self, hub: "AgentHub") -> None:
        pos = self.live
        if not pos:
            return
        p_fast = self.p_fast_used
        lf = strat.lift(p_fast, H_FAST, self.mean_interval)
        ls = strat.lift(self.p_slow_used, H_SLOW, self.mean_interval)
        pnl_pct = (pos.profit / max(pos.stake, 1e-9)) * 100.0
        spiked = self.labeler.age == 0     # a spike landed on this very tick
        pnl_peak = max(pos.pnl_peak_pct, pnl_pct)
        pos.pnl_peak_pct = pnl_peak
        exit_now, reason, _urgency = strat.should_exit(
            pos.mode,
            spike_now=spiked,
            pnl_pct=pnl_pct,
            p_fast=p_fast,
            mean_interval=self.mean_interval,
            ticks_held=self.tick_idx - pos.entry_idx,
            max_hold=self.max_hold_for(pos.mode),
            exit_threshold=float(self.settings.get("exit_threshold", 0.35)),
            stop_loss_pct=float(self.settings.get("stop_loss_pct", 40.0)),
            take_profit_pct=float(self.settings.get("take_profit_pct", 60.0)),
            lift_fast=lf,
            pnl_peak_pct=pnl_peak,
            lift_slow=ls,
        )
        if pos.mode == "spike" and spiked:
            ours = ((pos.side == "UP" and self.spike_dir > 0) or
                    (pos.side == "DOWN" and self.spike_dir < 0))
            if not ours:
                exit_now, reason = True, "spike_against"
            else:
                exit_now, reason = True, "spike_caught"
        # THE FLIP: the other book's entry signal closes this book and
        # queues the opposite leg for the same tick
        if not exit_now:
            other = self._other_mode_signal(pos.mode,
                                            float(self.settings.get("exit_threshold", 2.0)))
            if other.action.startswith("enter") and \
                    self._ev_check(other.mode if hasattr(other, "mode") else
                                   ("spike" if pos.mode == "drift" else "drift"),
                                   float(self.settings.get("exit_threshold", 2.0)))[0]:
                exit_now, reason = True, ("flip_to_spike" if pos.mode == "drift"
                                          else "flip_to_drift")
                self.flip_intent = "spike" if pos.mode == "drift" else "drift"
        if exit_now:
            await self._live_exit(hub, reason)

    async def _live_exit(self, hub: "AgentHub", reason: str) -> None:
        pos, ch = self.live, hub.authed
        if not pos or not ch:
            return        # refresh contract state for a valid sell price
        poc = await ch.rpc({"proposal_open_contract": 1,
                            "contract_id": pos.contract_id}, timeout=10)
        info = poc.get("proposal_open_contract", {}) or pos.last_poc
        price = float(info.get("sell_price", 0.0) or 0.0)
        resp = await ch.rpc({"sell": pos.contract_id, "price": price}, timeout=10)
        if resp.get("error"):
            # price may have moved — retry once with a fresh quote
            await asyncio.sleep(0.4)
            poc = await ch.rpc({"proposal_open_contract": 1,
                                "contract_id": pos.contract_id}, timeout=10)
            info = poc.get("proposal_open_contract", {}) or info
            price = float(info.get("sell_price", 0.0) or 0.0)
            resp = await ch.rpc({"sell": pos.contract_id, "price": price}, timeout=10)
            if resp.get("error"):
                logger.error("[%s] sell failed: %s", self.symbol, resp["error"])
                store.log("error", f"{self.symbol} sell failed: {resp['error']}")
                return
        # the POC stream may have settled the contract first — don't double-record
        if self.live is None:
            return
        sold = float(resp.get("sell", {}).get("sold_for", 0.0) or 0.0)
        profit = sold - pos.stake
        self.counters["live_trades"] += 1
        self.cooldown_until[pos.mode] = self.tick_idx + max(10, int(self.mean_interval * 0.05))
        hub.on_live_closed(profit)
        store.record_trade(
            symbol=self.symbol, mode=pos.mode, side=pos.side, live=True,
            stake=pos.stake, multiplier=pos.multiplier,
            entry_px=pos.entry_px, exit_px=sold, profit=profit,
            commission=pos.commission,
            ticks_held=self.tick_idx - pos.entry_idx, reason=reason,
            balance_after=hub.live_balance)
        hub.unregister_contract(pos.contract_id)
        self.live = None
        hub.risk.record_live(profit)
        emoji = "+" if profit >= 0 else ""
        logger.warning("[%s] LIVE close %s %s %s%.2f (%s)",
                       self.symbol, pos.mode, pos.side, emoji, profit, reason)
        store.log("trade", f"LIVE SELL {self.symbol} {pos.contract_type} "
                           f"{emoji}{profit:.2f} ({reason})")

    def on_poc(self, msg: dict, hub: "AgentHub") -> None:
        """proposal_open_contract stream update for our live position."""
        poc = msg.get("proposal_open_contract") or {}
        if not poc:
            return
        cid = int(poc.get("contract_id", 0) or 0)
        if self.live and cid == self.live.contract_id:
            self.live.last_poc = poc
            if poc.get("is_sold"):
                # contract settled on its own (stop-out or sold elsewhere)
                profit = float(poc.get("profit", 0.0) or 0.0)
                pos = self.live
                self.counters["live_trades"] += 1
                self.cooldown_until[pos.mode] = self.tick_idx + max(10, int(self.mean_interval * 0.05))
                hub.on_live_closed(profit)
                store.record_trade(
                    symbol=self.symbol, mode=pos.mode, side=pos.side, live=True,
                    stake=pos.stake, multiplier=pos.multiplier,
                    entry_px=pos.entry_px,
                    exit_px=float(poc.get("sell_price", 0) or 0),
                    profit=profit, commission=pos.commission,
                    ticks_held=self.tick_idx - pos.entry_idx,
                    reason="stop_out" if profit < 0 else "settled",
                    balance_after=hub.live_balance)
                hub.unregister_contract(pos.contract_id)
                hub.risk.record_live(profit)
                self.live = None
                store.log("trade", f"LIVE settle {self.symbol} {profit:+.2f} "
                                   f"({pos.contract_type})")

    # ---- status -----------------------------------------------------------

    def snapshot(self) -> dict:
        px = self.last_price
        fstat = self.fast.stats()
        sstat = self.slow.stats()
        wf = blend_weight(self.fast.n_updates, self.fast.brier_skill)
        ws = blend_weight(self.slow.n_updates, self.slow.brier_skill)
        paper = {}
        for mode, pos in self.paper.items():
            paper[mode] = None if not pos else {
                "side": pos.side, "stake": pos.stake,
                "entry_px": pos.entry_px,
                "pnl": round(pos.pnl_usd(px), 4),
                "pnl_pct": round(pos.pnl_pct(px), 2),
                "held": self.tick_idx - pos.entry_idx,
            }
        live = None
        if self.live:
            live = {
                "contract_id": self.live.contract_id,
                "type": self.live.contract_type, "mode": self.live.mode,
                "stake": self.live.stake, "entry_px": self.live.entry_px,
                "profit": round(self.live.profit, 4),
                "held": self.tick_idx - self.live.entry_idx,
            }
        dp = list(self.paper_profits["drift"])
        sp = list(self.paper_profits["spike"])
        return {
            "symbol": self.symbol, "label": self.label,
            "price": round(px, 5) if px else None,
            "age": self.labeler.age,
            "spikes": self.counters["spikes"],
            "mean_interval": round(self.mean_interval, 1),
            "p_fast": round(self.p_fast, 5),
            "p_slow": round(self.p_slow, 5),
            "model_fast": round(self.p_model[0], 5),
            "model_slow": round(self.p_model[1], 5),
            "hazard_fast": round(self.hazard.prob_within(self.labeler.age, 3), 5),
            "age_pct": round(self.hazard.percentile(self.labeler.age), 3),
            "threshold": round(self.labeler.threshold, 6),
            "auto_mode": self.auto_mode, "auto_reason": self.auto_reason,
            "paper": paper, "live": live,
            "paper_drift": {"n": len(dp), "net": round(sum(dp), 3),
                            "avg": round(float(np.mean(dp)), 4) if dp else 0},
            "paper_spike": {"n": len(sp), "net": round(sum(sp), 3),
                            "avg": round(float(np.mean(sp)), 4) if sp else 0},
            "models": {"fast": fstat, "slow": sstat},
            "hazard_gaps": len(self.hazard.intervals),
            "model_weight": {"fast": round(wf, 3), "slow": round(ws, 3)},
            "economics": {
                "avg_spike_pct": round(self.avg_spike_pct, 5),
                "drift_per_tick_pct": round(self.drift_per_tick_pct, 7),
            },
            "regime_shift": self.hazard.regime_shift,
        }


def hub_paper_balance() -> float:
    """Paper account equity: start + all-time paper profit (from the store)."""
    rows = store.get_trades(limit=100000, live=False)
    return round(config.PAPER_BALANCE + sum(r["profit"] for r in rows), 2)


# ---------------------------------------------------------------------------
# hub
# ---------------------------------------------------------------------------

class AgentHub:
    def __init__(self) -> None:
        self.settings: dict = store.load_settings()
        self.risk = risk_mod.RiskManager(self.settings)
        self.public: Optional[Channel] = None
        self.authed: Optional[Channel] = None
        self.brains: dict[str, SymbolBrain] = {}
        self.contract_map: dict[int, SymbolBrain] = {}
        self.running = False
        self.started_at: float = 0.0
        self.live_balance: float = 0.0
        self.currency = "USD"
        self.pips: dict[str, float] = {}
        self.tasks: list[asyncio.Task] = []
        self.status_error = ""
        self._last_equity = 0.0
        self._starting = False

    # ---- settings ---------------------------------------------------------

    def apply_settings(self, settings: dict) -> None:
        self.settings = settings
        self.risk.update_settings(settings)
        for b in self.brains.values():
            b.update_settings(settings)

    async def sync_auth(self) -> None:
        """Start/stop the authenticated channel when settings change live."""
        if not self.running:
            return
        from ..deriv import auth as auth_mod

        want = bool(self.settings.get("live_enabled"))
        app_id, pat = auth_mod.credentials()
        have = self.authed is not None
        if want and pat and app_id and not have:
            await self._start_authed()
        elif (not want or not (pat and app_id)) and have:
            if self.authed:
                await self.authed.stop()
                self.authed = None
            store.log("info", "live channel stopped (settings changed)")

    def register_contract(self, cid: int, brain: SymbolBrain) -> None:
        self.contract_map[cid] = brain

    def unregister_contract(self, cid: int) -> None:
        self.contract_map.pop(cid, None)

    def on_live_closed(self, profit: float) -> None:
        self.live_balance += profit

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self.running or self._starting:
            return
        self._starting = True
        try:
            await self._start_inner()
        finally:
            self._starting = False

    async def _start_inner(self) -> None:
        self.settings = store.load_settings()
        self.risk = risk_mod.RiskManager(self.settings)
        self.status_error = ""
        store.log("info", "agent starting")

        # public market-data channel
        self.public = dclient.public_channel()
        await self.public.start()
        for _ in range(30):
            if self.public.connected:
                break
            await asyncio.sleep(0.5)
        if not self.public.connected:
            self.status_error = "cannot reach Deriv public market-data endpoint"
            store.log("error", self.status_error)
            return

        # pip sizes
        resp = await self.public.rpc({"active_symbols": "brief"}, timeout=20)
        for s in resp.get("active_symbols", []):
            sym = s.get("underlying_symbol")
            if sym:
                self.pips[sym] = float(s.get("pip_size", 0.001) or 0.001)

        # brains + warm-up (cache first for instant restarts).
        # Slow symbols need proportionally more history: the hazard model
        # needs ~10 spike gaps, and a 1000-interval symbol only spikes
        # once per ~1000 ticks.
        symbols = list(self.settings.get("symbols", config.DEFAULT_SYMBOLS))
        for sym in symbols:
            if sym not in config.SYMBOL_META:
                continue
            brain = SymbolBrain(sym, self.pips.get(sym, 0.001), self.settings)
            brain.load_models()
            brain.load_hazard()          # restore spike-timing memory
            self.brains[sym] = brain

        # Warm-up strategy: history comes from OUR OWN tick cache (built
        # live 24/7) first, so the heavy Deriv download happens ONLY on the
        # very first boot. Live ticks keep appending to the cache forever.
        base_warm = int(self.settings.get("warmup_ticks", 4000))
        for sym, brain in self.brains.items():
            interval = config.SYMBOL_META.get(sym, {}).get("avg_interval", 500)
            warmup = max(base_warm, min(6000, int(interval * 12)))
            cached = store.load_cached_ticks(sym, warmup)
            if len(cached) >= 500:
                logger.info("%s: warming from own cache (%d ticks) — no download",
                            sym, len(cached))
                await brain.warm_async(cached)
            else:
                logger.info("%s: first boot — downloading %d ticks once...",
                            sym, warmup)
                ticks = await dclient.fetch_history(self.public, sym, warmup)
                if ticks:
                    store.cache_ticks(sym, ticks[-8000:])
                    await brain.warm_async(ticks)
                else:
                    logger.warning("%s: no history available — learning from "
                                   "live ticks only", sym)

        # ONE-TIME MASS STUDY: read ~50k real ticks per symbol DIRECTLY from
        # Deriv (chunked page reads, nothing stored locally) and push them
        # through the learning pipeline. The brain pre-trains on the symbol's
        # full recent history in minutes instead of trickling in knowledge
        # over days. Runs once; afterwards live ticks keep it sharp.
        if not store.load_settings().get("mass_study_done"):
            await self._mass_study()

        # live stream
        for sym, brain in self.brains.items():
            await self.public.subscribe(
                {"ticks": sym, "subscribe": 1},
                self._tick_handler(brain), symbol_key=sym)

        # authenticated channel (only when live trading is enabled)
        app_id, pat = auth_mod.credentials()
        if self.settings.get("live_enabled") and pat and app_id:
            await self._start_authed()

        self.running = True
        self.started_at = time.time()
        self.tasks = [
            asyncio.create_task(self._equity_loop(), name="equity"),
            asyncio.create_task(self._persist_loop(), name="persist"),
        ]
        store.log("info", f"agent running on {len(self.brains)} symbols")
        logger.info("AgentHub started (%d symbols, live=%s)",
                    len(self.brains), bool(self.settings.get("live_enabled")))

    def learning_snapshot(self) -> dict:
        """Plain-English summary of what the agent has learned so far."""
        symbols = {}
        for sym, b in self.brains.items():
            f, s = b.fast, b.slow
            symbols[sym] = {
                "updates": int(f.n_updates + s.n_updates),
                "fast_skill": round(f.brier_skill, 3),
                "slow_skill": round(s.brier_skill, 3),
                "spikes_seen": int(b.counters["spikes"]),
                "hazard_gaps": len(b.hazard.intervals),
                "memory_ok": len(b.hazard.intervals) > 0 or f.n_updates > 0,
            }
        return {
            "symbols": symbols,
            "last_save": store.last_model_save(),
            "notebook_on": notebook_available(),
        }


def notebook_available() -> bool:
    try:
        from .. import notebook
        return notebook.available()
    except Exception:
        return False

    async def _mass_study(self) -> None:
        """Deep pre-training on real history, straight from Deriv.

        Sized for tiny free hosts: ~20k ticks per symbol, fetched in
        1000-tick pages (server-friendly) and LEARNED in 400-tick slices
        with pauses between them, so the CPU duty cycle stays low and the
        web server / health checks never starve. Nothing is cached — the
        knowledge distils into the model weights and hazard statistics,
        which ARE persisted. Free hosts wipe the disk on redeploy, so the
        study re-runs per boot; at this size that costs a few minutes of
        gentle background work, not a crash.
        """
        want = int(self.settings.get("study_ticks", 20000))
        logger.info("[study] mass-learning begins (%d ticks per symbol, direct from Deriv)", want)
        for sym, brain in list(self.brains.items()):
            try:
                ticks = await dclient.fetch_history(self.public, sym, want,
                                                    page_pause=0.25)
                if len(ticks) < 2000:
                    logger.warning("[study] %s: only %d ticks available — skipped",
                                   sym, len(ticks))
                    continue
                t0 = time.time()
                chunk, pause = 400, 0.45
                for i in range(0, len(ticks), chunk):
                    for epoch, price in ticks[i:i + chunk]:
                        brain._step(price, epoch)
                    await asyncio.sleep(pause)   # let the host breathe
                brain.save_models()
                brain.save_hazard()
                logger.info("[study] %s: studied %d ticks (%d spikes) in %.0fs — "
                            "models saved", sym, len(ticks),
                            brain.counters["spikes"], time.time() - t0)
                await asyncio.sleep(1.5)
            except Exception as e:
                logger.warning("[study] %s: interrupted (%s) — will retry next boot",
                               sym, e)
                return
        store.save_setting("mass_study_done", time.time())
        store.log("info", "mass study complete — brain pre-trained on real history")
        logger.info("[study] complete — live trading with pre-trained brain")

    def _tick_handler(self, brain: SymbolBrain):
        async def handler(msg: dict) -> None:
            tick = msg.get("tick") or {}
            price = float(tick.get("quote", 0) or 0)
            epoch = float(tick.get("epoch", time.time()))
            if not price:
                return
            try:
                await brain.on_tick(price, epoch, self)
            except Exception as e:
                logger.exception("[%s] tick error: %s", brain.symbol, e)
        return handler

    async def _start_authed(self) -> None:
        from ..deriv import auth as auth_mod

        account_id = str(self.settings.get("account_id", "") or "")
        dclient.set_account_id(account_id)
        if not account_id:
            self.status_error = ("live enabled but no Options account selected — "
                                 "pick one in Settings")
            store.log("error", self.status_error)
            return
        try:
            accounts = await auth_mod.list_accounts()
            self._accounts = accounts
        except Exception as e:
            logger.warning("account list failed: %s", e)

        async def on_reconnect() -> None:
            ch = self.authed
            if not ch:
                return
            await ch.rpc({"balance": 1, "subscribe": 1})
            await self._adopt_portfolio()

        self.authed = dclient.authed_channel(on_reconnect=on_reconnect)
        self.authed.route_type("balance", self._balance_handler())
        self.authed.route_type("proposal_open_contract", self._poc_handler())
        await self.authed.start()
        for _ in range(40):
            if self.authed.connected:
                break
            await asyncio.sleep(1.0)
        if not self.authed.connected:
            self.status_error = f"auth failed: {self.authed.last_error}"
            store.log("error", self.status_error)

    def _balance_handler(self):
        async def handler(msg: dict) -> None:
            bal = msg.get("balance") or {}
            if bal:
                self.live_balance = float(bal.get("balance", 0) or 0)
                self.currency = bal.get("currency", self.currency)
        return handler

    def _poc_handler(self):
        async def handler(msg: dict) -> None:
            poc = msg.get("proposal_open_contract") or {}
            cid = int(poc.get("contract_id", 0) or 0)
            brain = self.contract_map.get(cid)
            if brain:
                brain.on_poc(msg, self)
        return handler

    async def _adopt_portfolio(self) -> None:
        """Pick up contracts left open by a previous run."""
        ch = self.authed
        if not ch:
            return
        resp = await ch.rpc({"portfolio": 1}, timeout=10)
        contracts = (resp.get("portfolio") or {}).get("contracts", []) or []
        for c in contracts:
            cid = int(c.get("contract_id", 0) or 0)
            underlying = c.get("underlying", "")
            brain = self.brains.get(underlying)
            if not brain or brain.live or not cid:
                continue
            ct = c.get("contract_type", "")
            side = "UP" if ct == "MULTUP" else "DOWN"
            brain.live = LivePosition(
                mode="drift", side=side, contract_type=ct, contract_id=cid,
                stake=float(c.get("buy_price", 0) or 0),
                multiplier=int(c.get("multiplier", 100) or 100),
                entry_px=float(c.get("entry_spot", 0) or 0),
                entry_idx=brain.tick_idx, entry_epoch=time.time(),
                last_poc=c)
            self.register_contract(cid, brain)
            await ch.rpc({"proposal_open_contract": 1, "contract_id": cid,
                          "subscribe": 1}, timeout=10)
            store.log("info", f"adopted open contract {cid} on {underlying}")

    async def stop(self) -> None:
        if not self.running and not self.public:
            return
        self.running = False
        store.log("info", "agent stopping")
        for t in self.tasks:
            t.cancel()
        self.tasks = []

        # close paper books at last price
        for brain in self.brains.values():
            for mode, pos in list(brain.paper.items()):
                if pos:
                    brain._close_paper(mode, pos, brain.last_price, "agent_stop")
            brain.save_models()
            brain.save_hazard()
            if brain.recent:
                store.cache_ticks(brain.symbol, list(brain.recent)[-8000:])

        if self.authed:
            await self.authed.stop()
            self.authed = None
        if self.public:
            await self.public.stop()
            self.public = None
        logger.info("AgentHub stopped")

    # ---- background loops --------------------------------------------------

    async def _equity_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                store.record_equity(hub_paper_balance(), live=False)
                if self.settings.get("live_enabled") and self.live_balance:
                    store.record_equity(self.live_balance, live=True)
                self._last_equity = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("equity loop: %s", e)

    async def _persist_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                for brain in self.brains.values():
                    brain.save_models()
                    brain.save_hazard()      # spike-timing memory survives restarts
                    # append fresh LIVE ticks to the cache: history grows
                    # 24/7 from the stream, downloads never repeat
                    if brain.recent:
                        store.cache_ticks(brain.symbol, list(brain.recent)[-300:])
                # permanent memory: mirror the notebook to GitHub (no-op
                # when the token isn't present, e.g. local runs)
                from .. import notebook
                notebook.push_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("persist loop: %s", e)

    # ---- status -----------------------------------------------------------

    def snapshot(self) -> dict:
        uptime = time.time() - self.started_at if self.started_at else 0
        return {
            "running": self.running,
            "started_at": self.started_at,
            "uptime_s": int(uptime),
            "error": self.status_error,
            "live_enabled": bool(self.settings.get("live_enabled")),
            "account_mode": self.settings.get("account_mode", "demo"),
            "account_id": self.settings.get("account_id", ""),
            "configured": bool(auth_mod.credentials()[0] and auth_mod.credentials()[1]),
            "public_connected": bool(self.public and self.public.connected),
            "auth_connected": bool(self.authed and self.authed.connected),
            "live_balance": self.live_balance,
            "currency": self.currency,
            "paper_balance": hub_paper_balance(),
            "learning": self.learning_snapshot(),
            "mode": self.settings.get("mode", "auto"),
            "risk": {
                "daily_profit": round(self.risk.daily_profit, 2),
                "consecutive_losses": self.risk.consecutive_losses,
            },
            "symbols": {s: b.snapshot() for s, b in dict(self.brains).items()},
        }


# process-wide singleton
hub = AgentHub()
