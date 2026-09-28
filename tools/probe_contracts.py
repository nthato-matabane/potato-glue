"""
Discover which contract types Deriv allows on Boom/Crash via the new public API:
- active_symbols full (per-symbol contract info)
- contracts_for with/without extra params
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
                return data


async def main():
    async with websockets.connect(WS_PUBLIC, ping_interval=30, open_timeout=20) as ws:
        s = Session(ws)

        # full active_symbols — dump Boom/Crash entries completely
        r = await s.call({"active_symbols": "full"})
        if "error" in r and r["error"]:
            print("active_symbols full ERROR:", json.dumps(r["error"])[:300])
        else:
            for x in r.get("active_symbols", []):
                if x.get("underlying_symbol") in ("BOOM1000", "CRASH1000"):
                    print("=== FULL ENTRY", x.get("underlying_symbol"), "===")
                    print(json.dumps(x, indent=1))

        # contracts_for variants
        variants = [
            {"contracts_for": "BOOM1000"},
            {"contracts_for": "BOOM1000", "currency": "USD"},
            {"contracts_for": "BOOM1000", "currency": "USD", "contract_type": "CALL"},
            {"contracts_for": "BOOM1000", "contract_type": ["CALL", "PUT"]},
            {"contracts_for": "BOOM1000", "contract_type": "CALL", "duration_unit": "t",
             "start": "latest", "currency": "USD"},
        ]
        for v in variants:
            r = await s.call(v)
            if r.get("error"):
                print(f"\nREQ {json.dumps(v)}\n  ERROR: {json.dumps(r['error'])[:250]}")
            else:
                cf = r.get("contracts_for", {})
                contracts = cf.get("contracts", [])
                names = sorted({c.get("contract_type", "?") for c in contracts})
                print(f"\nREQ {json.dumps(v)}\n  -> {len(contracts)} contracts, types={names}")
                if contracts:
                    print("  sample:", json.dumps(contracts[0])[:400])

        # active_symbols with contract_type filter
        r = await s.call({"active_symbols": "brief", "contract_type": ["CALL", "PUT"]})
        if r.get("error"):
            print("\nactive_symbols+contract_type ERROR:", json.dumps(r["error"])[:250])
        else:
            syms = [x.get("underlying_symbol") for x in r.get("active_symbols", [])]
            bc = [x for x in syms if x and ("BOOM" in x or "CRASH" in x)]
            print(f"\nactive_symbols filtered CALL/PUT -> {len(syms)} symbols; Boom/Crash: {bc}")

        # try proposal on public (expected: auth error — confirms shape)
        r = await s.call({"proposal": 1, "amount": 1, "basis": "stake", "contract_type": "CALL",
                          "currency": "USD", "duration": 5, "duration_unit": "t",
                          "underlying_symbol": "BOOM1000"})
        print("\nproposal attempt:", json.dumps(r.get("error", r))[:350])


if __name__ == "__main__":
    asyncio.run(main())
