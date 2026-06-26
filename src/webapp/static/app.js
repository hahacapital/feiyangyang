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
                 mode: $("mode").value, sort: $("sort").value };
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

function streamJob(jobId) {
  const ev = new EventSource(`/api/scan/${jobId}/events`);
  ev.addEventListener("progress", (m) => {
    const d = JSON.parse(m.data); setProgress(d.done, d.total);
  });
  let finished = false;
  ev.addEventListener("result", async (m) => {
    finished = true;
    ev.close();
    const d = JSON.parse(m.data);
    $("progress").classList.add("hidden");
    $("run").disabled = false;
    if (d.status !== "done") return failScan(d.error || "Scan did not complete.");
    try {                                            // gzip-eligible JSON poll
      const full = await (await fetch(`/api/scan/${jobId}/result`)).json();
      render(full.result);
    } catch { failScan("Could not load the result — please re-run."); }
  });
  ev.addEventListener("error", () => {
    if (finished || ev.readyState === EventSource.CLOSED) return;  // ignore post-result close
    ev.close();
    failScan("Service restarted — please re-run.");
  });
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
function render(r) {
  const rec = r.recommendation;
  if (rec.state === "in-market") setPlate("live", `LIVE — holding ${r.curves.picks.length ? document.getElementById("ticker").value.trim().toUpperCase() : ""}`.trim());
  else setPlate("parked", rec.top ? `STANDBY — park in ${rec.top}` : "STANDBY — no qualifying backup");
  renderGauges(r.ranked[0]);
  renderTable(r.ranked);
  renderChart(r.curves);
}

function renderGauges(top) {
  if (!top) { $("gauges").innerHTML = ""; return; }
  $("gauges").innerHTML =
    `<div>afscore<b>${(top.afscore * 100).toFixed(1)}</b></div>
     <div>corr_off<b>${top.corr_off.toFixed(2)}</b></div>
     <div>park%<b>${(top.park_return * 100).toFixed(0)}</b></div>`;
}

function renderTable(rows) {
  if (!rows.length) { $("table").innerHTML = `<p class="muted">No qualifying backup.</p>`; return; }
  const cols = [["#", (r, i) => i + 1], ["ticker", (r) => r.ticker],
    ["off%", (r) => (r.off_frac * 100).toFixed(0)], ["afscore", (r) => (r.afscore * 100).toFixed(1)],
    ["cagr_n", (r) => pct(r.cagr_n)], ["maxdd_n", (r) => pct(r.max_dd_n)],
    ["calmar_n", (r) => r.calmar_n.toFixed(2)], ["sharpe_n", (r) => r.sharpe_n.toFixed(2)],
    ["corr", (r) => r.corr_off.toFixed(2)], ["park%", (r) => (r.park_return * 100).toFixed(0)],
    ["cagr_f", (r) => pct(r.cagr_f)], ["maxdd_f", (r) => pct(r.max_dd_f)]];
  const head = "<tr>" + cols.map((c) => `<th>${c[0]}</th>`).join("") + "</tr>";
  const body = rows.map((r, i) => "<tr>" + cols.map((c) => `<td>${c[1](r, i)}</td>`).join("") + "</tr>").join("");
  $("table").innerHTML = `<table>${head}${body}</table>`;
}

// ---- 3-panel chart: equity (log) / regime ribbon / drawdown ----
function renderChart(curves) {
  if (!chart) chart = echarts.init($("chart"), null, { renderer: "canvas" });
  const dates = curves.dates;
  // Regime ribbon: look up fill/pattern by dataIndex — api.value() on a value axis
  // coerces the state string to null, so we never read state from api. Each state
  // also carries a non-color channel (solid cyan / amber+hatch / grey dotted-empty)
  // for colorblind separability per the design's redundant-encoding rule.
  const dateIdx = new Map(dates.map((d, i) => [d, i]));
  const regimeRows = curves.regime.map((s) => ({ value: [dateIdx.get(s.start), dateIdx.get(s.end)] }));
  const regimeStates = curves.regime.map((s) => s.state);
  const renderRibbon = (params, api) => {
    const x0 = api.coord([api.value(0), 0])[0];
    const x1 = api.coord([api.value(1), 0])[0];
    const cs = params.coordSys;
    const x = Math.min(x0, x1), w = Math.max(1, Math.abs(x1 - x0));
    const rect = { x, y: cs.y, width: w, height: cs.height };
    const state = regimeStates[params.dataIndex];
    if (state === "primary")
      return { type: "rect", shape: rect, style: { fill: REGIME_COLOR.primary } };
    if (state === "cash")                            // empty + dotted outline
      return { type: "rect", shape: rect,
        style: { fill: "transparent", stroke: REGIME_COLOR.cash, lineWidth: 1, lineDash: [2, 2] } };
    const hatch = [];                                // candidate: amber + diagonal hatch
    for (let hx = x - cs.height; hx < x + w; hx += 6)
      hatch.push({ type: "line",
        shape: { x1: hx, y1: cs.y + cs.height, x2: hx + cs.height, y2: cs.y },
        style: { stroke: "#10151c", lineWidth: 1 } });
    return { type: "group", clipPath: { type: "rect", shape: rect },
      children: [{ type: "rect", shape: rect, style: { fill: REGIME_COLOR.candidate } }, ...hatch] };
  };
  const lineStyles = ["solid", "dashed", "dotted"];
  const pickSeries = curves.picks.map((p, i) => ({
    name: `+ ${p.ticker}`, type: "line", xAxisIndex: 0, yAxisIndex: 0,
    data: p.equity, showSymbol: false,
    lineStyle: { width: i === 0 ? 2.4 : 1.4, type: lineStyles[(i + 2) % 3] },
    color: i === 0 ? COLORS.live : COLORS.cash }));
  const baseSeries = [
    { name: "Primary Buy&Hold", type: "line", xAxisIndex: 0, yAxisIndex: 0,
      data: curves.primary_buy_hold, showSymbol: false,
      lineStyle: { width: 1.2, color: COLORS.paper } },
    { name: "Primary + cash", type: "line", xAxisIndex: 0, yAxisIndex: 0,
      data: curves.primary_cash, showSymbol: false,
      lineStyle: { width: 1, type: "dashed", color: COLORS.cash } }];

  chart.setOption({
    animation: !REDUCE, animationDuration: 700, backgroundColor: "transparent",
    textStyle: { fontFamily: "Spline Sans Mono, monospace", color: COLORS.cash },
    grid: [{ left: 56, right: 18, top: 16, height: 250 },
           { left: 56, right: 18, top: 280, height: 26 },
           { left: 56, right: 18, top: 326, height: 92 }],
    axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: COLORS.edge } },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" },
      backgroundColor: "#10151c", borderColor: COLORS.edge,
      textStyle: { color: COLORS.paper, fontFamily: "Spline Sans Mono, monospace" } },
    xAxis: [
      { type: "category", data: dates, gridIndex: 0, axisLabel: { show: false }, axisLine: { lineStyle: { color: COLORS.edge } } },
      { type: "category", data: dates, gridIndex: 1, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { show: false } },
      { type: "category", data: dates, gridIndex: 2, axisLine: { lineStyle: { color: COLORS.edge } } }],
    yAxis: [
      { type: "log", gridIndex: 0, name: "equity", splitLine: { lineStyle: { color: "#1b2330" } } },
      { type: "value", gridIndex: 1, show: false, min: 0, max: 1 },
      { type: "value", gridIndex: 2, name: "DD %", splitLine: { lineStyle: { color: "#1b2330" } } }],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2] },
      { type: "slider", xAxisIndex: [0, 1, 2], bottom: 4, height: 14,
        borderColor: COLORS.edge, fillerColor: "rgba(43,184,230,.15)" }],
    series: [
      ...baseSeries, ...pickSeries,
      { name: "regime", type: "custom", xAxisIndex: 1, yAxisIndex: 1, clip: true,
        renderItem: renderRibbon, encode: { x: [0, 1] }, data: regimeRows,
        tooltip: { formatter: (p) => `regime: ${regimeStates[p.dataIndex]}` } },
      { name: "rank-1 DD", type: "line", xAxisIndex: 2, yAxisIndex: 2,
        data: curves.rank1_drawdown, showSymbol: false, areaStyle: { opacity: 0.25 },
        lineStyle: { width: 1, color: COLORS.drawdown }, color: COLORS.drawdown }],
  }, true);
  renderLegend(curves);
}

function renderLegend(curves) {
  const items = [
    `<span><i style="border-top-color:${COLORS.paper}"></i>Primary B&H</span>`,
    `<span><i style="border-top-color:${COLORS.cash};border-top-style:dashed"></i>Primary+cash</span>`,
    ...curves.picks.map((p, i) =>
      `<span><i style="border-top-color:${i === 0 ? COLORS.live : COLORS.cash};border-top-style:${["solid","dashed","dotted"][(i+2)%3]}"></i>${p.ticker}</span>`),
    `<span><b class="sw" style="background:${COLORS.live}"></b>LIVE</span>`,
    `<span><b class="sw" style="background:${COLORS.standby}"></b>PARKED</span>`,
    `<span><b class="sw" style="background:${COLORS.cash}"></b>CASH</span>`];
  $("legend").innerHTML = items.join("");
}

window.addEventListener("resize", () => chart && chart.resize());
window.matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", () => {
  if (chart) chart.setOption({ animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches });
});

syncRuleFields();
pollStatus();
