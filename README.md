# Nutritional Insights Dashboard (Cloud Computing Phase 2)

Frontend dashboard that fetches diet-analysis results from the deployed Azure
Function and renders them as four visualizations plus a paginated table.
# Nutritional Insights Dashboard (Cloud Computing Phase 3)

Frontend dashboard that fetches diet-analysis results from the deployed Azure
Function and renders them as four visualizations plus a searchable, server-paged
recipe explorer.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | Dashboard markup, CDN tags (Tailwind 2.0, Chart.js 4.4), styles. |
| `app.js` | Fetch layer, chart rendering, controls, pagination. All logic. |
| `app.js` | Fetch layer, chart rendering, controls, search and pagination. All logic. |
| `sample-response.json` | Captured `GET /api/insights` response. Reference for the real data shape. |
| `UI-for-project2.html` | Original static mockup (kept for reference, not deployed). |

## The endpoint constant (Person 3: read this)

The Azure Function base URL is defined in exactly one place:

```js
// app.js, near the top
const API_BASE =
  'https://diet-analysis-func-group9-hmhehrhjeabcd3h4.canadacentral-01.azurewebsites.net';
```

Nothing else hardcodes the URL, and there is no `localhost` anywhere in the code.
When Person 1 tightens CORS to the deployed dashboard origin, update this one line
and redeploy. `GET /api/ping` (returns `{"status":"ok"}`) is a quick health check.

## Endpoints the dashboard calls

| Endpoint | Used for |
| --- | --- |
| `GET /api/insights[?diet_type=]` | The four charts and the metadata bar. Precomputed server-side. |
| `GET /api/recipes?q=&diet_type=&page=&page_size=&sort=` | One page of the recipe explorer, plus a `total`. |
| `GET /api/ping` | Health check / warm-up. |

Every one of these goes through a single function, `apiFetch()` in `app.js` — see
the auth hooks below.

## Auth integration points (Person 3)

Three stubs, all next to each other in `app.js` under `AUTH HOOK`, and one comment
block inside `apiFetch()`. Nothing else needs to change:

| Hook | What to do with it |
| --- | --- |
| `requireAuth()` | Currently returns `true`. Return `false` (and redirect to the login page) to stop the dashboard rendering — `init()` bails before any request is made. |
| `setCurrentUser(name)` | Call it after login. Fills `#user-name` and reveals the top-right header slot and its logout button. |
| `onLogout(handler)` | Registers the click handler for the logout button. |
| `apiFetch()` header block | Add `headers.Authorization` here once and every request in the app is authenticated. |

The header slot is deliberately empty and hidden until `setCurrentUser()` is called,
so an unauthenticated page never shows a blank name or a dead logout button.

## Run locally

No build step. Two options:

1. **Open the file directly:** double-click `index.html` (runs from `file://`).
2. **Static server** (recommended, matches Static Web App hosting):

   ```bash
   cd "Project Part 2"
   python3 -m http.server 8000
   # then open http://localhost:8000/index.html
   ```

The page fetches from the live Azure endpoint over HTTPS. The endpoint allows
anonymous access with CORS `*`, so it works from `file://` and localhost.

## Behavior notes

- **Cold start:** the Function runs on a consumption plan and idles out. The first
  request after idle can take 5-20s. The dashboard shows "Waking up the Azure
  Function..." during that wait instead of a blank screen. Load `/api/ping` once
  before a demo to warm it.
- **Diet filter:** the dropdown is populated from `metadata.diet_types` (not
  hardcoded). Selecting a diet re-fetches with `?diet_type=<x>`, which the Function
  recomputes for every key (metadata, charts, correlations, table). `row_count`
  updates to prove the fetch is live.
   python3 -m http.server 8000
   ```

   then open <http://localhost:8000/index.html>.

The page fetches from the live Azure endpoint over HTTPS.

## Behavior notes

- **Cold start:** the Function idles out, and Phase 3's SQL database auto-pauses on
  top of that, so the first request after a quiet stretch can take 30-60s. The
  dashboard shows "Waking up the Azure Function..." during that wait instead of a
  blank screen. Load `/api/ping` once before a demo to warm it.
- **Diet filter:** the dropdown is populated from `metadata.diet_types`, not
  hardcoded. It filters the charts *and* the recipe explorer, so one selection means
  the same thing everywhere on the page. As of Phase 3 the Function no longer
  recomputes per request — each diet's payload is built once when `All_Diets.csv`
  changes and read back from memory or SQL.
- **Refresh** re-fetches the current selection.

## Visualizations

1. Grouped bar: average protein / carbs / fat per diet type (`avg_macros`).
2. Scatter: protein vs carbs per recipe, colored by diet type (`scatter_protein_vs_carbs`).
3. Pie: recipe count per diet type (`diet_counts`).
4. Heatmap: macronutrient correlation matrix, hand-rolled CSS grid with a diverging
   color scale (`correlations`). No chart-library plugin, so no extra CDN dependency.

Below the charts, a paginated table lists the top protein recipes per diet
(`top_protein_recipes`). Function execution time (`execution_time_ms`) is shown in
the metadata bar at the top.
## Recipe explorer (Phase 3)

Below the charts. Searching, filtering, sorting and paging all happen in SQL — the
browser never holds more than one page of rows.

- **Keyword search** matches recipe name, cuisine and diet type in a single `LIKE`
  against the pipeline's precomputed `search_text` column, instead of `OR`-ing
  across three columns. (A contains-match starts with `%`, so this scans the
  `IX_recipes_search_text` index rather than seeking it — on 7,806 rows that is a
  narrow index scan and still far cheaper than three column comparisons.) Input is
  debounced 350 ms (Enter skips the wait) and sent as a bound parameter, with `%`,
  `_` and `[` escaped so they match literally instead of acting as wildcards.
- **Diet filter** is the same dropdown that drives the charts.
- **Sort** by recipe name or by any macro, ascending or descending. The `sort`
  parameter names a key in a server-side whitelist; caller input never reaches the
  `ORDER BY` clause.
- **Pagination** is `OFFSET`/`FETCH` in SQL. `total` is a `COUNT(*)`, so the pager
  knows there are 313 pages without the browser downloading 7,806 rows. The pager
  renders first/last plus a window around the current page — never one button per
  page. Changing the search, filter, sort or page size resets to page 1.

Out-of-range input is clamped rather than rejected: `page=0` serves page 1,
`page_size=9999` serves 100, and a page past the end returns an empty result
instead of an error.
