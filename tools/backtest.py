"""
Backtest — replay REAL Deriv tick history through the agent's full
pipeline (labeler → hazard → models → strategy → paper books).

No synthetic fairy-tale data: every tick comes from Deriv's public API,
so the report reflects the actual instrument you are about to trade.

Usage:
    python tools/backtest.py                       # BOOM1000 + CRASH1000, 20k ticks
    python tools/backtest.py --symbol CRASH500 --ticks 30000
    python tools/backtest.py --all                 # every configured symbol
"""

import argparse
import asyncio
import copy
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, store                      # noqa: E402
from app.deriv import client as dclient            # noqa: E402
from app.engine.agent import SymbolBrain           # noqa: E402


async def run_one(symbol: str, ticks_wanted: int) -> dict:
    ch = dclient.public_channel()
    await ch.start()
    for _ in range(30):
        if ch.connected:
            break
        await asyncio.sleep(0.5)
    if not ch.connected:
        print(f"  {symbol}: cannot reach Deriv public endpoint")
        return {}

    print(f"  {symbol}: fetching {ticks_wanted} ticks of real history...")
    t0 = time.time()
    ticks = await dclient.fetch_history(ch, symbol, ticks_wanted, page_pause=0.2)
    await ch.stop()
    if len(ticks) < 1000:
        print(f"  {symbol}: only {len(ticks)} ticks available — skipping")
        return {}
    print(f"  {symbol}: {len(ticks)} ticks in {time.time()-t0:.1f}s "
          f"({(ticks[-1][0]-ticks[0][0])/60:.0f} min of market)")

    settings = copy.deepcopy(config.DEFAULT_SETTINGS)
    brain = SymbolBrain(symbol, 0.001, settings)
    brain.warm(ticks)

    dp = list(brain.paper_profits["drift"])
    sp = list(brain.paper_profits["spike"])
    price = ticks[-1][1]
    open_pnl = {m: (p.pnl_usd(price) if p else 0.0)
                for m, p in brain.paper.items()}

    def summarize(name, profits, open_pnl_val):
        if not profits:
            return f"{name:6}: no trades"
        wins = [p for p in profits if p > 0]
        net = sum(profits)
        wr = 100 * len(wins) / len(profits)
        return (f"{name:6}: n={len(profits):4d}  wr={wr:5.1f}%  "
                f"net={net:+8.3f}  avg={net/len(profits):+.4f}  "
                f"open={open_pnl_val:+.3f}")

    print("  " + summarize("drift", dp, open_pnl["drift"]))
    print("  " + summarize("spike", sp, open_pnl["spike"]))
    print(f"  spikes={brain.counters['spikes']}  "
          f"mean_interval={brain.mean_interval:.0f}  "
          f"fast_skill={brain.fast.brier_skill:.3f}  "
          f"slow_skill={brain.slow.brier_skill:.3f}  "
          f"auto={brain.auto_mode} ({brain.auto_reason})")

    return {
        "symbol": symbol, "ticks": len(ticks),
        "spikes": brain.counters["spikes"],
        "drift": {"n": len(dp), "net": sum(dp),
                  "win": (100 * len([p for p in dp if p > 0]) / len(dp)) if dp else 0,
                  "open": open_pnl["drift"]},
        "spike": {"n": len(sp), "net": sum(sp),
                  "win": (100 * len([p for p in sp if p > 0]) / len(sp)) if sp else 0,
                  "open": open_pnl["spike"]},
        "fast_skill": brain.fast.brier_skill,
        "mean_interval": brain.mean_interval,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BOOM1000")
    ap.add_argument("--ticks", type=int, default=20000)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    store.init(config.DATA_DIR / "backtest.db")

    symbols = config.DEFAULT_SYMBOLS if args.all else [args.symbol]
    print("\n" + "=" * 66)
    print("  SPIKE AGENT BACKTEST — real Deriv ticks, full agent pipeline")
    print("=" * 66)

    results = []
    for sym in symbols:
        r = await run_one(sym, args.ticks)
        if r:
            results.append(r)

    if results:
        print("\n" + "-" * 66)
        print(f"  {'symbol':<10} {'mode':<7} {'trades':>6} {'win%':>6} "
              f"{'net$':>9} {'open$':>7} {'skill':>6}")
        for r in results:
            for mode in ("drift", "spike"):
                d = r[mode]
                print(f"  {r['symbol']:<10} {mode:<7} {d['n']:>6} "
                      f"{d['win']:>5.1f}% {d['net']:>+9.3f} "
                      f"{d['open']:>+7.3f} {r['fast_skill']:>6.3f}")
        print("-" * 66)
        print("  net = realized paper P&L after simulated commissions.")
        print("  Expectancy > 0 on a mode = the agent would pick that side.\n")


if __name__ == "__main__":
    asyncio.run(main())
