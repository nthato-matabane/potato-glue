# ⚡ Spike Agent — self-learning Boom/Crash trader

A web app that studies Deriv's Crash & Boom indices, **teaches itself** to
predict spikes from live tick data, and trades multiplier contracts on your
Deriv account — **buys on Boom, sells on Crash**, with exits timed *just
before the spike*. It runs 24/7 in a container, is controlled from any
device's browser, and needs **no computer or phone of yours left running**.

```
 Any phone / laptop browser
        │  (your access password)
        ▼
 ┌───────────────────────────  cloud container  ───────────────────────────┐
 │  Dashboard (FastAPI + vanilla JS)                                      │
 │      │ start/stop · settings · equity · per-symbol learning panels     │
 │      ▼                                                                 │
 │  Self-learning agent  ──► Deriv WebSocket API (ticks + your account)   │
 │   labeler → hazard → online model → EV gate → risk → orders            │
 │      └──── SQLite: model weights, trades, equity (survives restarts)   │
 └─────────────────────────────────────────────────────────────────────────┘
```

---

## 1. What the study of Boom/Crash found (real data, this build)

Measured from Deriv's live feed across all 14 symbols (27 Sep 2026):

| Fact | Measurement |
|---|---|
| Boom spikes | **always UP** (100% of detected spikes), price drifts slowly DOWN between them |
| Crash spikes | **always DOWN**, price drifts slowly UP between them |
| Interval | matches the name: Boom/Crash 1000 ≈ 1000 ticks (measured 972–999), 500 ≈ 364–500, 150 ≈ ~130 |
| Spike arrival | close to memoryless; hazard rises modestly with ticks-since-spike — the model *measures* the real hazard curve instead of assuming |
| Trade types allowed | **multipliers only** (`MULTUP`/`MULTDOWN`, 100–500×) + accumulators. No CALL/PUT, no digits |
| Costs | commission ≈ **0.009% of notional per side** (≈1.8% of stake round trip at ×100 on a $5 stake) — this kills naive scalping, so the agent has an **EV gate** that refuses trades the maths can't pay for |
| API | Deriv's **new Options API** (`api.derivws.com`); legacy `ws.binaryws.com` was dead (HTTP 520) during the build |

**The two trades it knows how to make** (both are paper-traded always, the
agent trades live whichever side *actually pays*):

- **DRIFT mode** — `exit just before the spike`: on Boom go `MULTDOWN`, on
  Crash `MULTUP`, ride the slow drift, **sell the moment learned spike risk
  crosses the exit threshold**, rinse and repeat.
- **SPIKE mode** — `buy Boom / sell Crash`: on Boom `MULTUP`, on Crash
  `MULTDOWN`, enter only when learned spike odds clearly beat baseline
  (and beat costs), exit the tick the spike lands.
- **AUTO mode (default)** — measures both sides' net expectancy per symbol
  with hysteresis and trades only the profitable side; if neither pays, it
  keeps learning and stays in *observe* instead of gambling.

---

## 2. Get your Deriv credentials (5 minutes, one time)

The app talks to Deriv's **new API** (legacy app tokens don't work):

1. Go to **<https://developers.deriv.com>** → sign up / log in
   (use your normal Deriv account details).
2. In the **Dashboard → Register application** choose type **PAT**.
   This gives you an **App ID** (a fresh one — old app_ids are rejected).
3. **Dashboard → API tokens → Create PAT**, scopes:
   ✅ `trade` ✅ `account_manage`. Copy it **now** — it is shown once.
4. You automatically get a **demo Options account** on signup (paper money).
   You can list all your accounts later from the app's Settings page.
5. Paste **App ID** + **PAT** into the app's **⚙ Settings** → *Fetch my
   accounts* → pick the demo account → Save.

> Keep **Live trading OFF** (default) until the agent's paper results are
> positive — that is exactly what the auto mode is for.

---

## 3. Run it locally (first look)

```bash
cd spike-agent
pip install -r requirements.txt
cp .env.example .env          # optional; everything also works from the UI
python -m app.main            # → http://localhost:8000
```

Open <http://localhost:8000>, sign in with `ACCESS_PASSWORD`
(default `changeme` — change it in `.env`), press **Start agent**.

- Without credentials it still runs in **market-study + paper mode**
  (public tick data only — no token needed to watch and learn).
- Add credentials whenever you want account features or live trading.

### What you'll see

- Live status: Deriv link, balance, equity curve, daily P&L.
- One panel per symbol: price, spike age vs mean interval, P(spike ≤3/15
  ticks), auto-mode selection + reason, paper expectancy for **both** modes,
  model skill (Brier), regime-shift warnings.
- Activity feed + trade table (P = paper, L = live).
- **Start/Stop** switch — the agent resumes by itself after any restart.

---

## 4. Run 24/7 in the cloud (no hardware of yours)

Everything is containerized; any of these keeps it running while your
devices are off. **Pick a region close to Europe** (Deriv's infra) for the
lowest tick/order latency.

### Option A — Fly.io (free allowance, always-on) ★ recommended

```bash
# one-time: install flyctl from https://fly.io/docs/flyctl/install/
cd spike-agent
fly launch --no-deploy            # answers: Dockerfile detected, app name, region (e.g. cdg/ams)
fly volumes create spike_data --region <your-region> --size 1
fly secrets set ACCESS_PASSWORD="<strong-password>"   # Deriv creds can also be set later in the UI
fly deploy
```

Attach the volume to `/data` when prompted (flyctl asks for a mount during
`fly launch`, or add to `fly.toml`):

```toml
[mounts]
  source = "spike_data"
  destination = "/data"
```

Your app is then live at `https://<app-name>.fly.dev` — open it from any
device, sign in, press Start. The SQLite volume keeps the learned brain
across every redeploy.

### Option B — Render / Koyeb / Railway (push-button, but check “sleep”)

Create a **Web Service** → point at this repo/folder → it detects the
Dockerfile → add a **persistent disk** mounted at `/data` → set env vars →
deploy. ⚠️ Free tiers that **sleep after inactivity are not suitable** for
24/7 trading — verify the service never sleeps, or use the paid tier.

### Option C — cheapest reliable: any $3–5 VPS

```bash
git clone <your-repo> && cd spike-agent
cp .env.example .env && nano .env      # ACCESS_PASSWORD at minimum
docker compose up -d --build
docker compose logs -f                 # watch it learn
```

`restart: unless-stopped` brings it back after reboots and crashes.

---

## 5. Going live

1. Settings → App ID + PAT → *Fetch accounts* → pick your **real**
   account → enable **Live trading** → Save (the secure link re-establishes
   itself; the agent does not need a restart).
2. Defaults are deliberately conservative: $5 stake ×100 multiplier,
   stop −40% / take +60%, $20 max daily loss, 5-loss pause (30 min),
   entry threshold 0.50, one position per symbol.
3. Live orders are placed **only** when all of these hold:
   auto-mode selected that side · EV gate positive · risk manager clear ·
   balance above minimum.
4. Watch the *Activity* feed: entries/exits are logged with reasons
   (`pre_spike_exit`, `spike_caught`, `stop_loss`, `ev_block`, …).
5. Flip **Live trading** off at any moment — open positions remain yours
   and are adopted/managed on the next start.

---

## 6. Backtest on real ticks

```bash
python tools/backtest.py                          # BOOM1000 + CRASH1000 style defaults
python tools/backtest.py --symbol CRASH500 --ticks 30000
python tools/backtest.py --all                    # all six traded symbols
```

Replays real Deriv history through the **entire** agent pipeline (labeling,
hazard, models, strategy, EV gate, paper books) and prints per-mode
expectancy, win rates and model skill. No synthetic data.

## 7. Tests

```bash
python -m pytest tests/ -q      # 32 tests: labeler, hazard, model, strategy, EV gate, risk
```

---

## 8. Files

```
spike-agent/
├── app/
│   ├── main.py            FastAPI entrypoint (auto-resume on boot)
│   ├── api.py             REST API + session auth
│   ├── config.py          env + defaults
│   ├── store.py           SQLite: settings, trades, equity, spikes, models
│   ├── deriv/             new Options API: PAT→OTP auth, WS channels, history
│   └── engine/
│       ├── labeler.py     adaptive spike detection (6× rolling median move)
│       ├── hazard.py      online survival/hazard estimator
│       ├── features.py    scale-free per-tick features
│       ├── model.py       online logistic (RMSProp, replay, calibrated)
│       ├── strategy.py    drift/spike/auto + EV gate + exits
│       ├── risk.py        daily loss, streak pause, sizing
│       └── agent.py       brains + hub (the self-learning agent)
├── static/                dashboard (no build step, no CDN)
├── tests/                 pytest suite
├── tools/                 backtest + Deriv API probes (+ published schemas)
├── Dockerfile · docker-compose.yml · .env.example · requirements.txt
└── data/                  runtime SQLite + learned weights (volume in cloud)
```

---

## 9. Honest risk notes

- Synthetic indices are designed by Deriv; **no model can guarantee
  profits**. The agent's job is to measure the edge *after costs* and not
  trade when there isn't one — expect `observe` periods.
- Start on **demo**. Graduate to real money only after days of positive
  paper expectancy shown on your own dashboard.
- Free hosting tiers change; verify your host never sleeps the app.
- Protect your access password — anyone with it can start/stop the agent
  and read your balance.
- The PAT grants trade permission; revoke it from the Deriv dashboard at
  any time to freeze everything instantly.
