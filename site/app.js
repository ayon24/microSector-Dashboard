"use strict";

const LWC = () => window.LightweightCharts;
const $ = (sel) => document.querySelector(sel);

const store = {
  get(k, d) { try { return localStorage.getItem("msd-" + k) ?? d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("msd-" + k, v); } catch (e) { /* storage unavailable */ } },
};

const state = {
  view: store.get("view", "grid"),
  tf: store.get("tf", "1Y"),
  scale: store.get("scale", "linear"),
  q: "",
  broad: "",
  show: "all",
  sort: "ret-desc",
  tableSort: { key: null, dir: -1 },
};

let summary, bench;
const sectorCache = new Map();
const cards = new Map();          // id -> { el, chart, idxSeries, benchSeries, data }
let detailChart = null;

// ---------- data ----------

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

function loadSector(id) {
  if (!sectorCache.has(id)) sectorCache.set(id, getJSON(`data/indices/${id}.json`));
  return sectorCache.get(id);
}

// ---------- dates & maths (mirrors timeframe_start() in pipeline/export_json.py) ----------

function parseDate(s) { const [y, m, d] = s.split("-").map(Number); return { y, m, d }; }
function fmtISO(y, m, d) { return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`; }
function daysInMonth(y, m) { return new Date(Date.UTC(y, m, 0)).getUTCDate(); }

function targetDate(asOf, tf) {
  const { y, m, d } = parseDate(asOf);
  if (tf === "YTD") return fmtISO(y - 1, 12, 31);
  if (tf === "1W") {
    const t = new Date(Date.UTC(y, m - 1, d - 7));
    return t.toISOString().slice(0, 10);
  }
  const months = { "1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36, "5Y": 60 }[tf];
  let mm = m - months, yy = y;
  while (mm <= 0) { mm += 12; yy -= 1; }
  return fmtISO(yy, mm, Math.min(d, daysInMonth(yy, mm)));
}

// Charts for 1D / 1W show the last month so there is a line to look at.
function chartTf(tf) { return tf === "1D" || tf === "1W" ? "1M" : tf; }

function lastIndexOnOrBefore(dates, target) {
  let lo = 0, hi = dates.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (dates[mid] <= target) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

function rebased(dates, values, target) {
  let i = lastIndexOnOrBefore(dates, target);
  const sinceStart = i < 0;
  if (sinceStart) i = 0;
  const base = values[i];
  const out = [];
  for (let k = i; k < dates.length; k++) out.push({ time: dates[k], value: (values[k] / base) * 100 });
  return { points: out, sinceStart };
}

function ret(row, tf) { return row.returns ? row.returns[tf] : null; }
function relRet(r, b) { return r == null || b == null ? null : ((1 + r / 100) / (1 + b / 100) - 1) * 100; }

// ---------- formatting ----------

function fmtPct(v, digits = 1) {
  if (v == null || Number.isNaN(v)) return "—";
  const s = v > 0 ? "+" : v < 0 ? "−" : "";
  return s + Math.abs(v).toFixed(digits) + "%";
}
function cls(v) { return v == null ? "muted" : v > 0 ? "pos" : v < 0 ? "neg" : ""; }
function fmtDate(iso) {
  const { y, m, d } = parseDate(iso);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// ---------- chart ----------

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function chartOptions(compact) {
  const L = LWC();
  return {
    autoSize: true,
    layout: { background: { type: "solid", color: "transparent" }, textColor: cssVar("--text-3"), fontSize: compact ? 10 : 11, attributionLogo: false },
    grid: { vertLines: { visible: false }, horzLines: { color: cssVar("--grid") } },
    rightPriceScale: {
      borderVisible: false,
      scaleMargins: { top: 0.08, bottom: 0.08 },
      mode: state.scale === "log" ? L.PriceScaleMode.Logarithmic : L.PriceScaleMode.Normal,
    },
    timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true, lockVisibleTimeRangeOnResize: true },
    crosshair: {
      mode: L.CrosshairMode.Magnet,
      vertLine: { color: cssVar("--text-3"), labelVisible: !compact, style: L.LineStyle.Solid, width: 1 },
      horzLine: { visible: false, labelVisible: false },
    },
    handleScroll: false,
    handleScale: false,
  };
}

const AXIS_FORMAT = { type: "custom", minMove: 0.01, formatter: (v) => (Math.abs(v) >= 20 ? v.toFixed(0) : v.toFixed(1)) };

function createChart(el, compact) {
  const L = LWC();
  const chart = L.createChart(el, chartOptions(compact));
  const benchSeries = chart.addSeries(L.LineSeries, {
    color: cssVar("--series-bench"), lineWidth: 2, lineStyle: L.LineStyle.Dotted,
    priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat: AXIS_FORMAT,
  });
  const idxSeries = chart.addSeries(L.LineSeries, {
    color: cssVar("--series-idx"), lineWidth: 2, priceLineVisible: false, lastValueVisible: !compact, priceFormat: AXIS_FORMAT,
    crosshairMarkerRadius: 4, crosshairMarkerBorderColor: cssVar("--surface"), crosshairMarkerBorderWidth: 2,
  });
  const baseLine = idxSeries.createPriceLine({ price: 100, color: cssVar("--text-3"), lineWidth: 1, lineStyle: L.LineStyle.Dashed, axisLabelVisible: false });
  return { chart, idxSeries, benchSeries, baseLine };
}

function restyleChart(c, compact) {
  c.chart.applyOptions(chartOptions(compact));
  c.benchSeries.applyOptions({ color: cssVar("--series-bench") });
  c.idxSeries.applyOptions({ color: cssVar("--series-idx"), crosshairMarkerBorderColor: cssVar("--surface") });
  c.baseLine.applyOptions({ color: cssVar("--text-3") });
}

function setChartData(c, data, tf = chartTf(state.tf)) {
  const target = targetDate(summary.as_of, tf);
  const idx = rebased(data.dates, data.values, target);
  const startDate = idx.points.length ? idx.points[0].time : target;
  const b = rebased(bench.dates, bench.values, startDate);
  c.idxSeries.setData(idx.points);
  c.benchSeries.setData(b.points);
  c.chart.timeScale().fitContent();
  const m = c.idxMap = new Map(idx.points.map((p) => [p.time, p.value]));
  c.benchMap = new Map(b.points.map((p) => [p.time, p.value]));
  return { sinceStart: idx.sinceStart, start: startDate, size: m.size, target };
}

function wireReadout(c, el, defaultText) {
  c.chart.subscribeCrosshairMove((param) => {
    if (!param.time || !param.point) { el.innerHTML = defaultText(); return; }
    const t = typeof param.time === "string" ? param.time : fmtISO(param.time.year, param.time.month, param.time.day);
    const iv = c.idxMap.get(t), bv = c.benchMap.get(t);
    const ev = c.emaMap ? c.emaMap.get(t) : null;
    el.innerHTML = `<b>${fmtDate(t)}</b> · Index ${iv != null ? fmtPct(iv - 100) : "—"} · ${esc(bench.name)} ${bv != null ? fmtPct(bv - 100) : "—"}` +
      (ev != null ? ` · ${c.emaLabel} ${fmtPct(ev - 100)}` : "");
  });
}

// ---------- grid ----------

function cardDefaultReadout(row, info) {
  const tf = chartTf(state.tf);
  if (info && info.sinceStart) return `Chart since launch, ${fmtDate(info.start)}`;
  return tf !== state.tf ? `Chart: last 1M, rebased to 100` : `Rebased to 100 at start of ${tf}`;
}

function buildCard(row) {
  const el = document.createElement("article");
  el.className = "card";
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  el.dataset.id = row.id;
  el.innerHTML = `
    <div class="card-head">
      <div><div class="card-title">${esc(row.name)}</div><div class="card-broad">${esc(row.broad_sector)} · ${row.constituents} stocks</div></div>
      <div><div class="card-ret"></div><div class="card-rel"></div></div>
    </div>
    <div class="card-readout"></div>
    <div class="card-chart"><div class="skeleton"></div></div>
    <div class="card-foot"><span class="breadth"></span><span class="ma"></span></div>`;
  el.addEventListener("click", () => openDetail(row.id));
  el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail(row.id); } });
  const entry = { el, row, chart: null, info: null };
  cards.set(row.id, entry);
  observer.observe(el);
  return entry;
}

function updateCardText(entry) {
  const { el, row } = entry;
  const r = ret(row, state.tf);
  const rel = relRet(r, summary.benchmark.returns[state.tf]);
  el.querySelector(".card-ret").innerHTML = `<span class="${cls(r)}">${fmtPct(r)}</span>`;
  el.querySelector(".card-rel").textContent = `${fmtPct(rel)} vs ${bench.name}`;
  el.querySelector(".card-foot .breadth").textContent = `1D: ${row.advancers}▲ ${row.decliners}▼`;
  el.querySelector(".card-foot .ma").textContent =
    `>50 DMA ${row.above50_pct ?? "—"}% · >200 DMA ${row.above200_pct ?? "—"}%`;
  el.querySelector(".card-readout").textContent = cardDefaultReadout(row, entry.info);
}

async function drawCard(entry) {
  const data = await loadSector(entry.row.id);
  entry.data = data;
  if (!entry.chart) {
    const host = entry.el.querySelector(".card-chart");
    host.innerHTML = "";
    entry.chart = createChart(host, true);
    const readout = entry.el.querySelector(".card-readout");
    wireReadout(entry.chart, readout, () => esc(cardDefaultReadout(entry.row, entry.info)));
  }
  entry.chart.chart.applyOptions(chartOptions(true));
  entry.info = setChartData(entry.chart, data);
  entry.drawnFor = `${state.tf}|${state.scale}`;
  updateCardText(entry);
}

const observer = new IntersectionObserver((items) => {
  for (const it of items) {
    if (!it.isIntersecting) continue;
    const entry = cards.get(it.target.dataset.id);
    if (entry && entry.drawnFor !== `${state.tf}|${state.scale}`) drawCard(entry).catch(console.error);
  }
}, { rootMargin: "400px 0px" });

function isVisible(el) {
  const r = el.getBoundingClientRect();
  return r.bottom > -400 && r.top < window.innerHeight + 400 && el.style.display !== "none";
}

function filteredRows() {
  const q = state.q.trim().toLowerCase();
  let rows = summary.sectors.filter((s) => s.indexed || state.view === "table");
  if (state.broad) rows = rows.filter((s) => s.broad_sector === state.broad);
  if (q) rows = rows.filter((s) => s.name.toLowerCase().includes(q) || s.broad_sector.toLowerCase().includes(q));
  const byRet = (dir) => (a, b) => {
    const va = ret(a, state.tf), vb = ret(b, state.tf);
    if (va == null) return vb == null ? 0 : 1;   // missing returns always sink
    if (vb == null) return -1;
    return (vb - va) * dir;
  };
  if (state.show !== "all") {
    const ranked = rows.filter((s) => ret(s, state.tf) != null).sort(byRet(1));
    rows = state.show === "top" ? ranked.slice(0, 20) : ranked.slice(-20);
  }
  if (state.sort === "name") return [...rows].sort((a, b) => a.name.localeCompare(b.name));
  return [...rows].sort(byRet(state.sort === "ret-asc" ? -1 : 1));
}

function renderGrid() {
  const grid = $("#grid");
  const rows = filteredRows();
  const keep = new Set(rows.map((r) => r.id));
  for (const [id, entry] of cards) entry.el.style.display = keep.has(id) ? "" : "none";
  for (const row of rows) {
    const entry = cards.get(row.id) || buildCard(row);
    grid.appendChild(entry.el);  // re-appending moves the node, preserving its chart
    updateCardText(entry);
  }
  $("#empty").hidden = rows.length > 0;
  // redraw on-screen cards now; off-screen ones redraw when scrolled into view
  for (const row of rows) {
    const entry = cards.get(row.id);
    if (entry.chart && entry.drawnFor !== `${state.tf}|${state.scale}` && isVisible(entry.el)) {
      drawCard(entry).catch(console.error);
    }
  }
}

// ---------- table ----------

function tableColumns() {
  const tfCols = summary.timeframes.map((tf) => ({ key: "r:" + tf, label: tf, get: (s) => ret(s, tf), fmt: (v) => fmtPct(v), color: true, sel: tf === state.tf }));
  return [
    { key: "name", label: "Micro sector", get: (s) => s.name, cls: "l name", text: true },
    { key: "broad", label: "Sector", get: (s) => s.broad_sector, cls: "l", text: true },
    { key: "n", label: "Stocks", get: (s) => s.constituents },
    ...tfCols,
    { key: "rel", label: `vs ${bench.name} (${state.tf})`, get: (s) => relRet(ret(s, state.tf), summary.benchmark.returns[state.tf]), fmt: (v) => fmtPct(v), color: true },
    { key: "adv", label: "Adv / Dec", get: (s) => s.advancers - s.decliners, fmt: (_, s) => `${s.advancers} / ${s.decliners}` },
    { key: "a50", label: "% > 50 DMA", get: (s) => s.above50_pct, fmt: (v) => (v == null ? "—" : v.toFixed(0) + "%") },
    { key: "a200", label: "% > 200 DMA", get: (s) => s.above200_pct, fmt: (v) => (v == null ? "—" : v.toFixed(0) + "%") },
  ];
}

function renderTable() {
  const cols = tableColumns();
  let rows = filteredRows();
  const { key, dir } = state.tableSort;
  if (key) {
    const col = cols.find((c) => c.key === key);
    if (col) {
      rows = [...rows].sort((a, b) => {
        const va = col.get(a), vb = col.get(b);
        if (va == null) return 1;
        if (vb == null) return -1;
        return (col.text ? String(va).localeCompare(String(vb)) : va - vb) * dir;
      });
    }
  }
  const head = cols.map((c) => {
    const sort = key === c.key ? (dir > 0 ? "ascending" : "descending") : "none";
    return `<th class="sortable ${c.cls || ""} ${c.sel ? "sel" : ""}" data-key="${c.key}" aria-sort="${sort}" scope="col">${esc(c.label)}</th>`;
  }).join("");
  const benchRow = `<tr class="dim"><td class="l name">${esc(bench.name)}</td><td class="l">Benchmark</td><td></td>${
    summary.timeframes.map((tf) => { const v = summary.benchmark.returns[tf]; return `<td class="${cls(v)} ${tf === state.tf ? "sel" : ""}">${fmtPct(v)}</td>`; }).join("")
  }<td></td><td></td><td></td><td></td></tr>`;
  const body = rows.map((s) => {
    const tds = cols.map((c) => {
      const v = c.get(s);
      const txt = c.fmt ? c.fmt(v, s) : v == null ? "—" : esc(v);
      return `<td class="${c.cls || ""} ${c.color ? cls(v) : ""} ${c.sel ? "sel" : ""}">${txt}</td>`;
    }).join("");
    const title = s.indexed ? "" : ` title="Not indexed: fewer than ${summary.methodology.min_constituents} eligible stocks"`;
    return `<tr data-id="${s.id}" class="${s.indexed ? "" : "dim"}"${title}>${tds}</tr>`;
  }).join("");
  $("#table").innerHTML = `<thead><tr>${head}</tr></thead><tbody>${benchRow}${body}</tbody>`;
  $("#empty").hidden = rows.length > 0;
}

$("#table").addEventListener("click", (e) => {
  const th = e.target.closest("th.sortable");
  if (th) {
    const k = th.dataset.key;
    state.tableSort = { key: k, dir: state.tableSort.key === k ? -state.tableSort.dir : (k === "name" || k === "broad" ? 1 : -1) };
    renderTable();
    return;
  }
  const tr = e.target.closest("tr[data-id]");
  if (tr && !tr.classList.contains("dim")) openDetail(tr.dataset.id);
});

// ---------- detail ----------

function emaValues(values, period) {
  const k = 2 / (period + 1), out = new Array(values.length);
  let e = values[0];
  for (let i = 0; i < values.length; i++) { e = i === 0 ? values[0] : values[i] * k + e * (1 - k); out[i] = e; }
  return out;
}

// opts.ema: EMA period to overlay (opened from the EMA scanner)
async function openDetail(id, opts = {}) {
  const row = summary.sectors.find((s) => s.id === id);
  const data = await loadSector(id);
  const dlg = $("#detail");
  $("#detail-title").textContent = row.name;
  $("#detail-sub").textContent =
    `${row.broad_sector} · ${row.constituents} of ${row.members} stocks in index · since ${fmtDate(data.start)} · last rebalanced ${fmtDate(data.last_rebalance)}`;
  if (!dlg.open) dlg.showModal();

  if (!detailChart) {
    detailChart = createChart($("#detail-chart"), false);
    wireReadout(detailChart, $("#detail-readout"), () => detailChart.detailDefault());
  }
  detailChart.chart.applyOptions(chartOptions(false));
  const tf = opts.ema ? (opts.ema <= 30 ? "6M" : "1Y") : chartTf(state.tf);
  const info = setChartData(detailChart, data, tf);
  if (!detailChart.emaSeries) {
    detailChart.emaSeries = detailChart.chart.addSeries(LWC().LineSeries, {
      color: cssVar("--series-ema"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat: AXIS_FORMAT,
    });
  }
  detailChart.emaMap = null;
  if (opts.ema) {
    let i = lastIndexOnOrBefore(data.dates, info.target);
    if (i < 0) i = 0;
    const ev = emaValues(data.values, opts.ema), base = data.values[i], pts = [];
    for (let k = i; k < data.dates.length; k++) pts.push({ time: data.dates[k], value: (ev[k] / base) * 100 });
    detailChart.emaSeries.setData(pts);
    detailChart.emaMap = new Map(pts.map((q) => [q.time, q.value]));
    detailChart.emaLabel = `${opts.ema}-day EMA`;
  } else {
    detailChart.emaSeries.setData([]);
  }
  detailChart.chart.timeScale().fitContent();
  const detailDefault = () => {
    const base = info.sinceStart ? `Since launch, ${fmtDate(info.start)} = 100` : `${tf}, rebased to 100`;
    const key = opts.ema ? ` · <span class="key"><i class="swatch ema"></i>${opts.ema}-day EMA</span>` : "";
    return `${base} · <span class="key"><i class="swatch idx"></i>Index</span> · <span class="key"><i class="swatch bench"></i>${esc(bench.name)}</span>${key} · hover for values`;
  };
  $("#detail-readout").innerHTML = detailDefault();
  detailChart.detailDefault = detailDefault;

  const tfs = summary.timeframes;
  const stocks = [...data.stocks].sort((a, b) => (b.in_index - a.in_index) || ((b.returns[state.tf] ?? -Infinity) - (a.returns[state.tf] ?? -Infinity)));
  $("#detail-table").innerHTML = `<thead><tr><th class="l name" scope="col">Stock</th><th class="l" scope="col">Symbol</th><th scope="col">Close</th>${
    tfs.map((tf) => `<th scope="col" class="${tf === state.tf ? "sel" : ""}">${tf}</th>`).join("")
  }<th scope="col">&gt; 50 DMA</th><th scope="col">&gt; 200 DMA</th><th scope="col">Traded value (₹ cr, 20D median)</th></tr></thead><tbody>${
    stocks.map((s) => `<tr class="${s.in_index ? "" : "dim"}"${s.in_index ? "" : ' title="Not in index: below liquidity threshold at last rebalance, or listed since"'}>
      <td class="l name">${esc(s.name)}</td><td class="l">${esc(s.symbol)}${s.in_index ? "" : ' <span class="tag">excluded</span>'}</td>
      <td>${s.close != null ? s.close.toLocaleString("en-IN", { maximumFractionDigits: 2 }) : "—"}</td>
      ${tfs.map((tf) => `<td class="${cls(s.returns[tf])} ${tf === state.tf ? "sel" : ""}">${fmtPct(s.returns[tf])}</td>`).join("")}
      <td>${s.above50 == null ? "—" : s.above50 ? "Yes" : "No"}</td><td>${s.above200 == null ? "—" : s.above200 ? "Yes" : "No"}</td>
      <td>${s.median_traded_value_cr == null ? "—" : s.median_traded_value_cr.toLocaleString("en-IN")}</td></tr>`).join("")
  }</tbody>`;
}

$("#detail-close").addEventListener("click", () => $("#detail").close());
$("#detail").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });

// ---------- controls ----------

function syncControls() {
  document.querySelectorAll("#view-seg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.view === state.view));
  document.querySelectorAll("#tf-seg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.tf === state.tf));
  document.querySelectorAll("#scale-seg button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.scale === state.scale));
  $("#grid").hidden = state.view !== "grid";
  $("#table-wrap").hidden = state.view !== "table";
  $("#legend").style.visibility = state.view === "grid" ? "visible" : "hidden";
  $("#scale-seg").style.display = state.view === "grid" ? "" : "none";
  $("#chart-note").textContent = chartTf(state.tf) !== state.tf ? "Charts show the last month for 1D and 1W." : "";
}

function render() {
  syncControls();
  if (state.view === "grid") renderGrid(); else renderTable();
}

function wireControls() {
  $("#tf-seg").innerHTML = summary.timeframes.map((tf) => `<button type="button" data-tf="${tf}">${tf}</button>`).join("");
  $("#view-seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; state.view = b.dataset.view; store.set("view", state.view); render(); });
  $("#tf-seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; state.tf = b.dataset.tf; store.set("tf", state.tf); render(); });
  $("#scale-seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; state.scale = b.dataset.scale; store.set("scale", state.scale); render(); });
  $("#search").addEventListener("input", (e) => { state.q = e.target.value; render(); });
  $("#broad").addEventListener("change", (e) => { state.broad = e.target.value; render(); });
  $("#show").addEventListener("change", (e) => { state.show = e.target.value; render(); });
  $("#sort").addEventListener("change", (e) => { state.sort = e.target.value; render(); });

  const broads = [...new Set(summary.sectors.map((s) => s.broad_sector))].sort();
  $("#broad").insertAdjacentHTML("beforeend", broads.map((b) => `<option>${esc(b)}</option>`).join(""));

  $("#theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    store.set("theme", document.documentElement.dataset.theme);
    restyleAll();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", restyleAll);
}

function restyleAll() {
  for (const entry of cards.values()) if (entry.chart) restyleChart(entry.chart, true);
  if (detailChart) {
    restyleChart(detailChart, false);
    if (detailChart.emaSeries) detailChart.emaSeries.applyOptions({ color: cssVar("--series-ema") });
  }
}

// ---------- EMA scanner ----------

const ema = {
  period: store.get("ema-period", "20"),
  onlyAbove: store.get("ema-only-above", "0") === "1",
  q: "",
  broad: "",
  sort: "dist",
  open: new Set(),
};
let emaData = null;

function fmtStreak(st) {
  if (st == null) return "—";
  return st > 0 ? `▲ Above ${st}d` : `▼ Below ${-st}d`;
}

function sectorEma(sec) {
  const e = sec.ema[ema.period];
  if (!e) return null;
  let above = 0, total = 0;
  for (const sym of sec.constituents) {
    const se = emaData.stocks[sym] && emaData.stocks[sym].ema[ema.period];
    if (!se) continue;
    total++;
    if (se[0] > 0) above++;
  }
  return { dist: e[0], streak: e[1], above, total, breadth: total ? (100 * above) / total : null };
}

// A query that is exactly a stock symbol (e.g. "HAL") matches only that stock; otherwise match
// symbol or company name by substring.
function stockMatches(sym, q) {
  const st = emaData.stocks[sym];
  if (!st) return false;
  if (emaData.stocks[q.toUpperCase()]) return sym.toLowerCase() === q;
  return sym.toLowerCase().includes(q) || st.n.toLowerCase().includes(q);
}

function emaRows() {
  const q = ema.q.trim().toLowerCase();
  // Only micro sectors whose index crossed above the EMA on the latest session:
  // below (or on) it the session before, above it now.
  let rows = emaData.sectors.filter((s) => s.indexed).map((s) => ({ s, e: sectorEma(s) })).filter((r) => r.e);
  const universe = rows.length;
  rows = rows.filter((r) => r.e.streak === 1);
  const crossed = rows.length;
  if (ema.broad) rows = rows.filter((r) => r.s.broad_sector === ema.broad);
  if (q) {
    rows = rows.filter((r) => {
      r.hits = r.s.members.filter((sym) => stockMatches(sym, q));
      if (emaData.stocks[q.toUpperCase()]) return r.hits.length > 0;
      return r.s.name.toLowerCase().includes(q) || r.s.broad_sector.toLowerCase().includes(q) || r.hits.length;
    });
  }
  const key = {
    dist: (r) => r.e.dist, breadth: (r) => r.e.breadth ?? -1, r1: (r) => r.s.r1 ?? -Infinity,
  }[ema.sort];
  rows.sort(ema.sort === "name" ? (a, b) => a.s.name.localeCompare(b.s.name) : (a, b) => key(b) - key(a));
  return { rows, universe, crossed };
}

function stockTable(r) {
  const q = ema.q.trim().toLowerCase();
  const inIdx = new Set(r.s.constituents);
  let list = r.s.members.map((sym) => ({ sym, st: emaData.stocks[sym] })).filter((x) => x.st);
  list = list.map((x) => ({ ...x, e: x.st.ema[ema.period] || null }));
  if (ema.onlyAbove) list = list.filter((x) => x.e && x.e[0] > 0);
  list.sort((a, b) => (b.e ? b.e[0] : -Infinity) - (a.e ? a.e[0] : -Infinity));
  if (!list.length) return `<p class="empty">No stocks above the ${ema.period}-day EMA in this micro sector.</p>`;
  return `<div class="table-wrap"><table class="data"><thead><tr>
      <th class="l name" scope="col">Stock</th><th class="l" scope="col">Symbol</th><th scope="col">Close</th>
      <th scope="col">vs ${ema.period}-day EMA</th><th scope="col">Status</th><th scope="col">1D</th><th scope="col">In index</th>
    </tr></thead><tbody>${list.map((x) => `<tr class="${q && stockMatches(x.sym, q) ? "hit" : ""}">
      <td class="l name">${esc(x.st.n)}</td><td class="l">${esc(x.sym)}</td>
      <td>${x.st.c != null ? x.st.c.toLocaleString("en-IN", { maximumFractionDigits: 2 }) : "—"}</td>
      <td class="${cls(x.e && x.e[0])}">${x.e ? fmtPct(x.e[0], 2) : "—"}</td>
      <td class="status ${x.e ? (x.e[1] > 0 ? "pos" : "neg") : "muted"}">${x.e ? (x.e[1] === 1 ? "▲ Crossed above today" : fmtStreak(x.e[1])) : "Not enough history"}</td>
      <td class="${cls(x.st.r1)}">${fmtPct(x.st.r1)}</td>
      <td>${inIdx.has(x.sym) ? "Yes" : '<span class="tag">No</span>'}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderEma() {
  if (!emaData) return;
  const { rows, universe, crossed } = emaRows();
  const day = fmtDate(emaData.as_of) + (summary.provisional ? ` (${summary.snapshot_ist || "intraday"} IST snapshot)` : "");
  $("#ema-summary").innerHTML = `<b>${crossed}</b> of ${universe} micro sectors crossed above their ${ema.period}-day EMA on ${day}` +
    (rows.length !== crossed ? ` · showing ${rows.length}` : "");
  $("#ema-empty").hidden = rows.length > 0;
  $("#ema-empty").textContent = crossed
    ? "No micro sectors match the search or sector filter."
    : `No micro sector crossed above its ${ema.period}-day EMA on ${day}. Try another EMA period.`;
  const q = ema.q.trim();
  $("#ema-list").innerHTML = rows.map((r) => {
    const open = ema.open.has(r.s.id) || (q && r.hits && r.hits.length);
    const e = r.e;
    return `<details class="ema-row" data-id="${r.s.id}"${open ? " open" : ""}>
      <summary>
        <div class="ema-name">${esc(r.s.name)}<div class="card-broad">${esc(r.s.broad_sector)} · ${r.s.constituents.length} stocks</div></div>
        <div class="metric"><span class="lbl">vs ${ema.period}-day EMA</span><span class="val ${cls(e.dist)}">${fmtPct(e.dist, 2)}</span></div>
        <div class="metric m-breadth"><span class="lbl">Stocks above EMA</span><span class="val">${e.total ? `${e.above} / ${e.total}` : "—"}</span>
          <div class="bar" aria-hidden="true"><i style="width:${e.breadth ?? 0}%"></i></div></div>
        <div class="metric m-r1"><span class="lbl">1D</span><span class="val ${cls(r.s.r1)}">${fmtPct(r.s.r1)}</span></div>
        <svg class="chev" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="m9 6 6 6-6 6-1.4-1.4 4.6-4.6-4.6-4.6L9 6Z"/></svg>
      </summary>
      <div class="ema-stocks">${open ? emaStocksBody(r) : ""}</div>
    </details>`;
  }).join("");
  emaRowsById = new Map(rows.map((r) => [r.s.id, r]));
}

let emaRowsById = new Map();
function emaStocksBody(r) {
  const today = r.s.constituents.filter((sym) => { const x = emaData.stocks[sym]; return x && x.ema[ema.period] && x.ema[ema.period][1] === 1; }).length;
  return `<div class="ema-stocks-head"><span>Members, sorted by distance from the ${ema.period}-day EMA · ${today} crossed above today</span>
    <button type="button" class="link-btn" data-chart="${r.s.id}">Open chart with EMA</button></div>${stockTable(r)}`;
}

function wireEma() {
  const sel = $("#ema-period");
  if (!emaData.periods.map(String).includes(ema.period)) ema.period = String(emaData.periods.includes(20) ? 20 : emaData.periods[0]);
  sel.innerHTML = emaData.periods.map((p) => `<option value="${p}">${p}-day</option>`).join("");
  sel.value = ema.period;
  sel.addEventListener("change", () => { ema.period = sel.value; store.set("ema-period", ema.period); renderEma(); });
  $("#ema-search").addEventListener("input", (e) => { ema.q = e.target.value; renderEma(); });
  $("#ema-broad").insertAdjacentHTML("beforeend", [...new Set(emaData.sectors.map((s) => s.broad_sector))].sort().map((b) => `<option>${esc(b)}</option>`).join(""));
  $("#ema-broad").addEventListener("change", (e) => { ema.broad = e.target.value; renderEma(); });
  $("#ema-sort").addEventListener("change", (e) => { ema.sort = e.target.value; renderEma(); });
  const only = $("#ema-only-above");
  only.checked = ema.onlyAbove;
  only.addEventListener("change", () => { ema.onlyAbove = only.checked; store.set("ema-only-above", only.checked ? "1" : "0"); renderEma(); });
  $("#ema-list").addEventListener("toggle", (e) => {
    const d = e.target;
    if (!d.matches || !d.matches("details.ema-row")) return;
    const id = d.dataset.id;
    if (d.open) {
      ema.open.add(id);
      const body = d.querySelector(".ema-stocks");
      if (!body.innerHTML) body.innerHTML = emaStocksBody(emaRowsById.get(id));
    } else ema.open.delete(id);
  }, true);
  $("#ema-list").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-chart]");
    if (b) openDetail(b.dataset.chart, { ema: Number(ema.period) });
  });
}

async function showEma() {
  if (!emaData) {
    emaData = await getJSON("data/ema.json");
    wireEma();
  }
  renderEma();
}

// ---------- pages ----------

function route() {
  const page = location.hash.startsWith("#/ema") ? "ema" : "sectors";
  $("#page-sectors").hidden = page !== "sectors";
  $("#page-ema").hidden = page !== "ema";
  document.querySelectorAll(".nav a").forEach((a) => {
    if (a.dataset.page === page) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  document.title = page === "ema" ? "EMA Scanner · Micro-Sector Dashboard" : "Micro Sector Scanner · Micro-Sector Dashboard";
  if (page === "ema") showEma().catch((e) => { $("#ema-summary").textContent = "Could not load EMA data: " + e.message; console.error(e); });
  else render();
}

// ---------- boot ----------

async function main() {
  [summary, bench] = await Promise.all([getJSON("data/summary.json"), getJSON("data/benchmark.json")]);
  if (!summary.timeframes.includes(state.tf)) state.tf = "1Y";
  const when = summary.provisional
    ? `Data as of ${fmtDate(summary.as_of)}, ${summary.snapshot_ist || "intraday"} IST snapshot <span class="prov">(provisional until the official close is loaded)</span>`
    : `Data as of ${fmtDate(summary.as_of)} close`;
  document.querySelectorAll(".asof").forEach((el) => {
    el.innerHTML = `${when} · ${summary.sectors.filter((s) => s.indexed).length} micro-sector indices vs ${esc(bench.name)}`;
  });
  $("#bench-name").textContent = bench.name;
  const m = summary.methodology;
  $("#method").textContent = `Method: ${m.weighting}. Stocks need a 20-day median traded value of ₹${m.min_median_traded_value_cr} crore; a micro sector needs ${m.min_constituents}+ eligible stocks to be indexed. Returns use adjusted closes.`;
  const notIndexed = summary.sectors.filter((s) => !s.indexed);
  $("#not-indexed").textContent = notIndexed.length
    ? `Not indexed yet (fewer than ${m.min_constituents} eligible stocks): ${notIndexed.map((s) => s.name).join(", ")}. Shown in the table view.`
    : "";
  wireControls();
  window.addEventListener("hashchange", route);
  route();
}

function boot() {
  if (!window.LightweightCharts) { setTimeout(boot, 30); return; }
  main().catch((e) => { document.querySelectorAll(".asof").forEach((el) => { el.textContent = "Could not load data: " + e.message; }); console.error(e); });
}
boot();
