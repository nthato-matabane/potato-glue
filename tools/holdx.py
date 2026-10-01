"""
Experiment — does a tighter drift max-hold stop the spike_hit bleed?

Replays REAL Deriv ticks through the full pipeline with different drift
max-hold caps (as a fraction of the mean spike interval) and reports
per-reason paper P&L. Run before touching the strategy.

Usage:
    python tools/holdx.py --symbol BOOM500 --ticks 30000
    python tools/holdx.py --all --ticks 20000
"""

import argparse
import asyncio
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, store                      # noqa: E402
from app.deriv import client as dclient            # noqa: E402
from app.engine import agent as agent_mod          # noqa: E402
from app.engine.agent import SymbolBrain           # noqa: E402

CAPS = [0.35, 0.5, 0.65, 0.8]      # fraction of mean interval


async def fetch(symbol: str, n: int):
    ch = dclient.public_channel()
    await ch.start()
    for _ in range(30):
        if ch.connected:
            break
        await asyncio.sleep(0.5)
    if not ch.connected:
        return []
    ticks = await dclient.fetch_history(ch, symbol, n, page_pause=0.15)
    await ch.stop()
    return ticks


def run(symbol: str, ticks, cap: float) -> dict:
    """Warm a fresh brain with drift max_hold = cap * mean_interval."""
    s = copy.deepcopy(config.DEFAULT_SETTINGS)
    for k, v in config.FORCED_SETTINGS.items():
        s[k] = copy.deepcopy(v)
    brain = SymbolBrain(symbol, 0.001, s)
    orig_max_hold = SymbolBrain.max_hold_for

    def capped(self, mode):
        h = orig_max_hold(self, mode)
        if mode == "drift" and cap > 0:
            return max(30, int(cap * self.mean_interval))
        return h

    SymbolBrain.max_hold_for = capped
    try:
        brain.warm(ticks)
    finally:
        SymbolBrain.max_hold_for = orig_max_hold

    rows = store._query(
        "SELECT mode, reason, count(*) n, sum(profit) p, avg(profit) a "
        "FROM trades GROUP BY mode, reason")
    return {f"{r['mode']}:{r['reason']}": r for r in rows}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BOOM500")
    ap.add_argument("--ticks", type=int, default=30000)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    store.init(config.DATA_DIR / "holdx.db")
    symbols = config.DEFAULT_SYMBOLS if args.all else [args.symbol]

    for symbol in symbols:
        ticks = await fetch(symbol, args.ticks)
        if len(ticks) < 5000:
            print(f"{symbol}: only {len(ticks)} ticks — skip")
            continue
        print(f"\n=== {symbol}: {len(ticks)} real ticks ===")
        print(f"{'cap':>5} | {'drift:net':>10} {'n':>4} | {'drift:spike_hit':>16} "
              f"{'n':>4} | {'drift:max_hold':>15} {'n':>4} | {'spike:net':>10}")
        for cap in CAPS:
            store._exec("DELETE FROM trades")
            r = run(symbol, ticks, cap)
            d_net = r.get("drift:max_hold", {}).get("p", 0)
            d_n = r.get("drift:max_hold", {}).get("n", 0)
            d_all_n = sum(v.get("n", 0) for k, v in r.items() if k.startswith("drift:"))
            d_all_p = sum(v.get("p", 0) for k, v in r.items() if k.startswith("drift:"))
            sh_p = r.get("drift:spike_hit", {}).get("p", 0)
            sh_n = r.get("drift:spike_hit", {}).get("n", 0)
            mh_p = r.get("drift:max_hold", {}).get("p", 0)
            mh_n = r.get("drift:max_hold", {}).get("n", 0)
            s_all_p = sum(v.get("p", 0) for k, v in r.items() if k.startswith("spike:"))
            print(f"{cap:>5.2f} | {d_all_p:>+10.2f} {d_all_n:>4} | {sh_p:>+16.2f} "
                  f"{sh_n:>4} | {mh_p:>+15.2f} {mh_n:>4} | {s_all_p:>+10.2f}")


if __name__ == "__main__":
    asyncio.run(main())
