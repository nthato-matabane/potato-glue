"""
1. Show contracts_for_response schema (correct response shape)
2. Brute-force proposal validation on public WS: which contract_type + duration
   combos are tradable for BOOM1000 / CRASH1000 (validation runs pre-auth).
"""

import asyncio
import json
import os

import websockets

WS_PUBLIC = "wss://api.derivws.com/trading/v1/options/ws/public"


def show_schema():
    p = "schemas/contracts_for_response.schema.json"
    if os.path.exists(p):
        print("=== contracts_for_response schema ===")
        print(json.dumps(json.load(open(p)), indent=1)[:3000])
    p2 = "schemas/proposal_response.schema.json"
    if os.path.exists(p2):
        s = json.load(open(p2))
        print("\n=== proposal_response: key fields ===")
        props = s.get("properties", {}).get("proposal", {}).get("properties", {})
        print(" ", list(props.keys()))


class Session:
    def __init__(self, ws):
        self.ws = ws
        self.req_id = 0

    async def call(self, payload: dict, timeout: float = 15.0) -> dict:
        self.req_id += 1
        payload = dict(payload)
        payload["req_id"] = self.req_id
        await self.ws.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data.get("req_id") == self.req_id:
                return data


CONTRACT_TYPES = [
    "CALL", "PUT", "HIGHER", "LOWER", "ONETOUCH", "NOTOUCH",
    "DIGITMATCH", "DIGITDIFF", "DIGITOVER", "DIGITUNDER",
    "UPORDOWN", "EXPIRYRANGE", "TICKHIGH", "TICKLOW",
    "VANILLALONGCALL", "VANILLALONGPUT", "ACCU", "RESETCALL", "RESETPUT",
]
DURATIONS = [(1, "t"), (2, "t"), (5, "t"), (10, "t"), (15, "t"), (25, "t"),
             (60, "s"), (300, "s"), (3600, "s")]


async def probe_symbol(s: Session, sym: str):
    print(f"\n=== {sym}: proposal validation ===")
    ok, errs = [], {}
    for ct in CONTRACT_TYPES:
        ct_ok = []
        for dur, unit in DURATIONS:
            r = await s.call({
                "proposal": 1, "amount": 10, "basis": "stake",
                "contract_type": ct, "currency": "USD",
                "duration": dur, "duration_unit": unit,
                "underlying_symbol": sym,
            })
            if r.get("error"):
                code = r["error"].get("code", "?")
                errs[code] = errs.get(code, 0) + 1
            else:
                p = r.get("proposal", {})
                ct_ok.append(f"{dur}{unit}:payout={p.get('payout')} spread={p.get('spread')}")
        if ct_ok:
            print(f"  ✅ {ct}: " + " | ".join(ct_ok[:6]))
            ok.append(ct)
    print(f"  tradable types: {ok}")
    print(f"  error codes seen: {errs}")


async def main():
    show_schema()
    async with websockets.connect(WS_PUBLIC, ping_interval=30, open_timeout=20) as ws:
        s = Session(ws)
        for sym in ["BOOM1000", "CRASH1000"]:
            await probe_symbol(s, sym)


if __name__ == "__main__":
    asyncio.run(main())
