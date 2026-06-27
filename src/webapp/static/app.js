"use strict";
const $ = (id) => document.getElementById(id);
const COLORS = { live: "#2BB8E6", standby: "#F2A93B", cash: "#8A93A3",
                 drawdown: "#E8806B", paper: "#ECEFF3", edge: "#38424F" };
const REGIME_COLOR = { primary: COLORS.live, candidate: COLORS.standby, cash: COLORS.cash };
const REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const pct = (x) => (x * 100).toFixed(1) + "%";
let chart;

// ---- strategy field toggle ----
function syncRuleFields() {
  const cross = $("rule").value === "ma_cross";
  $("cross-fields").classList.toggle("hidden", !cross);
  $("ma-field").classList.toggle("hidden", cross);
}
$("rule").addEventListener("change", syncRuleFields);

// ---- cache status poll ----
async function pollStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    const lamp = $("cache-lamp").querySelector(".lamp");
    if (s.state === "ready") {
      lamp.className = "lamp ready";
      $("cache-text").textContent = `cache READY · ${s.total || ""}`.trim();
      $("run").disabled = false;
    } else {
      lamp.className = "lamp warming";
      $("cache-text").textContent = `Warming the cache — ${s.loaded} / ${s.total}`;
      $("run").disabled = true;
      setTimeout(pollStatus, 1500);
    }
  } catch { setTimeout(pollStatus, 2000); }
}

// ---- build request body ----
function buildBody() {
  const rule = $("rule").value;
  const body = { ticker: $("ticker").value.trim().toUpperCase(), rule,
                 mode: $("mode").value, sort: $("sort").value,
                 exclude_etf: $("exclude-etf").checked,
                 require_full_history: $("full-history").checked,
                 sp500_only: $("sp500-only").checked };
  if (rule === "ma_cross") { body.fast = +$("fast").value; body.slow = +$("slow").value; }
  else { body.ma = +$("ma").value; }
  return body;
}

// ---- run a scan ----
$("run").addEventListener("click", runScan);
async function runScan() {
  const body = buildBody();
  if (!body.ticker) { setPlate("idle", "Pick a ticker and run a scan."); return; }
  $("run").disabled = true;
  $("progress").classList.remove("hidden");
  setProgress(0, 0);
  let res;
  try {
    res = await fetch("/api/scan", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch { return failScan("Network error — try again."); }
  if (res.status === 422) return failScan("Check the strategy params (e.g. fast < slow).");
  if (res.status === 503) return failScan("Warming the cache — try again shortly.");
  if (!res.ok) return failScan("Scan failed to start.");
  const { job_id } = await res.json();
  streamJob(job_id);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function streamJob(jobId) {
  const ev = new EventSource(`/api/scan/${jobId}/events`);
  let finished = false;
  ev.addEventListener("progress", (m) => {
    const d = JSON.parse(m.data); setProgress(d.done, d.total);
  });
  ev.addEventListener("result", (m) => {
    finished = true; ev.close();
    const d = JSON.parse(m.data);
    if (d.status !== "done") return failScan(d.error || "Scan did not complete.");
    loadAndRender(jobId);
  });
  ev.addEventListener("error", () => {
    if (finished || ev.readyState === EventSource.CLOSED) return;
    // The SSE dropped (network blip / ALB idle timeout on a long scan). The scan
    // is still running server-side — don't give up, fall back to polling /result.
    finished = true; ev.close();
    pollResult(jobId);
  });
}

// Poll /result until the job finishes server-side, then render. Keeps the
// progress bar moving so a dropped SSE doesn't look frozen.
async function pollResult(jobId) {
  for (let i = 0; i < 160; i++) {                    // ~6-8 min ceiling
    let r;
    try { r = await fetch(`/api/scan/${jobId}/result`); }
    catch { await sleep(3000); continue; }
    if (r.status === 410) return failScan("服务已更新，请重新扫描。");
    const j = await r.json();
    if (j.status === "done") { setProgress(j.done, j.total); return loadAndRender(jobId); }
    if (j.status === "error") return failScan(j.error || "扫描失败，请重试。");
    setProgress(j.done || 0, j.total || 0);
    await sleep(2500);
  }
  failScan("扫描超时，请重试。");
}

async function loadAndRender(jobId) {
  $("progress").classList.add("hidden");
  $("run").disabled = false;
  try {                                              // gzip-eligible JSON payload
    const full = await (await fetch(`/api/scan/${jobId}/result`)).json();
    if (!full || full.status === "unknown_job") return failScan("服务已更新，请重新扫描。");
    render(full.result, jobId);
  } catch { failScan("结果加载失败，请重试。"); }
}

function failScan(msg) {
  $("progress").classList.add("hidden");
  $("run").disabled = false;
  setPlate("idle", msg);
}
function setProgress(done, total) {
  const frac = total ? done / total : 0;
  $("progress").querySelector("i").style.width = (frac * 100).toFixed(1) + "%";
  $("progress").querySelector(".readout").textContent =
    `SCANNING ${done.toLocaleString()} / ${total.toLocaleString()}`;
}
function setPlate(kind, text) {
  const p = $("state-plate");
  p.className = "plate " + kind + (REDUCE ? "" : " powering");
  p.querySelector(".plate-text").textContent = text;
}

// ---- render results ----
const G = { jobId: null, result: null };
const zhRegime = (s) => ({ primary: "在场(持主标的)", candidate: "停泊(备胎)", cash: "现金" }[s] || s);

function render(r, jobId) {
  G.result = r; G.jobId = jobId;
  const rec = r.recommendation;
  const primary = $("ticker").value.trim().toUpperCase();
  if (rec.state === "in-market") setPlate("live", `LIVE — 持有 ${primary}`);
  else setPlate("parked", rec.top ? `STANDBY — 停泊 ${rec.top}` : "STANDBY — 无合格备胎");
  renderGauges(r.ranked[0]);
  renderTable(r.ranked);
  if (r.ranked.length) {                 // default hero = rank-1, from the scan result
    renderChart(r.curves, r.ranked[0].ticker);
    markSelected(r.ranked[0].ticker);
  }
}

function renderGauges(top) {
  if (!top) { $("gauges").innerHTML = ""; return; }
  $("gauges").innerHTML =
    `<div>抗脆弱<b>${(top.afscore * 100).toFixed(1)}</b></div>
     <div>相关性<b>${top.corr_off.toFixed(2)}</b></div>
     <div>停泊%<b>${(top.park_return * 100).toFixed(0)}</b></div>`;
}

// Chinese, click-to-select backups table
const COLS = [
  ["#", (r, i) => i + 1], ["标的", (r) => r.ticker],
  ["空仓%", (r) => (r.off_frac * 100).toFixed(0)], ["抗脆弱", (r) => (r.afscore * 100).toFixed(1)],
  ["年化(裸)", (r) => pct(r.cagr_n)], ["回撤(裸)", (r) => pct(r.max_dd_n)],
  ["Calmar", (r) => r.calmar_n.toFixed(2)], ["Sharpe", (r) => r.sharpe_n.toFixed(2)],
  ["相关性", (r) => r.corr_off.toFixed(2)], ["停泊%", (r) => (r.park_return * 100).toFixed(0)],
  ["年化(滤)", (r) => pct(r.cagr_f)], ["回撤(滤)", (r) => pct(r.max_dd_f)]];

function renderTable(rows) {
  if (!rows.length) { $("table").innerHTML = `<p class="muted">无合格备胎。</p>`; return; }
  const head = "<thead><tr>" + COLS.map((c) => `<th>${c[0]}</th>`).join("") + "</tr></thead>";
  const body = "<tbody>" + rows.map((r, i) =>
    `<tr data-ticker="${r.ticker}">` + COLS.map((c) => `<td>${c[1](r, i)}</td>`).join("") + "</tr>")
    .join("") + "</tbody>";
  $("table").innerHTML = `<table>${head}${body}</table>`;
  $("table").querySelectorAll("tbody tr").forEach((tr) =>
    tr.addEventListener("click", () => selectBackup(tr.dataset.ticker)));
}

function markSelected(ticker) {
  $("table").querySelectorAll("tbody tr").forEach((tr) =>
    tr.classList.toggle("sel", tr.dataset.ticker === ticker));
}

// Click a backup row -> fetch its combined equity curve and redraw the chart.
async function selectBackup(ticker) {
  markSelected(ticker);
  let res;
  try {
    res = await fetch(`/api/scan/${G.jobId}/curve?ticker=${encodeURIComponent(ticker)}`);
  } catch { return; }                    // transient network: keep the current chart
  if (res.status === 410) {              // job gone (service restarted/redeployed)
    setPlate("idle", "服务已更新，请重新扫描。");
    return;
  }
  if (!res.ok) return;
  const j = await res.json();
  if (j && j.curves) renderChart(j.curves, ticker);
}

// ---- 3-panel chart: one hero backup (equity, log) / regime ribbon / drawdown ----
function renderChart(curves, heroTicker) {
  if (!chart) chart = echarts.init($("chart"), null, { renderer: "canvas" });
  const dates = curves.dates;
  const hero = curves.picks[0];          // the charted backup's combined equity
  const dateIdx = new Map(dates.map((d, i) => [d, i]));
  const regimeRows = curves.regime.map((s) => ({ value: [dateIdx.get(s.start), dateIdx.get(s.end)] }));
  const regimeStates = curves.regime.map((s) => s.state);
  // Regime ribbon: fill/pattern by dataIndex (api.value on a value axis coerces the
  // state to null); each state also carries a non-color channel for colorblindness.
  const renderRibbon = (params, api) => {
    const x0 = api.coord([api.value(0), 0])[0];
    const x1 = api.coord([api.value(1), 0])[0];
    const cs = params.coordSys;
    const x = Math.min(x0, x1), w = Math.max(1, Math.abs(x1 - x0));
    const rect = { x, y: cs.y, width: w, height: cs.height };
    const state = regimeStates[params.dataIndex];
    if (state === "primary")
      return { type: "rect", shape: rect, style: { fill: REGIME_COLOR.primary } };
    if (state === "cash")
      return { type: "rect", shape: rect,
        style: { fill: "transparent", stroke: REGIME_COLOR.cash, lineWidth: 1, lineDash: [2, 2] } };
    const hatch = [];
    for (let hx = x - cs.height; hx < x + w; hx += 6)
      hatch.push({ type: "line",
        shape: { x1: hx, y1: cs.y + cs.height, x2: hx + cs.height, y2: cs.y },
        style: { stroke: "#10151c", lineWidth: 1 } });
    return { type: "group", clipPath: { type: "rect", shape: rect },
      children: [{ type: "rect", shape: rect, style: { fill: REGIME_COLOR.candidate } }, ...hatch] };
  };

  const heroArea = new echarts.graphic.LinearGradient(0, 0, 0, 1, [
    { offset: 0, color: "rgba(43,184,230,0.30)" }, { offset: 1, color: "rgba(43,184,230,0.015)" }]);
  const ddArea = new echarts.graphic.LinearGradient(0, 0, 0, 1, [
    { offset: 0, color: "rgba(232,128,107,0.05)" }, { offset: 1, color: "rgba(232,128,107,0.34)" }]);

  const heroSeries = hero ? [{
    name: heroTicker, type: "line", xAxisIndex: 0, yAxisIndex: 0, data: hero.equity,
    showSymbol: false, smooth: false, sampling: "lttb", z: 4,
    lineStyle: { width: 2.6, color: COLORS.live }, areaStyle: { color: heroArea } }] : [];

  chart.setOption({
    animation: !REDUCE, animationDuration: 650, backgroundColor: "transparent",
    textStyle: { fontFamily: "Spline Sans Mono, monospace", color: COLORS.cash },
    grid: [{ left: 60, right: 20, top: 14, height: 252 },
           { left: 60, right: 20, top: 282, height: 18 },
           { left: 60, right: 20, top: 318, height: 100 }],
    axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: COLORS.edge, type: "dashed" } },
    tooltip: {
      trigger: "axis", axisPointer: { type: "cross", label: { backgroundColor: "#1a2330" } },
      backgroundColor: "rgba(16,21,28,0.95)", borderColor: COLORS.edge, borderWidth: 1,
      textStyle: { color: COLORS.paper, fontFamily: "Spline Sans Mono, monospace", fontSize: 12 },
      valueFormatter: (v) => (v == null ? "" : (Math.abs(v) >= 5 ? v.toFixed(0) : v.toFixed(2))) },
    xAxis: [
      { type: "category", data: dates, gridIndex: 0, boundaryGap: false, axisTick: { show: false },
        axisLabel: { show: false }, axisLine: { lineStyle: { color: COLORS.edge } } },
      { type: "category", data: dates, gridIndex: 1, boundaryGap: false, axisTick: { show: false },
        axisLabel: { show: false }, axisLine: { show: false } },
      { type: "category", data: dates, gridIndex: 2, boundaryGap: false, axisTick: { show: false },
        axisLabel: { color: COLORS.cash, fontSize: 10, hideOverlap: true },
        axisLine: { lineStyle: { color: COLORS.edge } } }],
    yAxis: [
      { type: "log", gridIndex: 0, axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { formatter: (v) => v + "×", color: COLORS.cash, fontSize: 10 },
        splitLine: { lineStyle: { color: "rgba(56,66,79,0.35)" } } },
      { type: "value", gridIndex: 1, show: false, min: 0, max: 1 },
      { type: "value", gridIndex: 2, max: 0, splitNumber: 3, axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { formatter: (v) => v + "%", color: COLORS.cash, fontSize: 10 },
        splitLine: { lineStyle: { color: "rgba(56,66,79,0.25)" } } }],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2] },
      { type: "slider", xAxisIndex: [0, 1, 2], bottom: 2, height: 16, borderColor: "transparent",
        backgroundColor: "rgba(255,255,255,0.02)", fillerColor: "rgba(43,184,230,0.12)",
        handleStyle: { color: COLORS.live, borderColor: COLORS.live },
        moveHandleStyle: { color: COLORS.edge },
        dataBackground: { lineStyle: { color: COLORS.edge }, areaStyle: { color: "rgba(56,66,79,0.25)" } },
        selectedDataBackground: { lineStyle: { color: COLORS.live }, areaStyle: { color: "rgba(43,184,230,0.12)" } },
        textStyle: { color: COLORS.cash, fontSize: 9 } }],
    series: [
      { name: "主标的 B&H", type: "line", xAxisIndex: 0, yAxisIndex: 0, data: curves.primary_buy_hold,
        showSymbol: false, z: 2, lineStyle: { width: 1, color: "rgba(236,239,243,0.45)", type: "dashed" } },
      ...heroSeries,
      { name: "regime", type: "custom", xAxisIndex: 1, yAxisIndex: 1, clip: true,
        renderItem: renderRibbon, encode: { x: [0, 1] }, data: regimeRows,
        tooltip: { formatter: (p) => `状态: ${zhRegime(regimeStates[p.dataIndex])}` } },
      { name: "回撤%", type: "line", xAxisIndex: 2, yAxisIndex: 2, data: curves.rank1_drawdown,
        showSymbol: false, z: 2, lineStyle: { width: 1, color: COLORS.drawdown }, areaStyle: { color: ddArea } }],
  }, true);
  renderLegend(heroTicker, hero);
}

function renderLegend(heroTicker, hero) {
  const items = [
    `<span><i style="border-top-color:rgba(236,239,243,0.5);border-top-style:dashed"></i>主标的 B&H</span>`,
    hero ? `<span><i style="border-top-color:${COLORS.live};border-top-width:3px"></i>${heroTicker} 组合净值</span>` : "",
    `<span><b class="sw" style="background:${COLORS.live}"></b>在场</span>`,
    `<span><b class="sw" style="background:${COLORS.standby}"></b>停泊</span>`,
    `<span><b class="sw" style="background:${COLORS.cash}"></b>现金</span>`,
    `<span class="hint">点表格行切换备胎</span>`];
  $("legend").innerHTML = items.filter(Boolean).join("");
}

window.addEventListener("resize", () => chart && chart.resize());
window.matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", () => {
  if (chart) chart.setOption({ animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches });
});

syncRuleFields();
pollStatus();
