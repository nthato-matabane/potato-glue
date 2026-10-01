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
  renderLearning(s.learning, s.error);
  window._snap = s;
}

function renderLearning(learning, hubError) {
  const el = $("learning-body");
  if (!el) return;
  if (!learning || !learning.symbols || !Object.keys(learning.symbols).length) {
    el.innerHTML = `<div class="learn-row">${
      hubError ? "⚠ " + hubError : "starting — no brain data yet"}</div>`;
    return;
  }
  const rows = Object.entries(learning.symbols).map(([sym, L]) => {
    const label = SYMBOL_META[sym] || sym;
    const bestSkill = Math.max(L.fast_skill || 0, L.slow_skill || 0);
    const skillTxt = bestSkill >= 0.2 ? `✅ sharp (score ${bestSkill.toFixed(2)}/1.0)`
      : bestSkill >= 0.05 ? `⏳ warming up (score ${bestSkill.toFixed(2)}/1.0)`
      : "⏳ studying…";
    const memory = L.memory_ok
      ? "✅ kept" + (learning.notebook_on ? " + backed up to GitHub ☁️" : " — add GITHUB_TOKEN on Render to survive redeploys ☁️")
      : "⏳ still studying its first history";
    const gaps = L.hazard_gaps || 0;
    return `<div class="learn-row">
      <b>${label}</b>
      <span>studied <b>${(L.updates || 0).toLocaleString()}</b> lessons · seen <b>${L.spikes_seen || 0}</b> spikes · timing memory <b>${gaps}</b> spike gaps</span>
      <span class="dim">prediction skill: ${skillTxt}</span>
      <span class="dim">memory: ${memory}</span>
    </div>`;
  });
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

  setMeter(el, ".pf", st.p_fast, st.p_fast, v => (v * 100).toFixed(3) + "%");
  setMeter(el, ".ps", st.p_slow, st.p_slow, v => (v * 100).toFixed(2) + "%");
  const ageFrac = st.mean_interval ? Math.min(st.age / st.mean_interval, 1) : 0;
  setMeter(el, ".age", ageFrac, st.age + " / " + Math.round(st.mean_interval || 0));
  setMeter(el, ".agep", st.age_pct, st.age_pct, v => (v * 100).toFixed(0) + "%");

  const pd = st.paper_drift || {}, ps = st.paper_spike || {};
  const expEl = el.querySelector(".expd");
  expEl.textContent = `${money(pd.net)} (${pd.n || 0})`;
  expEl.className = (pd.net || 0) >= 0 ? "pos" : "neg";
  const exps = el.querySelector(".exps");
  exps.textContent = `${money(ps.net)} (${ps.n || 0})`;
  exps.className = (ps.net || 0) >= 0 ? "pos" : "neg";
  el.querySelector(".spk").textContent = st.spikes || 0;

  el.querySelector(".mi").textContent = "interval " + Math.round(st.mean_interval || 0);
  const m = (st.models || {}).fast || {};
  el.querySelector(".mf").textContent = "skill " + (m.brier_skill != null ? m.brier_skill : "—");
  el.querySelector(".rs").textContent = st.regime_shift ? "⚠ regime shift" : "";
  el.querySelector(".rs").style.color = st.regime_shift ? "var(--amber)" : "";

  el.querySelector(".fs").textContent = ((st.models || {}).fast || {}).brier_skill;
  el.querySelector(".ssk").textContent = ((st.models || {}).slow || {}).brier_skill;
  el.querySelector(".fu").textContent = ((st.models || {}).fast || {}).updates;
  el.querySelector(".su").textContent = ((st.models || {}).slow || {}).updates;
  el.querySelector(".br").textContent = ((st.models || {}).fast || {}).base_rate;
  el.querySelector(".thr").textContent = st.threshold;
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

// ---------------------------------------------------------------- chart ----

async function refreshEquity() {
  try {
    const r = await api("/api/equity");
    equityData = r;
    drawChart();
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
