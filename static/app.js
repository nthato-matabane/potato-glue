/* Spike Agent dashboard — vanilla JS, no dependencies */
"use strict";

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v == null ? "—" : Number(v).toFixed(d));
const money = (v) => (v == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(Number(v)).toFixed(2));
const pct = (v) => (v == null ? "—" : (Number(v) * 100).toFixed(2) + "%");
const tstr = (ts) => new Date(ts * 1000).toLocaleTimeString();

let SETTINGS = null;
let RUNNING = false;
let statusTimer = null, equityTimer = null, tradesTimer = null, logTimer = null;
let equityData = { paper: [], live: [] };

const SYMBOL_META = {
  BOOM1000: "Boom 1000", CRASH1000: "Crash 1000",
  BOOM500: "Boom 500", CRASH500: "Crash 500",
  BOOM150N: "Boom 150", CRASH150N: "Crash 150",
  BOOM300N: "Boom 300", CRASH300N: "Crash 300",
  BOOM50: "Boom 50", CRASH50: "Crash 50",
  BOOM600: "Boom 600", CRASH600: "Crash 600",
  BOOM900: "Boom 900", CRASH900: "Crash 900",
};

// ---------------------------------------------------------------- auth ----

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("unauthorized");
  }
  hideLogin();
  return res.json();
}

function showLogin() { $("login").classList.remove("hidden"); }
function hideLogin() { $("login").classList.add("hidden"); }

$("login-btn").addEventListener("click", doLogin);
$("login-password").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
async function doLogin() {
  const pw = $("login-password").value;
  const r = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    credentials: "same-origin", body: JSON.stringify({ password: pw }),
  }).then((x) => x.json());
  if (r.ok) {
    $("login-error").textContent = "";
    hideLogin();
    startPolling();
  } else {
    $("login-error").textContent = "Wrong password";
  }
}

// --------------------------------------------------------------- status ----

async function refreshStatus() {
  let s;
  try { s = await api("/api/status"); }
  catch (e) { return; }
  SETTINGS = s.settings;
  RUNNING = s.running;

  // header
  const run = $("run-badge");
  run.textContent = s.running ? "RUNNING" : "STOPPED";
  run.className = "badge " + (s.running ? "on" : "off");
  $("toggle-btn").textContent = s.running ? "Stop agent" : "Start agent";
  $("toggle-btn").className = "btn " + (s.running ? "danger" : "primary");

  const conn = $("conn-badge");
  if (s.public_connected && (!s.live_enabled || s.auth_connected)) {
    conn.textContent = "Deriv ✅";
    conn.className = "badge on";
  } else if (s.public_connected) {
    conn.textContent = "ticks only";
    conn.className = "badge warn";
  } else {
    conn.textContent = s.running ? "connecting…" : "offline";
    conn.className = "badge dim";
  }

  $("balance-badge").textContent = s.live_enabled && s.live_balance
    ? `${s.currency} ${fmt(s.live_balance)}` : `paper ${money(s.paper_balance)}`;

  // stats
  $("stat-paper").textContent = money(s.paper_balance);
  $("stat-live").textContent = s.live_enabled ? `${s.currency} ${fmt(s.live_balance)}` : "paper mode";
  const dp = s.risk && s.risk.daily_profit;
  const dEl = $("stat-daily");
  dEl.textContent = money(dp);
  dEl.className = dp > 0 ? "pos" : (dp < 0 ? "neg" : "");
  $("stat-mode").textContent =
    (s.mode === "auto" ? "auto ⟡" : s.mode) + (s.live_enabled ? " · LIVE" : " · paper");
  $("stat-uptime").textContent = hms(s.uptime_s);

  // error banner
  const b = $("error-banner");
  if (s.error) { b.textContent = "⚠ " + s.error; b.classList.remove("hidden"); }
  else b.classList.add("hidden");

  renderSymbols(s.symbols || {}, s.last_signal || {});
  $("stat-trades").textContent = countLive(s.symbols || {});
  // notebook_status is served at the top level (api.py promotes it from
  // s.learning.notebook_status). Defend in case the shape changes.
  renderLearning(s.learning, s.error, s.notebook_status || (s.learning && s.learning.notebook_status));
  renderProfitability(s, equityData);
  window._snap = s;
}

function durNote(learning, ns) {
  // Durability note that's honest about the free path: the GitHub branch is
  // the durable store; /data is a working cache. "Backed up + restores on
  // boot" only when the token is on THIS container AND there's a GitHub
  // brain to restore from. Otherwise say exactly what's missing.
  if (!ns) return "";
  const githubThere = ns.github_brain_present;
  const tokenOnThis = ns.token_set && ns.available;
  if (githubThere && tokenOnThis) {
    const m = ns.github_meta || {};
    return `backed up to GitHub + restores on boot ☁️ · GitHub brain: <b>${m.trade_count || 0}</b> trades, <b>${m.total_updates || 0}</b> model updates, ${m.brains ? m.brains + " brains" : ""}${m.db_size ? " (" + (m.db_size / 1024 / 1024).toFixed(2) + " MB)" : ""} — protected from overwrite (push gate on)`;
  }
  if (!ns.token_set) {
    return `add <b>GITHUB_TOKEN</b> on Render (Environment) — a token with <i>repo</i> scope — so this container can back up and restore the brain. Currently no token on this container.`;
  }
  if (!githubThere) {
    return `token is on this container, but there's no brain on the <b>agent-memory</b> GitHub branch yet — back it up once from Settings so restores work on future boots.`;
  }
  if (ns.branch_exists && !tokenOnThis) {
    return `GitHub brain exists but this container doesn't have the token — it can't back up or restore. Redeploy once with the token set so the new container gets it.`;
  }
  return "";
}

function renderLearning(learning, hubError, ns) {
  const el = $("learning-body");
  if (!el) return;
  if (!learning || !learning.symbols || !Object.keys(learning.symbols).length) {
    const rows = [];
    if (hubError) rows.push(`<div class="learn-row">⚠ ${hubError}</div>`);
    else rows.push(`<div class="learn-row">starting — no brain data yet</div>`);
    if (ns) {
      const token = ns.token_set ? "✅ set" : "❌ not set";
      const avail = ns.available ? "✅ available" : "❌ unavailable";
      const branch = ns.branch_exists ? "✅ exists" : "❌ missing";
      rows.push(`<div class="learn-row dim">memory backend: token ${token} · repo ${ns.repo_slug || "—"} · notebook ${avail} · branch ${branch}</div>`);
      if (ns.github_brain_present) {
        const m = ns.github_meta || {};
        rows.push(`<div class="learn-row dim">GitHub brain: <b>${m.trade_count || 0}</b> trades, <b>${m.total_updates || 0}</b> model updates, ${m.brains ? m.brains + " brains" : "no brains yet"}${m.db_size ? " (" + (m.db_size / 1024 / 1024).toFixed(2) + " MB on GitHub)" : ""} — <b>protected from overwrite</b> (push gate on)`);
      } else {
        rows.push(`<div class="learn-row dim">no brain on the <b>agent-memory</b> GitHub branch yet</div>`);
      }
      const src = ns.restore_source;
      if (src === "github") rows.push(`<div class="learn-row dim">this boot: restored <b>from GitHub</b> ☁️</div>`);
      else if (src === "disk") rows.push(`<div class="learn-row dim">this boot: loaded from <b>/data</b> (persisted across this container's lifetime)</div>`);
      else if (src === "fresh") rows.push(`<div class="learn-row dim">this boot: <b>fresh start</b> — no brain on disk or GitHub to restore</div>`);
      rows.push(`<div class="learn-row">${durNote(learning, ns)}</div>`);
    } else {
      rows.push(`<div class="learn-row">Add <b>GITHUB_TOKEN</b> on Render (Environment) — a token with <i>repo</i> scope — so the agent can back up and restore its brain across redeploys.</div>`);
    }
    el.innerHTML = rows.join("");
    return;
  }
  const rows = Object.entries(learning.symbols).map(([sym, L]) => {
    const label = SYMBOL_META[sym] || sym;
    const bestSkill = Math.max(L.fast_skill || 0, L.slow_skill || 0);
    const skillTxt = bestSkill >= 0.2 ? `✅ sharp (score ${bestSkill.toFixed(2)}/1.0)`
      : bestSkill >= 0.05 ? `⏳ warming up (score ${bestSkill.toFixed(2)}/1.0)`
      : "⏳ studying…";
    const memory = L.memory_ok
      ? "✅ kept"
      : "⏳ still studying its first history";
    const gaps = L.hazard_gaps || 0;
    return `<div class="learn-row">
      <b>${label}</b>
      <span>studied <b>${(L.updates || 0).toLocaleString()}</b> lessons · seen <b>${L.spikes_seen || 0}</b> spikes · timing memory <b>${gaps}</b> spike gaps</span>
      <span class="dim">prediction skill: ${skillTxt}</span>
      <span class="dim">memory: ${memory}</span>
    </div>`;
  });
  // one durability row for the whole agent (not per-symbol), so the panel
  // stops saying "add GITHUB_TOKEN to survive redeploys" on every symbol
  // when the brain is already kept and the real question is the token.
  rows.push(`<div class="learn-row">${durNote(learning, ns)}</div>`);
  el.innerHTML = rows.join("");
}

function hms(sec) {
  if (!sec) return "—";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s2 = sec % 60;
  return (h ? h + "h " : "") + (h || m ? m + "m " : "") + s2 + "s";
}
function countLive(symbols) {
  let n = 0;
  for (const k in symbols) if (symbols[k].live) n++;
  return n;
}

// ---------------------------------------------------------- symbol cards ----

const cards = {};

function renderSymbols(syms, signals) {
  const grid = $("symbols");
  const keys = Object.keys(syms);
  // create missing cards in a stable order
  for (const key of Object.keys(SYMBOL_META)) {
    if (!syms[key] || cards[key]) continue;
    const el = document.createElement("div");
    el.className = "card";
    el.id = "card-" + key;
    el.innerHTML = cardHTML(SYMBOL_META[key]);
    grid.appendChild(el);
    cards[key] = el;
  }
  for (const key of keys) {
    const st = syms[key];
    const el = cards[key];
    if (!el) continue;
    updateCard(el, st, signals[key]);
  }
}

function cardHTML(name) {
  return `
  <div class="top">
    <span class="name">${name}</span>
    <span class="mode-chip mode-observe">observe</span>
  </div>
  <div class="top">
    <span class="price">—</span>
    <span class="pos-slot"></span>
  </div>
  <div class="meters">
    <div class="meter">
      <label>P(spike ≤3 ticks) <b class="pf">—</b></label>
      <div class="bar risk"><i style="width:0%"></i></div>
    </div>
    <div class="meter">
      <label>P(spike ≤15 ticks) <b class="ps">—</b></label>
      <div class="bar risk"><i style="width:0%"></i></div>
    </div>
    <div class="meter">
      <label>Spike age <b class="age">—</b></label>
      <div class="bar"><i style="width:0%"></i></div>
    </div>
    <div class="meter">
      <label>Age percentile <b class="agep">—</b></label>
      <div class="bar"><i style="width:0%"></i></div>
    </div>
  </div>
  <div class="exp">
    <div>drift exp.<b class="expd">—</b></div>
    <div>spike exp.<b class="exps">—</b></div>
    <div>spikes<b class="spk">—</b></div>
  </div>
  <div class="meta">
    <span class="mi">interval —</span>
    <span class="mf">model —</span>
    <span class="rs"></span>
  </div>
  <details class="model"><summary>model internals</summary>
    <div class="kv">
      <i>fast skill</i><b class="fs">—</b><i></i>
      <i>slow skill</i><b class="ssk">—</b><i></i>
      <i>fast upd</i><b class="fu">—</b><i></i>
      <i>slow upd</i><b class="su">—</b><i></i>
      <i>base rate</i><b class="br">—</b><i></i>
      <i>threshold</i><b class="thr">—</b><i></i>
    </div>
    <div class="reason" style="margin-top:6px"></div>
  </details>`;
}

function updateCard(el, st, sig) {
  el.querySelector(".name").textContent = st.label || st.symbol;
  el.querySelector(".price").textContent = st.price != null ? st.price : "—";

  const chip = el.querySelector(".mode-chip");
  const mode = st.auto_mode || "observe";
  chip.textContent = mode;
  chip.className = "mode-chip mode-" + mode;

  // Symbols without a spike labeler/hazard/models (e.g. volatility watch,
  // observe-only) don't have spike probabilities or spike age — show "—"
  // instead of misleading zeroes.
  const hasSpikeModel = !!(st.models && st.models.fast);
  if (hasSpikeModel) {
    setMeter(el, ".pf", st.p_fast, st.p_fast, v => (v * 100).toFixed(3) + "%");
    setMeter(el, ".ps", st.p_slow, st.p_slow, v => (v * 100).toFixed(2) + "%");
    const ageFrac = st.mean_interval ? Math.min(st.age / st.mean_interval, 1) : 0;
    setMeter(el, ".age", ageFrac, st.age + " / " + Math.round(st.mean_interval || 0));
    setMeter(el, ".agep", st.age_pct, st.age_pct, v => (v * 100).toFixed(0) + "%");
  } else {
    [".pf", ".ps", ".age", ".agep"].forEach(sel => {
      const b = el.querySelector(sel); if (b) b.textContent = "\u2014";
      const meter = b ? b.closest(".meter") : null;
      if (meter) { const bar = meter.querySelector("i"); if (bar) bar.style.width = "0%"; }
    });
  }

  const pd = st.paper_drift || {}, ps = st.paper_spike || {};
  const expEl = el.querySelector(".expd");
  expEl.textContent = hasSpikeModel ? `${money(pd.net)} (${pd.n || 0})` : "\u2014";
  expEl.className = (pd.net || 0) >= 0 ? "pos" : "neg";
  const exps = el.querySelector(".exps");
  exps.textContent = hasSpikeModel ? `${money(ps.net)} (${ps.n || 0})` : "\u2014";
  exps.className = (ps.net || 0) >= 0 ? "pos" : "neg";
  el.querySelector(".spk").textContent = hasSpikeModel ? (st.spikes || 0) : "\u2014";

  el.querySelector(".mi").textContent = "interval " + Math.round(st.mean_interval || 0);
  const m = (st.models || {}).fast || {};
  el.querySelector(".mf").textContent = hasSpikeModel ? ("skill " + (m.brier_skill != null ? m.brier_skill : "\u2014")) : "\u2014";
  el.querySelector(".rs").textContent = st.regime_shift ? "⚠ regime shift" : "";
  el.querySelector(".rs").style.color = st.regime_shift ? "var(--amber)" : "";

  el.querySelector(".fs").textContent = hasSpikeModel ? ((st.models || {}).fast || {}).brier_skill : "\u2014";
  el.querySelector(".ssk").textContent = hasSpikeModel ? ((st.models || {}).slow || {}).brier_skill : "\u2014";
  el.querySelector(".fu").textContent = hasSpikeModel ? ((st.models || {}).fast || {}).updates : "\u2014";
  el.querySelector(".su").textContent = hasSpikeModel ? ((st.models || {}).slow || {}).updates : "\u2014";
  el.querySelector(".br").textContent = hasSpikeModel ? ((st.models || {}).fast || {}).base_rate : "\u2014";
  el.querySelector(".thr").textContent = st.threshold != null ? st.threshold : "\u2014";
  el.querySelector(".reason").textContent = st.auto_reason || "";

  // position chip
  const slot = el.querySelector(".pos-slot");
  let chipHTML = "";
  if (st.live) {
    chipHTML = `<span class="tag L">LIVE ${st.live.type}</span>
      <b class="${st.live.profit >= 0 ? "pos" : "neg"}">${money(st.live.profit)}</b>`;
  } else if (st.paper && (st.paper.drift || st.paper.spike)) {
    const p = st.paper.drift || st.paper.spike;
    const which = st.paper.drift ? "paper·drift" : "paper·spike";
    chipHTML = `<span class="tag P">${which} ${p.side}</span>
      <b class="${p.pnl >= 0 ? "pos" : "neg"}">${money(p.pnl)}</b>`;
  }
  slot.innerHTML = chipHTML;
}

function setMeter(el, sel, frac, text, fmtFn) {
  const m = el.querySelector(sel).closest(".meter");
  m.querySelector("b").textContent = typeof fmtFn === "function" ? fmtFn(frac) : text;
  m.querySelector("i").style.width = Math.max(0, Math.min(1, frac || 0) * 100) + "%";
}

// ---------------------------------------------------------------- profitability ----

function fmtPnl(v) {
  return v == null ? "\u2014" : (v < 0 ? "-$" : "+$") + Math.abs(Number(v)).toFixed(2);
}

function profitVerdict(s, trend, todayPnl, bestSkill, nSharp, hasTodayActivity) {
  // Plain-English verdict from real data. We don't declare "profitable" until
  // the equity curve is trending up AND today is positive AND at least one
  // symbol is sharp. Everything else is an honest intermediate state.
  // hasTodayActivity is true when the paper equity curve has a non-null delta
  // since midnight (i.e. there has been today's activity) -- not a trade count.
  const running = !!(s && s.running);
  if (!running || !s || !s.symbols) return { text: "agent not running", cls: "neutral" };
  if (!hasTodayActivity) {
    if (bestSkill >= 0.2) return { text: "learning \u2014 brain is sharp but hasn\u2019t traded today yet", cls: "warm" };
    return { text: "learning \u2014 studying the market, no trades today yet", cls: "neutral" };
  }
  const up = trend == null ? null : trend > 0;
  const posDay = todayPnl > 0;
  const flatDay = Math.abs(todayPnl) < 0.20;
  if (up && posDay && nSharp >= 1) return { text: "early positive \u2014 paper up, today positive, brain sharp", cls: "good" };
  if (up && flatDay && nSharp >= 1) return { text: "learning \u2014 paper up, today roughly flat, brain sharp", cls: "warm" };
  if (up && !posDay && nSharp >= 1) return { text: "learning \u2014 paper up but today red, brain sharp", cls: "warm" };
  if (!up && posDay && nSharp >= 1) return { text: "mixed \u2014 paper down, today positive, brain sharp", cls: "warm" };
  if (!up && !posDay && nSharp >= 1) return { text: "learning \u2014 paper down, today red, brain still sharp", cls: "bad" };
  if (bestSkill >= 0.2) return { text: "learning \u2014 brain sharp but not pulling ahead yet", cls: "warm" };
  return { text: "learning \u2014 not yet profitable", cls: "bad" };
}

let _lastProfit = null;

function renderProfitability(s, equityData) {
  const el = $("profit-body");
  if (!el) return;
  if (!s || !equityData || !equityData.paper || equityData.paper.length < 2) {
    el.innerHTML = `<div class="profit-empty">starting \u2014 the agent hasn\u2019t run yet</div>`;
    _lastProfit = null;
    return;
  }
  const paper = s.paper_balance != null ? Number(s.paper_balance) : null;
  const mode = s.mode || "auto";
  const live = !!(s && s.live_enabled);

  // today\u2019s P&L from the paper equity curve (delta since midnight UTC).
  // Cross-checked against risk.daily_profit and today\u2019s trades below.
  const curve = equityData.paper;
  const todayStart = Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate()) / 1000;
  let todayVal = null, firstToday = null, lastToday = null;
  for (let i = curve.length - 1; i >= 0; i--) {
    if (curve[i].ts >= todayStart) { lastToday = curve[i].value; if (!firstToday) firstToday = curve[i].value; }
    else if (firstToday && !todayVal) { todayVal = curve[i].value; break; }
  }
  if (lastToday != null && firstToday != null) {
    // firstToday is the first point at/after midnight; use it as the day\u2019s start.
    todayVal = firstToday;
  }
  const todayPnl = (lastToday != null && todayVal != null) ? lastToday - todayVal : null;

  // equity trend: slope of the last ~40 paper points (a few minutes of ticks).
  const trendWindow = Math.min(curve.length, 40);
  const recent = curve.slice(-trendWindow);
  let trend = null;
  if (recent.length >= 2) {
    const xs = recent.map((p, i) => i);
    const ys = recent.map((p) => p.value);
    const n = xs.length;
    const mx = xs.reduce((a, b) => a + b, 0) / n;
    const my = ys.reduce((a, b) => a + b, 0) / n;
    let num = 0, den = 0;
    for (let i = 0; i < n; i++) { num += (xs[i] - mx) * (ys[i] - my); den += (xs[i] - mx) ** 2; }
    trend = den ? (num / den) : 0; // value change per point in the window
  }

  // per-symbol skill + recent paper P&L (last ~400 trades per mode) + recent
  // trade count. paper_drift/paper_spike are rolling deques (maxlen 400), so
  // they are "recent", NOT strictly "today". We label them honestly.
  const syms = s.symbols || {};
  const rows = Object.entries(syms).map(([key, st]) => {
    const label = (SYMBOL_META && SYMBOL_META[key]) || key;
    const fast = (st && st.models && st.models.fast) || {};
    const slow = (st && st.models && st.models.slow) || {};
    const bestSkill = Math.max(Number(fast.brier_skill || 0), Number(slow.brier_skill || 0));
    const skillCls = bestSkill >= 0.2 ? "sharp" : (bestSkill >= 0.05 ? "warming" : "studying");
    const skillTxt = bestSkill > 0 ? bestSkill.toFixed(2) : "\u2014";
    const pd = st && st.paper_drift ? st.paper_drift : {};
    const ps = st && st.paper_spike ? st.paper_spike : {};
    const pnl = ((pd && pd.net) || (ps && ps.net) || null);
    const n = ((pd && pd.n) || (ps && ps.n) || 0);
    return { key, label, bestSkill, skillCls, skillTxt, pnl, n };
  });
  // sort by absolute recent P&L desc, then by skill desc
  rows.sort((a, b) => Math.abs(b.pnl || 0) - Math.abs(a.pnl || 0) || b.bestSkill - a.bestSkill);

  // best skill + count of sharp symbols (used by the verdict)
  let bestSkill = 0, nSharp = 0;
  for (const r of rows) {
    if (r.bestSkill > bestSkill) bestSkill = r.bestSkill;
    if (r.bestSkill >= 0.2) nSharp++;
  }

  // Whether there has been today\u2019s paper activity: the equity curve has at
  // least one point today AND today\u2019s curve delta is non-zero (or we just
  // don\u2019t know yet). We don\u2019t claim a precise today trade count from the
  // rolling per-symbol deques (those are recent, not today).
  const hasTodayActivity = todayPnl != null;

  const v = profitVerdict(s, trend, todayPnl, bestSkill, nSharp, hasTodayActivity);

  // cross-check: risk.daily_profit and trades-based today P&L (if we had the
  // trades endpoint here). We don\u2019t fetch trades in renderProfitability to
  // avoid an extra request; the per-symbol n is the proxy for today\u2019s count.
  const riskDaily = (s && s.risk && s.risk.daily_profit != null) ? Number(s.risk.daily_profit) : null;

  el.innerHTML = `
    <div class="profit-head">
      <div class="profit-verdict ${v.cls}">${escapeHTML(v.text)}</div>
      <div class="profit-stat"><label>Paper balance</label><b>${paper != null ? money(paper) : "\u2014"}</b>${live ? `<div class="sub">live: ${s.currency||""} ${fmt(Number(s.live_balance||0))}</div>` : `<div class="sub">paper only</div>`}</div>
      <div class="profit-stat"><label>Today\u2019s P&amp;L (paper)</label><b>${todayPnl != null ? fmtPnl(todayPnl) : "\u2014"}</b>${riskDaily != null ? `<div class="sub">risk log: ${fmtPnl(riskDaily)}</div>` : ""}</div>
      <div class="profit-stat"><label>Equity trend</label><b>${trend == null ? "\u2014" : (trend > 0.001 ? "\u25b2 up" : (trend < -0.001 ? "\u25bc down" : "\u25ac flat"))}</b><div class="sub">last ${trendWindow} points</div></div>
    </div>
    <div class="profit-syms">
      <div class="ps-head"><span>Symbol</span><span>skill / recent paper P&amp;L</span><span>Recent</span><span>Trades</span></div>
      ${rows.length ? rows.map(r => `
        <div class="profit-row">
          <div class="sym">${escapeHTML(r.label)}</div>
          <div class="sk"><i>skill ${r.skillTxt}</i> ${r.skillCls === "sharp" ? "\u2705" : (r.skillCls === "warming" ? "\u23f3" : "\u231b")}</div>
          <div class="pnl">${r.pnl != null ? fmtPnl(r.pnl) : "\u2014"}</div>
          <div class="tt">${r.n || 0}</div>
        </div>`).join("")}
      ${rows.length === 0 ? `<div class="profit-empty">no symbols loaded yet</div>` : ""}
    </div>
  `;
  _lastProfit = { trend, todayPnl, bestSkill, nSharp, hasTodayActivity };
}

// ---------------------------------------------------------------- chart ----

async function refreshEquity() {
  try {
    const r = await api("/api/equity");
    equityData = r;
    drawChart();
    renderProfitability(window._snap, equityData);
  } catch (e) {}
}

function drawChart() {
  const c = $("equity-chart");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 600, h = 120;
  c.width = w * dpr; c.height = h * dpr;
  c.style.height = h + "px";
  const ctx = c.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const series = [
    { data: equityData.paper || [], color: "#4da3ff" },
    { data: equityData.live || [], color: "#2fd07f" },
  ];
  const all = series.flatMap((s) => s.data.map((p) => p.value));
  if (all.length < 2) {
    ctx.fillStyle = "#7f8ea8";
    ctx.font = "12px sans-serif";
    ctx.fillText("equity curve appears once the agent runs", 10, h / 2);
    return;
  }
  let min = Math.min(...all), max = Math.max(...all);
  if (max - min < 1e-9) { max += 1; min -= 1; }
  const pad = (max - min) * 0.08;

  // grid
  ctx.strokeStyle = "#1f2b40"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = 6 + (h - 14) * (i / 4);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  for (const s of series) {
    if (s.data.length < 2) continue;
    const t0 = s.data[0].ts, t1 = s.data[s.data.length - 1].ts;
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.8;
    ctx.beginPath();
    s.data.forEach((p, i) => {
      const x = t1 > t0 ? ((p.ts - t0) / (t1 - t0)) * (w - 4) + 2 : 2;
      const y = 6 + (h - 14) * (1 - (p.value - min + pad) / (max - min + 2 * pad));
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  }
}

// --------------------------------------------------------------- trades ----

async function refreshTrades() {
  try {
    const r = await api("/api/trades?limit=60");
    const tb = $("trades-table").querySelector("tbody");
    tb.innerHTML = r.trades.map((t) => `
      <tr>
        <td>${tstr(t.ts)}</td>
        <td>${SYMBOL_META[t.symbol] || t.symbol}</td>
        <td>${t.mode}</td>
        <td>${t.side}</td>
        <td><span class="tag ${t.live ? "L" : "P"}">${t.live ? "L" : "P"}</span></td>
        <td class="pnum ${t.profit >= 0 ? "pos" : "neg"}">${t.profit >= 0 ? "+" : ""}${money(t.profit).replace("$", "$")}</td>
        <td>${t.reason}</td>
      </tr>`).join("");
  } catch (e) {}
}

async function refreshLogs() {
  try {
    const r = await api("/api/logs?limit=50");
    const feed = $("activity");
    feed.innerHTML = r.logs.map((l) => `
      <div class="ev ${l.level === "trade" ? "trade" : (l.level === "error" ? "warn" : "")}">
        <span class="t">${tstr(l.ts)}</span><span>${escapeHTML(l.message)}</span>
      </div>`).join("");
  } catch (e) {}
}
function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --------------------------------------------------------------- control ----

$("toggle-btn").addEventListener("click", async () => {
  $("toggle-btn").disabled = true;
  try {
    if (RUNNING) await api("/api/agent/stop", { method: "POST" });
    else await api("/api/agent/start", { method: "POST" });
  } finally {
    $("toggle-btn").disabled = false;
    setTimeout(refreshStatus, 600);
  }
});

// ------------------------------------------------------------- settings ----

$("settings-btn").addEventListener("click", openSettings);
$("settings-close").addEventListener("click", () => $("settings").classList.add("hidden"));

async function openSettings() {
  const r = await api("/api/settings");
  const s = r.settings || {};
  SETTINGS = s;
  $("set-appid").value = s.deriv_app_id || "";
  $("set-pat").value = "";
  $("set-pat").placeholder = s.has_pat ? "•••• saved — paste to replace" : "paste Personal Access Token";
  $("set-live").checked = !!s.live_enabled;
  $("set-mode").value = s.mode || "auto";
  $("set-stake").value = s.stake_usd;
  $("set-mult").value = String(s.multiplier);
  $("set-dailyl").value = s.max_daily_loss_usd;
  $("set-sl").value = s.stop_loss_pct;
  $("set-tp").value = s.take_profit_pct;
  $("set-entry").value = s.entry_threshold;
  $("set-exit").value = s.exit_threshold;
  $("set-auton").value = s.auto_min_trades;

  // symbol chips
  const wrap = $("set-symbols");
  wrap.innerHTML = "";
  const chosen = new Set(s.symbols || []);
  for (const [sym, label] of Object.entries(SYMBOL_META)) {
    const l = document.createElement("label");
    l.innerHTML = `<input type="checkbox" value="${sym}" ${chosen.has(sym) ? "checked" : ""}> ${label}`;
    l.className = chosen.has(sym) ? "on" : "";
    l.querySelector("input").addEventListener("change", (e) => {
      l.classList.toggle("on", e.target.checked);
    });
    wrap.appendChild(l);
  }

  // account select
  const sel = $("set-account");
  sel.innerHTML = `<option value="">${s.account_id || "— select —"}</option>`;
  if (s.account_id) sel.value = s.account_id;
  $("settings").classList.remove("hidden");
  $("settings-msg").textContent = "";
}

$("fetch-accounts").addEventListener("click", async () => {
  const msg = $("settings-msg");
  msg.className = "msg"; msg.textContent = "Fetching accounts…";
  if ($("set-pat").value || $("set-appid").value) {
    // save creds first so the server can authenticate the call
    await api("/api/settings", { method: "POST", body: { settings: {
      deriv_app_id: $("set-appid").value,
      deriv_pat: $("set-pat").value || undefined,
    }}});
  }
  const r = await api("/api/accounts");
  if (!r.ok) {
    msg.className = "msg err";
    msg.textContent = r.error || "failed — check App ID + PAT (see README for setup)";
    return;
  }
  const sel = $("set-account");
  sel.innerHTML = "";
  for (const a of r.accounts) {
    const id = a.account_id || a.loginid || a.id || "";
    const cur = a.currency || "";
    const type = a.account_type || a.type || (id.includes("D") ? "demo" : "real");
    if (!id) continue;
    const o = document.createElement("option");
    o.value = id;
    o.textContent = `${id} · ${type} · ${cur}`;
    sel.appendChild(o);
  }
  if (!sel.options.length) {
    msg.className = "msg err";
    msg.textContent = "No Options accounts returned — sign up at developers.deriv.com first (see README)";
    return;
  }
  msg.className = "msg ok";
  msg.textContent = `Found ${sel.options.length} account(s) — pick one and Save`;
});

$("settings-save").addEventListener("click", async () => {
  const symbols = [...document.querySelectorAll("#set-symbols input:checked")].map((i) => i.value);
  const settings = {
    deriv_app_id: $("set-appid").value,
    mode: $("set-mode").value,
    live_enabled: $("set-live").checked,
    account_id: $("set-account").value,
    symbols: symbols.length ? symbols : undefined,
    stake_usd: parseFloat($("set-stake").value),
    multiplier: parseInt($("set-mult").value, 10),
    max_daily_loss_usd: parseFloat($("set-dailyl").value),
    stop_loss_pct: parseFloat($("set-sl").value),
    take_profit_pct: parseFloat($("set-tp").value),
    entry_threshold: parseFloat($("set-entry").value),
    exit_threshold: parseFloat($("set-exit").value),
    auto_min_trades: parseInt($("set-auton").value, 10),
  };
  const pat = $("set-pat").value;
  if (pat) settings.deriv_pat = pat;
  if (symbols.length && JSON.stringify(symbols) !== JSON.stringify((SETTINGS || {}).symbols)) {
    settings.symbols = symbols;
    // symbol universe changes need a restart of the agent
    settings.agent_running = false;
  }
  const msg = $("settings-msg");
  try {
    const r = await api("/api/settings", { method: "POST", body: { settings } });
    msg.className = "msg ok";
    msg.textContent = "Saved ✓" + (settings.live_enabled
      ? " — live trading ON: agent restarts its secure Deriv link"
      : "");
    $("set-pat").value = "";
    setTimeout(refreshStatus, 500);
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = "save failed";
  }
});

// ---------------------------------------------------------------- start ----

function startPolling() {
  if (statusTimer) return;
  refreshStatus();
  refreshEquity();
  refreshTrades();
  refreshLogs();
  statusTimer = setInterval(refreshStatus, 2000);
  equityTimer = setInterval(refreshEquity, 30000);
  tradesTimer = setInterval(refreshTrades, 15000);
  logTimer = setInterval(refreshLogs, 10000);
}

startPolling();
window.addEventListener("resize", drawChart);
