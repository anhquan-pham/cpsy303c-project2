# Nutritional Insights Dashboard (Cloud Computing Phase 2)

Frontend dashboard that fetches diet-analysis results from the deployed Azure
Function and renders them as four visualizations plus a paginated table.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | Dashboard markup, CDN tags (Tailwind 2.0, Chart.js 4.4), styles. |
| `app.js` | Fetch layer, chart rendering, controls, pagination. All logic. |
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
