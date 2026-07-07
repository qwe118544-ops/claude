/* polyweather UI: city tabs, PMF chart, trajectory chart, feeds, SSE refresh */
"use strict";

let cities = [];
let current = null;
let pmfChart, trajChart;

const $ = (id) => document.getElementById(id);

async function jget(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

function fmtTs(iso) {
  return iso ? iso.slice(11, 16) + "Z" : "";
}

async function init() {
  cities = await jget("/api/cities");
  const tabs = $("tabs");
  tabs.innerHTML = "";
  cities.forEach((c) => {
    const b = document.createElement("button");
    b.textContent = `${c.name} ${c.local_time} · ${c.phase}`;
    b.dataset.key = c.key;
    b.onclick = () => select(c.key);
    tabs.appendChild(b);
  });
  pmfChart = echarts.init($("pmfChart"), null, { renderer: "canvas" });
  trajChart = echarts.init($("trajChart"), null, { renderer: "canvas" });
  window.addEventListener("resize", () => { pmfChart.resize(); trajChart.resize(); });
  select(cities[0].key);
  connectSSE();
  setInterval(refreshHealth, 30000);
  refreshHealth();
  setInterval(() => { if (current) refresh(current); }, 60000); // safety net
}

function select(key) {
  current = key;
  document.querySelectorAll("nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.key === key));
  refresh(key);
}

async function refresh(key) {
  let st;
  try { st = await jget("/api/state/" + key); }
  catch (e) { console.error(e); return; }
  renderStatus(st);
  renderPmf(st);
  renderTraj(st);
  renderFeeds(st);
}

function renderStatus(st) {
  const p = st.pricing;
  $("dayCard").innerHTML = `
    <h3>${st.name} — ${st.climate_date}</h3>
    <div class="big">${p && p.run_max_int != null ? p.run_max_int + "°C" : "–"}
      <span class="muted">running max</span></div>
    <div class="muted">local ${st.local_time} · phase ${st.phase}
      ${p ? "· priced " + fmtTs(p.ts) : "· no pricing yet"}</div>
    ${p && p.inputs ? `<div class="muted">last obs ${p.inputs.last_temp ?? "–"}°C ·
      hours left in window ${p.inputs.hours_left}</div>` : ""}`;

  let mhtml = "<h3>Polymarket top buckets (display only)</h3>";
  const bs = (st.market && st.market.buckets) || [];
  if (!bs.length) mhtml += `<div class="muted">no market data</div>`;
  bs.forEach((b) => {
    mhtml += `<div class="mktline"><span>${b.title}</span>
      <span>mkt <b>${(b.yes * 100).toFixed(0)}%</b>
      ${b.ours != null ? " · ours <b>" + (b.ours * 100).toFixed(0) + "%</b>" : ""}</span></div>`;
  });
  if (st.market && st.market.ts)
    mhtml += `<div class="muted">as of ${fmtTs(st.market.ts)}</div>`;
  $("marketCard").innerHTML = mhtml;

  let shtml = "<h3>Signals</h3>";
  if (p && p.signals) {
    const s = p.signals;
    const b = s.breeze_front || {};
    shtml += `<span class="badge ${b.arrived || b.confidence >= 0.5 ? "on" : ""}">
      breeze ${b.arrived ? "ARRIVED" : (b.confidence * 100).toFixed(0) + "%" +
      (b.eta_min != null ? " ETA " + b.eta_min + "m" : "")}</span>`;
    shtml += `<span class="badge ${s.peak_passed.p >= 0.8 ? "on" : ""}">
      peak passed ${(s.peak_passed.p * 100).toFixed(0)}%</span>`;
    const c = s.ceiling || {};
    if (c.cap_850 != null)
      shtml += `<span class="badge">ceiling ${c.cap_850}°C</span>`;
    const r = s.residual || {};
    shtml += `<span class="badge ${Math.abs(r.mean_residual) >= 0.7 ? "on" : ""}">
      resid ${r.mean_residual > 0 ? "+" : ""}${r.mean_residual}°C</span>`;
    if (s.momentum && s.momentum.overall)
      shtml += `<span class="badge on">model drift ${s.momentum.overall > 0 ? "+" : ""}${s.momentum.overall}°C/run</span>`;
  } else shtml += `<div class="muted">–</div>`;
  $("signalCard").innerHTML = shtml;
}

function renderPmf(st) {
  const p = st.pricing;
  if (!p) { pmfChart.clear(); return; }
  const entries = Object.entries(p.int_pmf).map(([k, v]) => [parseInt(k), v])
    .sort((a, b) => a[0] - b[0]).filter(([_, v]) => v > 0.002);
  const mktByInt = {};
  ((st.market && st.market.buckets) || []).forEach((b) => {
    if (b.lo != null && b.lo === b.hi) mktByInt[b.lo] = b.yes;
  });
  pmfChart.setOption({
    backgroundColor: "transparent",
    grid: { left: 45, right: 15, top: 30, bottom: 25 },
    legend: { textStyle: { color: "#8a93a3" }, top: 0 },
    xAxis: { type: "category", data: entries.map(([k]) => k + "°C"),
      axisLabel: { color: "#8a93a3" } },
    yAxis: { type: "value", max: 1, axisLabel: { color: "#8a93a3", formatter: (v) => (v * 100) + "%" },
      splitLine: { lineStyle: { color: "#262b36" } } },
    series: [
      { name: "ours", type: "bar", data: entries.map(([_, v]) => +v.toFixed(4)),
        itemStyle: { color: "#4da3ff" }, barMaxWidth: 40 },
      { name: "market (top buckets)", type: "scatter", symbolSize: 12,
        itemStyle: { color: "#f2b544" },
        data: entries.map(([k]) => mktByInt[k] != null ? +mktByInt[k] : null) },
    ],
    tooltip: { trigger: "axis", valueFormatter: (v) => v != null ? (v * 100).toFixed(1) + "%" : "–" },
  });
}

function renderTraj(st) {
  const obs = st.obs.map(([t, v]) => [t + "Z", v]);
  const fast = st.fast_obs.map(([t, v]) => [t + "Z", v]);
  let runMax = -99; const run = st.obs.map(([t, v]) => {
    runMax = Math.max(runMax, Math.round(v)); return [t + "Z", runMax];
  });
  const series = [
    { name: "METAR obs", type: "line", data: obs, showSymbol: true, symbolSize: 5,
      lineStyle: { width: 2, color: "#4da3ff" }, itemStyle: { color: "#4da3ff" } },
    { name: "fast obs (10min)", type: "line", data: fast, showSymbol: false,
      lineStyle: { width: 1, color: "#58c77a", opacity: 0.8 }, itemStyle: { color: "#58c77a" } },
    { name: "running max (int)", type: "line", data: run, step: "end", showSymbol: false,
      lineStyle: { width: 1, type: "dashed", color: "#f2b544" }, itemStyle: { color: "#f2b544" } },
  ];
  const palette = ["#b48ce0", "#e07a9b", "#7ac0e0", "#e0c47a", "#8ce0b4", "#e08c7a"];
  let i = 0;
  for (const [model, curve] of Object.entries(st.model_curves || {})) {
    series.push({
      name: model.replace(/_/g, " "), type: "line", showSymbol: false,
      data: curve.map(([t, v]) => [t + ":00", v]),
      lineStyle: { width: 1, opacity: 0.55, color: palette[i % palette.length] },
      itemStyle: { color: palette[i % palette.length] },
    });
    i++;
  }
  const cap = st.pricing && st.pricing.signals && st.pricing.signals.ceiling
    ? st.pricing.signals.ceiling.cap_850 : null;
  trajChart.setOption({
    backgroundColor: "transparent",
    grid: { left: 45, right: 15, top: 30, bottom: 40 },
    legend: { textStyle: { color: "#8a93a3", fontSize: 11 }, top: 0, type: "scroll" },
    xAxis: { type: "time", axisLabel: { color: "#8a93a3" } },
    yAxis: { type: "value", scale: true,
      axisLabel: { color: "#8a93a3", formatter: "{value}°C" },
      splitLine: { lineStyle: { color: "#262b36" } } },
    series,
    tooltip: { trigger: "axis" },
    ...(cap != null ? {
      graphic: [{ type: "text", right: 20, top: 34,
        style: { text: "ceiling " + cap + "°C", fill: "#ff6b6b", fontSize: 11 } }],
    } : {}),
  }, true);
  if (cap != null) {
    trajChart.setOption({ series: [{ name: "ceiling", type: "line", markLine: {
      silent: true, symbol: "none", data: [{ yAxis: cap }],
      lineStyle: { color: "#ff6b6b", type: "dotted" },
      label: { show: false } }, data: [] }] });
  }
}

function renderFeeds(st) {
  $("explainFeed").innerHTML = (st.explain_feed || []).map((e) =>
    `<li><span class="ts">${fmtTs(e.ts)}</span>${e.lines.join(" · ")}</li>`).join("")
    || `<li class="muted">no changes yet</li>`;
  $("signalFeed").innerHTML = (st.signal_events || []).map((e) =>
    `<li><span class="ts">${fmtTs(e.ts)}</span>${e.kind}
     ${e.active ? "<b>ACTIVE</b>" : "cleared"}
     ${e.payload && e.payload.eta_min != null ? "ETA " + e.payload.eta_min + "m" : ""}
     ${e.payload && e.payload.confidence != null ? "conf " + (e.payload.confidence * 100).toFixed(0) + "%" : ""}</li>`).join("")
    || `<li class="muted">none today</li>`;
}

async function refreshHealth() {
  try {
    const h = await jget("/api/health");
    $("healthBar").innerHTML = Object.entries(h).map(([src, d]) => {
      const lag = d.last_lag_s != null ? Math.round(d.last_lag_s / 60) + "m lag" : "";
      const stale = d.errors_6h > 3 || d.last_ok == null;
      return `<div class="src ${stale ? "stale" : ""}"><b>${src}</b>
        ${lag} · ${d.errors_6h} err/6h</div>`;
    }).join("") || `<div class="muted">no fetches yet</div>`;
  } catch (e) { /* server restarting */ }
}

function connectSSE() {
  const es = new EventSource("/stream");
  es.onopen = () => $("conn").classList.add("ok");
  es.onerror = () => $("conn").classList.remove("ok");
  es.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.city === current &&
          (msg.type === "pricing" || msg.type === "obs" || msg.type === "market")) {
        refresh(current);
      }
    } catch (e) { /* keepalive */ }
  };
}

init();
