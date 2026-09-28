"""
Dump: full active_symbols list + raw ticks_history shape from the new Options public WS.
"""

import asyncio
import json

import websockets

WS_PUBLIC = "wss://api.derivws.com/trading/v1/options/ws/public"


class Session:
    def __init__(self, ws):
        self.ws = ws
        self.req_id = 0

    async def call(self, payload: dict, timeout: float = 25.0) -> dict:
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


async def main():
    async with websockets.connect(WS_PUBLIC, ping_interval=20, open_timeout=20) as ws:
        s = Session(ws)

        resp = await s.call({"active_symbols": "brief"})
        symbols = resp.get("active_symbols", [])
        print(f"=== {len(symbols)} ACTIVE SYMBOLS ===")
        for x in symbols:
            print(f"  {x.get('symbol','?'):<14} {x.get('display_name','?'):<34} "
                  f"submarket={x.get('submarket')} exchange_open={x.get('exchange_is_open')}")

        # search anything spike/vol/crash/boom related
        print("\n=== KEYWORD MATCHES (crash/boom/synth/vol/spike) ===")
        for x in symbols:
            hay = json.dumps(x).lower()
            if any(k in hay for k in ("crash", "boom", "spike", "volatility", "synthetic")):
                print(" ", json.dumps(x)[:300])

        # raw ticks_history shape
        print("\n=== RAW ticks_history (BOOM1000, count 10) ===")
        for sym in ["BOOM1000", "CRASH1000"]:
            try:
                r = await s.call({"ticks_history": sym, "count": 10, "end": "latest", "style": "ticks"})
                print(f"  {sym}: {json.dumps(r)[:600]}")
            except RuntimeError as e:
                print(f"  {sym}: ERROR {json.dumps(e.args[0])[:300] if e.args else e}")

        print("\n=== RAW ticks_history (first listed symbol, count 5) ===")
        first = symbols[0].get("symbol") if symbols else None
        if first:
            r = await s.call({"ticks_history": first, "count": 5, "end": "latest", "style": "ticks"})
            print(f"  {first}: {json.dumps(r)[:600]}")

        # what does a live ticks subscription look like
        print("\n=== RAW ticks subscribe (first symbol) ===")
        if first:
            self_id = s.req_id + 1
            await ws.send(json.dumps({"ticks": first, "subscribe": 1, "req_id": self_id}))
            for _ in range(3):
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                print("  ", raw[:300])


if __name__ == "__main__":
    asyncio.run(main())
