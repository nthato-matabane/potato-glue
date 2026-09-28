"""
Deriv Boom/Crash study — NEW Options API public WS (no auth).
1. Full Crash/Boom family + pip sizes
2. contracts_for: which trade types each symbol supports
3. Real tick statistics per symbol: drift direction, spike intervals/sizes,
   hazard curve (spike probability vs ticks-since-last-spike)
"""

import asyncio
import json
import statistics
import sys

import websockets

WS_PUBLIC = "wss://api.derivws.com/trading/v1/options/ws/public"


class Session:
    def __init__(self, ws):
        self.ws = ws
        self.req_id = 0

    async def call(self, payload: dict, timeout: float = 30.0) -> dict:
        self.req_id += 1
        payload = dict(payload)
        payload["req_id"] = self.req_id
        await self.ws.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data.get("req_id") == self.req_id:
                if "error" in data and data["error"]:
                    raise RuntimeError(data["error"])
                return data


def spike_stats(sym: str, prices: list, times: list) -> dict:
    moves = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    abs_moves = [abs(m) for m in moves]
    med = statistics.median(abs_moves)
    thresh = max(6 * med, 1e-12)
    spike_idx = [i for i, m in enumerate(abs_moves) if m > thresh]
    intervals = [spike_idx[i + 1] - spike_idx[i] for i in range(len(spike_idx) - 1)]
    up = sum(1 for i in spike_idx if moves[i] > 0)
    down = len(spike_idx) - up
    sp = set(spike_idx)
    drift = [moves[i] for i in range(len(moves)) if i not in sp]
    print(f"\n  {sym}: {len(prices)} ticks | last {prices[-1]:.4f} | "
          f"span {(times[-1]-times[0])/60:.1f} min")
    print(f"    median|move|={med:.6f}  spike thresh(6x)={thresh:.6f}")
    print(f"    spikes={len(spike_idx)} (up={up} down={down}) -> "
          f"{'BOOM (up spikes)' if up >= down else 'CRASH (down spikes)'}")
    if intervals:
        print(f"    interval(ticks): mean={statistics.mean(intervals):.0f} "
              f"median={statistics.median(intervals):.0f} min={min(intervals)} max={max(intervals)}")
    if drift:
        print(f"    non-spike drift: mean={statistics.mean(drift):+.7f} sum={sum(drift):+.4f}")
    spike_sizes = [abs_moves[i] for i in spike_idx]
    if spike_sizes:
        print(f"    spike size: mean={statistics.mean(spike_sizes):.4f} "
              f"min={min(spike_sizes):.4f} max={max(spike_sizes):.4f}")
    # hazard: P(spike within next 5 ticks | ticks_since_spike = b)
    buckets = {}
    tss = 0
    for i in range(len(moves)):
        hit = 1 if i in sp else 0
        b = min(tss // 50 * 50, 500)
        buckets.setdefault(b, [0, 0])
        buckets[b][1] += 1
        if hit:
            buckets[b][0] += 1
        tss = 0 if hit else tss + 1
    haz = [(b, hits / tot) for b, (hits, tot) in sorted(buckets.items()) if tot >= 50]
    if haz:
        print("    hazard/since-spike: " + "  ".join(f"{b}:{p:.3f}" for b, p in haz))
    return {"spikes": len(spike_idx), "intervals": intervals}


async def main():
    async with websockets.connect(WS_PUBLIC, ping_interval=30, open_timeout=20) as ws:
        s = Session(ws)

        resp = await s.call({"active_symbols": "brief"})
        symbols = resp.get("active_symbols", [])
        bc = [x for x in symbols if "Boom" in x.get("underlying_symbol_name", "")
              or "Crash" in x.get("underlying_symbol_name", "")]
        print(f"=== CRASH/BOOM FAMILY ({len(bc)}) ===")
        for x in sorted(bc, key=lambda y: y.get("underlying_symbol_name", "")):
            print(f"  {x['underlying_symbol']:<12} {x['underlying_symbol_name']:<24} "
                  f"pip={x.get('pip_size')} open={x.get('exchange_is_open')} "
                  f"trades={x.get('trade_count')} suspended={x.get('is_trading_suspended')}")

        # ---- contract types ----
        print("\n=== CONTRACT TYPES (contracts_for) ===")
        for sym in ["BOOM1000", "CRASH1000", "BOOM500", "CRASH500"]:
            try:
                r = await s.call({"contracts_for": sym})
            except RuntimeError as e:
                print(f"  {sym}: ERROR {json.dumps(e.args[0])[:250] if e.args else e}")
                continue
            cf = r.get("contracts_for", {})
            contracts = cf.get("contracts", [])
            names = sorted({c.get("contract_type", c.get("short_name", "?")) for c in contracts})
            print(f"  {sym}: {len(contracts)} contracts | {names}")
            if contracts:
                print(f"      sample: {json.dumps(contracts[0])[:500]}")

        # ---- tick study ----
        print("\n=== TICK STUDY (3000 ticks each) ===")
        for x in sorted(bc, key=lambda y: y.get("underlying_symbol_name", "")):
            sym = x["underlying_symbol"]
            try:
                r = await s.call({"ticks_history": sym, "count": 3000,
                                  "end": "latest", "style": "ticks"})
            except RuntimeError as e:
                print(f"  {sym}: ERROR {json.dumps(e.args[0])[:250] if e.args else e}")
                continue
            hist = r.get("history", {})
            prices, times = hist.get("prices", []), hist.get("times", [])
            if len(prices) > 200:
                spike_stats(sym, prices, times)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
