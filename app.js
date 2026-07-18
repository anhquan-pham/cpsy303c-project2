/*
 * Nutritional Insights dashboard - Cloud Computing Phase 2
 * Fetches insights from the deployed Azure Function and renders 4 visualizations.
 *
 * ---------------------------------------------------------------------------
 * OBSERVED RESPONSE SHAPE (verified against the live endpoint on 2026-07-17,
 * not assumed from a description). See sample-response.json for a full capture.
 *
 * GET /api/insights            -> full dataset
 * GET /api/insights?diet_type=keto  -> RECOMPUTES EVERY KEY for that diet
 *                                       (full server round-trip, not a partial slice)
 * GET /api/ping                -> { "status": "ok", "time": "..." }
 *
 * {
 *   metadata: {
 *     dataset: "All_Diets.csv",
 *     row_count: 7806,
 *     diet_types: ["dash","keto","mediterranean","paleo","vegan"],
 *     execution_time_ms: 13.1,
 *     generated_at: "2026-07-17T20:10:57Z"
 *   },
 *   avg_macros: [ { Diet_type, "Protein(g)", "Carbs(g)", "Fat(g)" }, ... ],   // 1 row per diet
 *   diet_counts: [ { Diet_type, count }, ... ],                                // sorted desc
 *   scatter_protein_vs_carbs: [ { Diet_type, "Protein(g)", "Carbs(g)" }, ... ],// 500 sample points, long tail
 *   correlations: { labels: ["Protein(g)","Carbs(g)","Fat(g)"], matrix: [[..],[..],[..]] },
 *   top_protein_recipes: [ { Diet_type, Recipe_name, "Protein(g)", "Carbs(g)", "Fat(g)" }, ... ] // 5 per diet
 * }
 *
 * NOTE: keys carry "(g)" suffixes and Diet_type is capitalized. Access exactly.
 * ---------------------------------------------------------------------------
 */

// =============================================================================
// THE ONE PLACE the endpoint lives. Person 1 is tightening CORS to the real
// dashboard URL after deploy; Person 3 updates this single constant on deploy.
// No other file, and nothing below, hardcodes the URL.
// =============================================================================
const API_BASE =
  'https://diet-analysis-func-group9-hmhehrhjeabcd3h4.canadacentral-01.azurewebsites.net';

// -----------------------------------------------------------------------------
// Palette. Okabe-Ito colorblind-safe categorical colors, one per diet type,
// reused across every chart so a color always means the same diet.
// -----------------------------------------------------------------------------
const DIET_COLORS = {
  dash: '#0072B2',          // blue
  keto: '#009E73',          // green
  mediterranean: '#E69F00', // orange
  paleo: '#CC79A7',         // pink
  vegan: '#D55E00',         // vermillion
};
const FALLBACK_COLOR = '#6b7280';
const dietColor = (d) => DIET_COLORS[String(d).toLowerCase()] || FALLBACK_COLOR;

// Macro colors for the grouped bar chart (bars are macros, not diets).
const MACRO_COLORS = { protein: '#0072B2', carbs: '#E69F00', fat: '#D55E00' };

// -----------------------------------------------------------------------------
// State
// -----------------------------------------------------------------------------
let lastGood = null;      // last successful response, kept so the UI never blanks on error
let dropdownReady = false; // populate the diet dropdown only once (from the full response)
let tableRows = [];       // top_protein_recipes for the current view
let currentPage = 1;
const PAGE_SIZE = 10;

// Live Chart.js instances. Destroyed before re-render so filter changes do not
// leak instances or leave stale tooltips behind.
const charts = { bar: null, scatter: null, pie: null };

// =============================================================================
// STEP 1 - FETCH LAYER. Everything goes through fetchInsights().
// =============================================================================
async function fetchInsights(dietType) {
  const useFilter = dietType && dietType !== 'all';
  const url = useFilter
    ? `${API_BASE}/api/insights?diet_type=${encodeURIComponent(dietType)}`
    : `${API_BASE}/api/insights`;

  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    // Non-2xx surfaces as a real error, not a silent console log.
    throw new Error(`HTTP ${res.status} ${res.statusText}`);
  }
  return res.json();
}

async function pingHealth() {
  try {
    const res = await fetch(`${API_BASE}/api/ping`, { headers: { Accept: 'application/json' } });
    return res.ok;
  } catch {
    return false;
  }
}

// -----------------------------------------------------------------------------
// Status banner. Cold starts on a consumption-plan Function can take 5-20s after
// idle; show that plainly so a blank screen never reads as a crash.
// -----------------------------------------------------------------------------
const statusEl = () => document.getElementById('status-banner');

function showStatus(kind, message) {
  const el = statusEl();
  el.textContent = message;
  el.className =
    'status-banner ' +
    (kind === 'error'
      ? 'bg-red-100 text-red-800 border border-red-300'
      : kind === 'ok'
      ? 'bg-green-100 text-green-800 border border-green-300'
      : 'bg-blue-100 text-blue-800 border border-blue-300');
  el.style.display = 'block';
}

function hideStatus() {
  statusEl().style.display = 'none';
}

// Load orchestration: loading -> cold-start hint after 3s -> render or error.
async function load(dietType) {
  setControlsDisabled(true);
  showStatus('info', 'Loading insights from the Azure Function...');
  const coldHint = setTimeout(() => {
    showStatus('info', 'Waking up the Azure Function... cold starts after idle can take up to ~20s. Still working.');
  }, 3000);

  try {
    const data = await fetchInsights(dietType);
    clearTimeout(coldHint);
    lastGood = data;
    render(data);
    hideStatus();
  } catch (err) {
    clearTimeout(coldHint);
    showStatus(
      'error',
      `Could not load data: ${err.message}. The function may be starting up or unreachable - press Refresh to retry.`
    );
    // Leave whatever was last rendered on screen instead of clearing it.
  } finally {
    setControlsDisabled(false);
  }
}

function setControlsDisabled(disabled) {
  ['diet-select', 'refresh-btn'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = disabled;
  });
}

// =============================================================================
// RENDER - metadata, controls, 4 charts, table.
// =============================================================================
function render(data) {
  renderMetadata(data.metadata);
  if (!dropdownReady) populateDietDropdown(data.metadata.diet_types);

  renderBarChart(data.avg_macros);
  renderScatter(data.scatter_protein_vs_carbs);
  renderPie(data.diet_counts);
  renderHeatmap(data.correlations);

  tableRows = Array.isArray(data.top_protein_recipes) ? data.top_protein_recipes : [];
  currentPage = 1;
  renderTable();
}

// STEP 4 - metadata. execution_time_ms is the rubric-marked value; row_count
// updates on every filter, which proves the fetch is live.
function renderMetadata(meta) {
  document.getElementById('meta-exec').textContent =
    meta.execution_time_ms != null ? `${meta.execution_time_ms} ms` : '-';
  document.getElementById('meta-rows').textContent =
    meta.row_count != null ? meta.row_count.toLocaleString() : '-';
  document.getElementById('meta-dataset').textContent = meta.dataset || '-';
  document.getElementById('meta-generated').textContent = meta.generated_at
    ? new Date(meta.generated_at).toLocaleString()
    : '-';
}

function populateDietDropdown(dietTypes) {
  const sel = document.getElementById('diet-select');
  // Rebuild from metadata.diet_types (not hardcoded). Keep an "All" option.
  sel.innerHTML = '';
  const all = document.createElement('option');
  all.value = 'all';
  all.textContent = 'All Diet Types';
  sel.appendChild(all);
  (dietTypes || []).forEach((d) => {
    const opt = document.createElement('option');
    opt.value = d;
    opt.textContent = d.charAt(0).toUpperCase() + d.slice(1);
    sel.appendChild(opt);
  });
  dropdownReady = true;
}

// Shared Chart.js options. responsive + maintainAspectRatio:false so the chart
// sizes to its .relative wrapper, never to a height class on the canvas.
const baseOptions = () => ({
  responsive: true,
  maintainAspectRatio: false,
});

// 1) GROUPED BAR - avg_macros. One group per diet, three bars (protein/carbs/fat).
function renderBarChart(avgMacros) {
  const labels = avgMacros.map((r) => r.Diet_type);
  const ctx = document.getElementById('barChart').getContext('2d');
  if (charts.bar) charts.bar.destroy();
  charts.bar = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Protein (g)', data: avgMacros.map((r) => r['Protein(g)']), backgroundColor: MACRO_COLORS.protein },
        { label: 'Carbs (g)', data: avgMacros.map((r) => r['Carbs(g)']), backgroundColor: MACRO_COLORS.carbs },
        { label: 'Fat (g)', data: avgMacros.map((r) => r['Fat(g)']), backgroundColor: MACRO_COLORS.fat },
      ],
    },
    options: {
      ...baseOptions(),
      scales: { y: { beginAtZero: true, title: { display: true, text: 'grams (avg)' } } },
      plugins: { legend: { position: 'bottom' } },
    },
  });
}

// 2) SCATTER - protein (x) vs carbs (y). One dataset per diet so color is
// meaningful and the legend explains it, rather than a single grey cloud.
function renderScatter(points) {
  const byDiet = {};
  points.forEach((p) => {
    const d = p.Diet_type;
    (byDiet[d] ||= []).push({ x: p['Protein(g)'], y: p['Carbs(g)'] });
  });
  const datasets = Object.keys(byDiet).map((d) => ({
    label: d,
    data: byDiet[d],
    backgroundColor: dietColor(d) + 'cc',
    pointRadius: 3,
    pointHoverRadius: 5,
  }));

  const ctx = document.getElementById('scatterPlot').getContext('2d');
  if (charts.scatter) charts.scatter.destroy();
  charts.scatter = new Chart(ctx, {
    type: 'scatter',
    data: { datasets },
    options: {
      ...baseOptions(),
      scales: {
        x: { title: { display: true, text: 'Protein (g)' } },
        y: { title: { display: true, text: 'Carbs (g)' } },
      },
      plugins: {
        legend: { position: 'bottom' },
        tooltip: {
          callbacks: {
            label: (c) => `${c.dataset.label}: ${c.parsed.x}g protein, ${c.parsed.y}g carbs`,
          },
        },
      },
    },
  });
}

// 3) PIE - diet_counts (recipe distribution by diet type).
function renderPie(dietCounts) {
  const labels = dietCounts.map((r) => r.Diet_type);
  const ctx = document.getElementById('pieChart').getContext('2d');
  if (charts.pie) charts.pie.destroy();
  charts.pie = new Chart(ctx, {
    type: 'pie',
    data: {
      labels,
      datasets: [
        { data: dietCounts.map((r) => r.count), backgroundColor: labels.map(dietColor) },
      ],
    },
    options: {
      ...baseOptions(),
      plugins: {
        legend: { position: 'bottom' },
        tooltip: {
          callbacks: {
            label: (c) => {
              const total = c.dataset.data.reduce((a, b) => a + b, 0);
              const pct = total ? ((c.parsed / total) * 100).toFixed(1) : '0';
              return `${c.label}: ${c.parsed.toLocaleString()} (${pct}%)`;
            },
          },
        },
      },
    },
  });
}

// 4) HEATMAP - correlations, hand-rolled CSS grid (no chart lib needed for a
// fixed 3x3). Diverging blue(-1) - white(0) - red(+1) scale.
function corrColor(v) {
  // v in [-1, 1]. Interpolate white->blue for negatives, white->red for positives.
  const t = Math.min(Math.abs(v), 1);
  if (v >= 0) {
    // white (255,255,255) -> red (213,94,0)
    const r = Math.round(255 + t * (213 - 255));
    const g = Math.round(255 + t * (94 - 255));
    const b = Math.round(255 + t * (0 - 255));
    return `rgb(${r},${g},${b})`;
  }
  // white -> blue (0,114,178)
  const r = Math.round(255 + t * (0 - 255));
  const g = Math.round(255 + t * (114 - 255));
  const b = Math.round(255 + t * (178 - 255));
  return `rgb(${r},${g},${b})`;
}

const shortLabel = (l) => l.replace('(g)', '').trim();

function renderHeatmap(corr) {
  const host = document.getElementById('heatmap');
  const labels = corr.labels;
  const matrix = corr.matrix;
  host.innerHTML = '';

  const grid = document.createElement('div');
  grid.className = 'heatmap-grid';
  grid.style.gridTemplateColumns = `auto repeat(${labels.length}, 1fr)`;

  // top-left blank corner
  grid.appendChild(cell('', 'heatmap-corner'));
  // column headers
  labels.forEach((l) => grid.appendChild(cell(shortLabel(l), 'heatmap-head')));

  // rows
  matrix.forEach((row, i) => {
    grid.appendChild(cell(shortLabel(labels[i]), 'heatmap-head heatmap-rowhead'));
    row.forEach((v) => {
      const c = cell(v.toFixed(2), 'heatmap-cell');
      c.style.backgroundColor = corrColor(v);
      c.style.color = Math.abs(v) > 0.6 ? '#fff' : '#111';
      c.title = `${v.toFixed(3)}`;
      grid.appendChild(c);
    });
  });

  host.appendChild(grid);

  // legend
  const legend = document.createElement('div');
  legend.className = 'heatmap-legend';
  legend.innerHTML =
    '<span>-1</span><span class="heatmap-scale"></span><span>+1</span>';
  host.appendChild(legend);
}

function cell(text, cls) {
  const d = document.createElement('div');
  d.className = cls;
  d.textContent = text;
  return d;
}

// STEP 3 - table + pagination, wired to top_protein_recipes.
function renderTable() {
  const body = document.getElementById('recipe-body');
  body.innerHTML = '';
  const totalPages = Math.max(1, Math.ceil(tableRows.length / PAGE_SIZE));
  if (currentPage > totalPages) currentPage = totalPages;

  const start = (currentPage - 1) * PAGE_SIZE;
  const pageRows = tableRows.slice(start, start + PAGE_SIZE);

  if (pageRows.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="5" class="px-3 py-4 text-center text-gray-500">No recipes.</td>';
    body.appendChild(tr);
  } else {
    pageRows.forEach((r, idx) => {
      const tr = document.createElement('tr');
      tr.className = idx % 2 ? 'bg-gray-50' : '';
      tr.innerHTML = `
        <td class="px-3 py-2">
          <span class="diet-dot" style="background:${dietColor(r.Diet_type)}"></span>${r.Diet_type}
        </td>
        <td class="px-3 py-2">${escapeHtml(r.Recipe_name)}</td>
        <td class="px-3 py-2 text-right">${fmt(r['Protein(g)'])}</td>
        <td class="px-3 py-2 text-right">${fmt(r['Carbs(g)'])}</td>
        <td class="px-3 py-2 text-right">${fmt(r['Fat(g)'])}</td>`;
      body.appendChild(tr);
    });
  }

  renderPagination(totalPages);
}

function renderPagination(totalPages) {
  const nav = document.getElementById('pagination');
  nav.innerHTML = '';

  const mkBtn = (label, page, opts = {}) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.className = opts.active
      ? 'px-3 py-1 bg-blue-600 text-white rounded'
      : 'px-3 py-1 bg-gray-300 rounded hover:bg-gray-400 disabled:opacity-40 disabled:cursor-not-allowed';
    b.disabled = !!opts.disabled;
    if (!opts.disabled && !opts.active) {
      b.addEventListener('click', () => {
        currentPage = page;
        renderTable();
      });
    }
    return b;
  };

  nav.appendChild(mkBtn('Previous', currentPage - 1, { disabled: currentPage <= 1 }));
  for (let p = 1; p <= totalPages; p++) {
    nav.appendChild(mkBtn(String(p), p, { active: p === currentPage }));
  }
  nav.appendChild(mkBtn('Next', currentPage + 1, { disabled: currentPage >= totalPages }));
}

// -----------------------------------------------------------------------------
// Small helpers
// -----------------------------------------------------------------------------
const fmt = (n) => (typeof n === 'number' ? n.toFixed(1) : n ?? '-');

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// =============================================================================
// Wire-up
// =============================================================================
function init() {
  document.getElementById('diet-select').addEventListener('change', (e) => {
    load(e.target.value);
  });
  document.getElementById('refresh-btn').addEventListener('click', () => {
    const diet = document.getElementById('diet-select').value || 'all';
    load(diet);
  });

  load('all');
}

document.addEventListener('DOMContentLoaded', init);
