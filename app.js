/*
 * Nutritional Insights dashboard - Cloud Computing Phase 3
 * Renders 4 visualizations plus a searchable, server-paged recipe explorer.
 *
 * ---------------------------------------------------------------------------
 * RESPONSE SHAPES
 *
 * GET /api/insights                 -> the whole dataset
 * GET /api/insights?diet_type=keto  -> the same shape, one diet
 *
 *   Phase 2 recomputed this on every request. Phase 3 does not: the payload is
 *   built once when All_Diets.csv changes and read back out of memory or SQL,
 *   so metadata now also carries served_from ("memory" | "sql"),
 *   cache_build_id and response_time_ms.
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
 * GET /api/recipes?q=&diet_type=&page=&page_size=&sort=     (Phase 3)
 *
 *   Searching, filtering and paging all happen in SQL. `items` is only ever one
 *   page long; `total` is the size of the whole matching set, which is what the
 *   pager needs and what the browser must not have to download to know.
 *
 * {
 *   items: [ { id, Diet_type, Recipe_name, Cuisine_type, "Protein(g)", "Carbs(g)", "Fat(g)" }, ... ],
 *   total: 732, page: 1, page_size: 25, total_pages: 30,
 *   query: { q, diet_type, sort },
 *   metadata: { served_from, response_time_ms }
 * }
 *
 * GET /api/ping                     -> { "status": "ok", "time": "..." }
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

// Live Chart.js instances. Destroyed before re-render so filter changes do not
// leak instances or leave stale tooltips behind.
const charts = { bar: null, scatter: null, pie: null };

// Recipe explorer. Every field here is a query parameter the server acts on -
// nothing in this object is used to filter or slice rows in the browser.
const explorer = {
  q: '',
  dietType: 'all',
  page: 1,
  pageSize: 25,
  sort: 'name_asc',
  totalPages: 1,
  // Monotonic id of the newest in-flight request. Typing "chicken" fires several
  // searches; without this, a slow response for "chi" can land after "chicken"
  // and overwrite the results with the wrong ones.
  requestId: 0,
};

const SEARCH_DEBOUNCE_MS = 350;

// =============================================================================
// STEP 1 - FETCH LAYER.
//
// Every request the dashboard makes goes through apiFetch. That is deliberate:
// it is the single place a header, a token refresh or a 401 redirect has to be
// added, so authentication does not have to be threaded through each caller.
// =============================================================================
async function apiFetch(path, params = {}) {
  const url = new URL(`${API_BASE}/api/${path}`);
  Object.entries(params).forEach(([key, value]) => {
    // Skip empty values so the URL stays readable: /api/recipes?page=2 rather
    // than /api/recipes?q=&diet_type=&page=2.
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, value);
    }
  });

  const headers = { Accept: 'application/json' };

  // --- AUTH HOOK (Person 3) --------------------------------------------------
  // Add the bearer token here and every call in the app is authenticated:
  //     const token = sessionStorage.getItem('token');
  //     if (token) headers.Authorization = `Bearer ${token}`;
  // A 401 can be turned into a redirect to the login page in the block below.
  // ---------------------------------------------------------------------------

  const res = await fetch(url, { headers });
  if (!res.ok) {
    // Surface the function's own error text when it sends one - "the pipeline
    // has not run yet" is far more useful on screen than a bare 503.
    let detail = '';
    try {
      const body = await res.json();
      detail = body.detail || body.error || '';
    } catch {
      /* response was not JSON; the status line is all we have */
    }
    throw new Error(`HTTP ${res.status} ${res.statusText}${detail ? ` - ${detail}` : ''}`);
  }
  return res.json();
}

function fetchInsights(dietType) {
  const useFilter = dietType && dietType !== 'all';
  return apiFetch('insights', useFilter ? { diet_type: dietType } : {});
}

function fetchRecipes({ q, dietType, page, pageSize, sort }) {
  return apiFetch('recipes', {
    q,
    diet_type: dietType && dietType !== 'all' ? dietType : '',
    page,
    page_size: pageSize,
    sort,
  });
}

async function pingHealth() {
  try {
    await apiFetch('ping');
    return true;
  } catch {
    return false;
  }
}

// =============================================================================
// AUTH HOOK (Person 3 owns the body of everything in this block)
//
// The dashboard already calls requireAuth() before it renders anything and
// already has a header slot for the user's name and a logout button. Replacing
// the three stubs below is the whole integration - no other part of this file
// needs to change.
// =============================================================================

// Return false to stop the dashboard rendering (and send the visitor to the
// login page instead). Returning true here means "no auth yet, show everyone".
function requireAuth() {
  return true;
}

// Reveals the header slot and puts the signed-in user's name in it.
function setCurrentUser(name) {
  const box = document.getElementById('user-box');
  const label = document.getElementById('user-name');
  if (!box || !label) return;

  if (name) {
    label.textContent = name;
    box.classList.add('is-signed-in');
  } else {
    label.textContent = '';
    box.classList.remove('is-signed-in');
  }
}

// Registers what the logout button should do. Unwired until Person 3 calls it,
// which is why the button stays hidden alongside the name.
function onLogout(handler) {
  const btn = document.getElementById('logout-btn');
  if (btn) btn.addEventListener('click', handler);
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

  // The recipe table is NOT rendered from this payload any more. It has its own
  // endpoint and its own lifecycle - see loadRecipes().
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

// =============================================================================
// STEP 3 - DATA INTERACTION: keyword search, diet filter, pagination.
//
// None of this filters or slices in the browser. Each of these functions renders
// exactly what one /api/recipes response contained; changing a control sends a
// new query and asks the database for the answer.
// =============================================================================
const RECIPE_COLUMNS = 6;

const recipeBody = () => document.getElementById('recipe-body');

// A single full-width row: empty state, error, or "searching".
function setRecipeMessage(text) {
  const body = recipeBody();
  body.innerHTML = '';
  const td = document.createElement('td');
  td.colSpan = RECIPE_COLUMNS;
  td.className = 'px-3 py-6 text-center text-gray-500';
  td.textContent = text; // textContent, so an error string can never inject markup
  const tr = document.createElement('tr');
  tr.appendChild(td);
  body.appendChild(tr);
}

async function loadRecipes() {
  const reqId = ++explorer.requestId;
  const body = recipeBody();
  body.classList.add('results-loading');

  try {
    const data = await fetchRecipes({
      q: explorer.q,
      dietType: explorer.dietType,
      page: explorer.page,
      pageSize: explorer.pageSize,
      sort: explorer.sort,
    });

    // A newer keystroke already fired a newer search; this answer is stale.
    if (reqId !== explorer.requestId) return;

    // Trust the server's echo rather than local state: it clamps the page, so
    // asking for page 400 of a 30-page result comes back as the page it served.
    explorer.page = data.page;
    explorer.totalPages = data.total_pages;

    renderRecipeRows(data.items);
    renderRecipeSummary(data);
    renderPagination();
  } catch (err) {
    if (reqId !== explorer.requestId) return;
    setRecipeMessage(`Could not load recipes: ${err.message}`);
    document.getElementById('recipe-summary').textContent = '';
    document.getElementById('pagination').innerHTML = '';
  } finally {
    if (reqId === explorer.requestId) body.classList.remove('results-loading');
  }
}

function renderRecipeRows(items) {
  if (!Array.isArray(items) || items.length === 0) {
    setRecipeMessage(
      explorer.q
        ? `No recipes match "${explorer.q}". Try a shorter keyword, or clear the filters.`
        : 'No recipes to show.'
    );
    return;
  }

  const body = recipeBody();
  body.innerHTML = '';
  items.forEach((r, idx) => {
    const tr = document.createElement('tr');
    tr.className = idx % 2 ? 'bg-gray-50' : '';
    // dietColor() resolves through a fixed palette map, so the only thing
    // reaching the style attribute is one of our own hex constants.
    tr.innerHTML = `
      <td class="px-3 py-2 whitespace-nowrap">
        <span class="diet-dot" style="background:${dietColor(r.Diet_type)}"></span>${escapeHtml(r.Diet_type ?? '-')}
      </td>
      <td class="px-3 py-2">${escapeHtml(r.Recipe_name ?? '-')}</td>
      <td class="px-3 py-2 text-gray-600">${escapeHtml(r.Cuisine_type ?? '-')}</td>
      <td class="px-3 py-2 text-right">${fmt(r['Protein(g)'])}</td>
      <td class="px-3 py-2 text-right">${fmt(r['Carbs(g)'])}</td>
      <td class="px-3 py-2 text-right">${fmt(r['Fat(g)'])}</td>`;
    body.appendChild(tr);
  });
}

// "Showing 26-50 of 732 recipes matching "chicken" in keto". The total comes
// from the server's COUNT, which is the only reason we can state it without
// having downloaded all 732 rows.
function renderRecipeSummary(data) {
  const el = document.getElementById('recipe-summary');
  const total = data.total || 0;

  if (total === 0) {
    el.textContent = 'No matching recipes';
    return;
  }

  const first = (data.page - 1) * data.page_size + 1;
  const last = Math.min(data.page * data.page_size, total);
  const scope = [];
  if (data.query && data.query.q) scope.push(`matching "${data.query.q}"`);
  if (data.query && data.query.diet_type && data.query.diet_type !== 'all') {
    scope.push(`in ${data.query.diet_type}`);
  }

  el.textContent =
    `Showing ${first.toLocaleString()}-${last.toLocaleString()} of ` +
    `${total.toLocaleString()} recipes${scope.length ? ' ' + scope.join(' ') : ''}`;
}

/**
 * Which page numbers to draw. 7,806 recipes is 313 pages at 25 a page, so a
 * button per page is not an option - this keeps the ends, a window around the
 * current page, and marks the jumps between them.
 *
 *   pageWindow(1, 313)   -> [1, 2, 3, 'gap', 313]
 *   pageWindow(50, 313)  -> [1, 'gap', 48, 49, 50, 51, 52, 'gap', 313]
 *   pageWindow(2, 4)     -> [1, 2, 3, 4]
 */
function pageWindow(current, total, span = 2) {
  const wanted = new Set([1, total]);
  for (let p = current - span; p <= current + span; p++) {
    if (p >= 1 && p <= total) wanted.add(p);
  }

  const out = [];
  let previous = 0;
  [...wanted]
    .sort((a, b) => a - b)
    .forEach((p) => {
      if (previous && p - previous > 1) out.push('gap');
      out.push(p);
      previous = p;
    });
  return out;
}

function renderPagination() {
  const nav = document.getElementById('pagination');
  nav.innerHTML = '';
  const { page, totalPages } = explorer;

  const goTo = (p) => {
    explorer.page = Math.min(Math.max(1, p), totalPages);
    loadRecipes();
    // The pager sits below a full page of rows; bring the top of the results
    // back into view instead of leaving the reader at the bottom of page 2.
    document.getElementById('recipe-search').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  };

  const mkBtn = (label, opts = {}) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.className = opts.active
      ? 'px-3 py-1 bg-blue-600 text-white rounded'
      : 'px-3 py-1 bg-gray-300 rounded hover:bg-gray-400 disabled:opacity-40 disabled:cursor-not-allowed';
    b.disabled = !!opts.disabled;
    if (opts.onClick && !opts.disabled && !opts.active) b.addEventListener('click', opts.onClick);
    return b;
  };

  nav.appendChild(mkBtn('« First', { disabled: page <= 1, onClick: () => goTo(1) }));
  nav.appendChild(mkBtn('‹ Prev', { disabled: page <= 1, onClick: () => goTo(page - 1) }));

  pageWindow(page, totalPages).forEach((entry) => {
    if (entry === 'gap') {
      const gap = document.createElement('span');
      gap.className = 'page-gap';
      gap.textContent = '…';
      nav.appendChild(gap);
      return;
    }
    nav.appendChild(mkBtn(String(entry), { active: entry === page, onClick: () => goTo(entry) }));
  });

  nav.appendChild(mkBtn('Next ›', { disabled: page >= totalPages, onClick: () => goTo(page + 1) }));
  nav.appendChild(mkBtn('Last »', { disabled: page >= totalPages, onClick: () => goTo(totalPages) }));

  const label = document.createElement('span');
  label.className = 'text-sm text-gray-600 ml-2';
  label.textContent = `Page ${page.toLocaleString()} of ${totalPages.toLocaleString()}`;
  nav.appendChild(label);
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
  // Person 3: return false from requireAuth() (and redirect to the login page)
  // and nothing below runs - no charts, no data, no requests.
  if (!requireAuth()) return;

  const dietSelect = document.getElementById('diet-select');
  const search = document.getElementById('recipe-search');
  const sortSelect = document.getElementById('recipe-sort');
  const pageSizeSelect = document.getElementById('recipe-page-size');

  // Diet type drives both halves of the page.
  dietSelect.addEventListener('change', (e) => {
    explorer.dietType = e.target.value;
    explorer.page = 1; // a different filter means the page you were on is meaningless
    load(e.target.value);
    loadRecipes();
  });

  document.getElementById('refresh-btn').addEventListener('click', () => {
    load(dietSelect.value || 'all');
    loadRecipes();
  });

  // Keyword search. Debounced so typing "chicken" is one or two queries rather
  // than seven; Enter skips the wait.
  let searchTimer = null;
  const runSearch = () => {
    const next = search.value.trim();
    if (next === explorer.q) return; // nothing actually changed
    explorer.q = next;
    explorer.page = 1;
    loadRecipes();
  };
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, SEARCH_DEBOUNCE_MS);
  });
  search.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      clearTimeout(searchTimer);
      runSearch();
    }
  });

  sortSelect.addEventListener('change', (e) => {
    explorer.sort = e.target.value;
    explorer.page = 1;
    loadRecipes();
  });

  pageSizeSelect.addEventListener('change', (e) => {
    explorer.pageSize = Number(e.target.value) || 25;
    explorer.page = 1;
    loadRecipes();
  });

  document.getElementById('recipe-reset').addEventListener('click', () => {
    // Only re-fetch the charts if the diet filter was actually narrowing them.
    const chartsNeedReload = dietSelect.value !== 'all';

    search.value = '';
    sortSelect.value = 'name_asc';
    pageSizeSelect.value = '25';
    dietSelect.value = 'all';
    Object.assign(explorer, { q: '', dietType: 'all', sort: 'name_asc', pageSize: 25, page: 1 });

    if (chartsNeedReload) load('all');
    loadRecipes();
  });

  load('all');
  loadRecipes();
}

document.addEventListener('DOMContentLoaded', init);
