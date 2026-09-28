# DEPLOY.md — put Spike Agent in the cloud, free, 24/7

Goal: the agent runs on its own server, reachable from **any device's browser**,
with **no computer or phone of yours left running** and **no money spent**.

The path below uses **GitHub (free) + Render (free plan, no credit card)**.
Render builds the Docker image in the cloud and gives you a public URL like
`https://spike-agent-xxxx.onrender.com`.

Total time: ~20 minutes (5 min for Deriv, ~10 min for GitHub + Render).

---

## Step 1 — Get a Deriv API token (5 min, once)

1. Sign in at <https://developers.deriv.com>.
2. **Dashboard → Register application**
   - Type: **Personal Access Token (PAT)**
   - Name: anything, e.g. `spike-agent`
   - Submit → note the **App ID** shown for it.
3. **Dashboard → API tokens → Create token**
   - Scopes: **Read** + **Trade** (+ **Manage accounts** if offered)
   - Account: pick your **Demo** account first (you'll add the real one later)
   - Submit → **copy the token now** (shown only once; starts with `aADS...`).
4. Keep both handy: **App ID** and **Token**. You paste them into the
   dashboard's Settings page later — they never go into Git.

> These are NOT the legacy `app_id` from app.deriv.com. Only PAT-type app
> credentials from developers.deriv.com work with the new Options API.

## Step 2 — Push the code to GitHub (free)

The code lives in this `spike-agent/` folder. It is already a fresh local git
repo (initialized during this deployment). Create an empty repo on GitHub and
push:

1. Sign up / log in at <https://github.com> (free).
2. Click **+ → New repository**
   - Name: `spike-agent`
   - **Public** (Render's free plan connects to public repos easily; private
     works too if you authorize Render's GitHub app)
   - Do **NOT** tick "Add a README" — keep it empty.
   - Create repository.
3. Run these in this folder (I can run them for you if you prefer):

```bash
git remote add origin https://github.com/<your-username>/spike-agent.git
git push -u origin main
```

(Git Credential Manager will pop a browser login the first time.)
Stuck on credentials? Run `git push -u origin main` and choose **HTTPS**,
then sign in when the GitHub window opens.

## Step 3 — Deploy on Render (free, no credit card)

1. Sign up at <https://render.com> with your GitHub account (no card needed).
2. **New → Blueprint → Connect a repository** → pick `spike-agent`.
3. Render reads `render.yaml` automatically. When it asks for env vars:
   - `ACCESS_PASSWORD` → make up a strong password (this locks your dashboard)
   - `KEEPALIVE_URL` → leave blank for now (learned automatically) or paste
     `https://<service-name>.onrender.com/api/health`
4. **Apply**. Render builds the Docker image (~3–5 min) and starts it.
5. Open your URL: `https://spike-agent-xxxx.onrender.com` → log in with
   `ACCESS_PASSWORD`. You should see **Deriv: connecting / agent warming up**
   — the keep-alive now learns your public URL from this visit and pings it
   every 5 minutes so the free instance **never sleeps**.

> Verify the service is up from anywhere: `curl https://<your-url>/api/health`
> → `{"ok": true, ...}`.

## Step 4 — Connect your Deriv account (in the dashboard)

1. Open **⚙ Settings** on the dashboard (works from your phone too).
2. Paste **Deriv App ID** and **PAT token** (Step 1).
3. Pick **Demo** account mode and save. Status shows the Deriv connection
   turning green (the app exchanges the token for a 120 s WebSocket OTP —
   your token itself is never stored outside the server's database).

## Step 5 — Run it

1. Flip the agent switch to **RUNNING** — all 6 symbols start learning
   (first warmup pulls ~4,000 recent ticks per symbol, ~30–60 s).
2. Watch the equity curve and per-symbol skill on paper trades
   (play money) — leave it for a day or two.
3. When you're satisfied: Settings → **Live trading ON** (and switch
   `account_mode` to `real` when you're ready) — start small ($1–5 stake),
   keep the daily loss cap in place.

**It keeps running without you**: `AUTO_START=true` restarts the agent after
any redeploy/crash, learned weights persist in SQLite, and the keep-alive ping
stops the free instance from sleeping.

---

## Free-plan facts you should know

| Topic | What happens |
|---|---|
| Cost | $0 — Render free plan: 750 h/month (≈ one instance 24/7), 512 MB RAM |
| Sleep | Free instances sleep after ~15 min without traffic — **defeated** by the built-in 5-min keep-alive ping |
| Redeploys | Filesystem is wiped on every redeploy → trade history/brain reset; the agent **relearns from live ticks on boot** (~40 s warmup) and `AUTO_START` switches it back on. New code push → auto rebuild + restart. |
| Secrets | `ACCESS_PASSWORD`, PAT, App ID live only in Render env vars / server DB — never in Git (`.env` is git-ignored) |
| Data cap | Nothing special; Deriv market data is unlimited on the API |

## Other free hosts (same container, same keep-alive)

- **Koyeb** (no card): free instance = 512 MB, sleeps after 1 h idle — the
  same keep-alive defeats that. Deploy from the GitHub repo (Docker runtime).
- **Oracle Cloud Always Free VM** (credit card for signup, $0 forever):
  a real always-on server, no sleep, no 750 h cap — `docker compose up -d`
  via SSH. Best long-term if you have a card.
- **Fly.io**: **no free tier** since 2024 (~$5/mo) — config included
  (`fly.toml`) but not recommended for "free".

## Local test before deploying (optional)

```bash
pip install -r requirements.txt
ACCESS_PASSWORD=changeme python -m app.main
# → http://localhost:8000
python -m pytest tests/ -q     # 32 tests
```
