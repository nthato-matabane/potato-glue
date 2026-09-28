"""
Profit-per-trade tuner — finds the exit settings that make the most money
per $1 of capital on REAL Boom/Crash tick history.

For each combination of (take_profit %, stop_loss %, multiplier) it replays
real ticks through the full agent pipeline and reports average profit per
trade. The winner becomes the app's default.

Usage:
    python tools/sweep.py                        # BOOM1000 + CRASH1000
    python tools/sweep.py --symbols BOOM500,CRASH500
"""

import argparse
import asyncio
import copy
import itertools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config                              # noqa: E402
from app.deriv import client as dclient             # noqa: E402
from app.engine.agent import SymbolBrain            # noqa: E402

TP_GRID = [100.0]          # tp/sl rarely bind (bot exits on its own timing)
SL_GRID = [40.0]
MULT_GRID = [200, 300, 400, 500]


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


def replay(symbol: str, ticks, tp: float, sl: float, mult: int) -> dict:
    s = copy.deepcopy(config.DEFAULT_SETTINGS)
    s["stake_usd"] = 1.0
    s["multiplier"] = mult
    s["take_profit_pct"] = tp
    s["stop_loss_pct"] = sl
    brain = SymbolBrain(symbol, 0.001, s)
    brain.warm(ticks)

    def stats(profits):
        if not profits:
            return {"n": 0, "net": 0.0, "avg": 0.0}
        wins = [p for p in profits if p > 0]
        return {"n": len(profits), "net": sum(profits),
                "avg": sum(profits) / len(profits),
                "wr": 100 * len(wins) / len(profits)}

    dp, sp = list(brain.paper_profits["drift"]), list(brain.paper_profits["spike"])
    return {"drift": stats(dp), "spike": stats(sp),
            "auto": brain.auto_mode, "reason": brain.auto_reason}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BOOM1000,CRASH1000")
    ap.add_argument("--ticks", type=int, default=25000)
    args = ap.parse_args()

    combos = list(itertools.product(TP_GRID, SL_GRID, MULT_GRID))
    print(f"{len(combos)} settings combos, {args.ticks} real ticks per symbol\n")

    best = None
    for symbol in args.symbols.split(","):
        symbol = symbol.strip().upper()
        print(f"=== {symbol}: fetching {args.ticks} ticks...")
        t0 = time.time()
        ticks = await fetch(symbol, args.ticks)
        if len(ticks) < 3000:
            print(f"  only {len(ticks)} ticks — skipping")
            continue
        print(f"  {len(ticks)} ticks in {time.time()-t0:.0f}s — replaying...")

        rows = []
        for tp, sl, mult in combos:
            r = replay(symbol, ticks, tp, sl, mult)
            for mode in ("drift", "spike"):
                st = r[mode]
                if st["n"] >= 5:            # need enough trades to trust it
                    rows.append((mode, tp, sl, mult, st))

        rows.sort(key=lambda x: x[4]["avg"], reverse=True)
        print("  mode    tp%   sl%  mult   n    win%   net$    avg$/trade")
        for mode, tp, sl, mult, st in rows[:8]:
            print(f"  {mode:5} {tp:5.0f} {sl:5.0f} {mult:5d} {st['n']:4d}"
                  f"  {st.get('wr',0):5.1f} {st['net']:+7.2f} {st['avg']:+8.4f}")
        if rows:
            m, tp, sl, mult, st = rows[0]
            print(f"  -> BEST: {m}, tp={tp:.0f}%, sl={sl:.0f}%, x{mult}: "
                  f"{st['avg']:+.4f} $/trade over {st['n']} trades\n")
            if best is None or st["avg"] > best[4]["avg"]:
                best = (symbol, m, tp, sl, mult, st)

    if best:
        symbol, m, tp, sl, mult, st = best
        print(f"WINNER overall: {symbol} {m} mode, tp={tp:.0f}%, "
              f"sl={sl:.0f}%, x{mult} -> {st['avg']:+.4f} $/trade")
    else:
        print("no combination produced enough trades")


if __name__ == "__main__":
    asyncio.run(main())
