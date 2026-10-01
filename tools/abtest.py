"""
A/B experiment — old brain-handling vs new, on REAL Deriv ticks.

  OLD: model voice capped at 0.7 x skill, drift gate checked only the
       3-tick horizon, hazard memory lost on every restart.
  NEW: skillful model keeps the leading voice, drift gate checks BOTH
       horizons (3-tick + 15-tick).

Same ticks through the same pipeline; only the decision wiring differs.
Usage:  python tools/abtest.py --ticks 30000
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


def old_blend(n_updates: int, brier_skill: float) -> float:
    """The pre-fix formula: model capped at 70% even when proven."""
    skill = min(max(brier_skill, 0.0) / 0.2, 1.0)
    return min(n_updates / 800.0, 1.0) * 0.7 * skill


def fresh_brain(symbol: str) -> SymbolBrain:
    s = copy.deepcopy(config.DEFAULT_SETTINGS)
    for k, v in config.FORCED_SETTINGS.items():
        s[k] = copy.deepcopy(v)
    return SymbolBrain(symbol, 0.001, s)


def report(tag: str, brain: SymbolBrain) -> None:
    rows = store._query(
        "SELECT mode, reason, count(*) n, sum(profit) p, avg(profit) a "
        "FROM trades GROUP BY mode, reason ORDER BY mode, reason")
    tot_p = sum(r["p"] for r in rows)
    tot_n = sum(r["n"] for r in rows)
    print(f"  [{tag}] trades={tot_n}  NET={tot_p:+.2f}")
    for r in rows:
        print(f"     {r['mode']:>5}:{r['reason']:<15} n={r['n']:>4} "
              f"net={r['p']:>+9.2f}  avg={r['a']:+.4f}")
    print(f"     skill fast={brain.fast.brier_skill:.3f} "
          f"slow={brain.slow.brier_skill:.3f}")


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


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=30000)
    args = ap.parse_args()

    store.init(config.DATA_DIR / "abtest.db")
    for symbol in config.DEFAULT_SYMBOLS:
        ticks = await fetch(symbol, args.ticks)
        if len(ticks) < 5000:
            print(f"{symbol}: only {len(ticks)} ticks — skip")
            continue
        print(f"\n=== {symbol}: {len(ticks)} real ticks ===")

        store._exec("DELETE FROM trades")
        agent_mod.blend_weight = old_blend          # restore old formula
        old = fresh_brain(symbol)
        old.warm(ticks)
        report("OLD", old)

        store._exec("DELETE FROM trades")
        import importlib
        importlib.reload(agent_mod)                 # back to the new formula
        new = fresh_brain(symbol)
        new.warm(ticks)
        report("NEW", new)


if __name__ == "__main__":
    asyncio.run(main())
