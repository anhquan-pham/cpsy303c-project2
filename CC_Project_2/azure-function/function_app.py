"""
Diet Analysis - Azure Function (HTTP trigger, Python v2 model)

Reads the All_Diets.csv dataset from Azure Blob Storage, runs the Phase 1
data analysis, and returns the results as JSON for the dashboard to chart.

Endpoint (after deploy):
    GET https://<your-function-app>.azurewebsites.net/api/insights
    Optional query param:  ?diet_type=keto   (filter to one diet; "all" = no filter)

App settings this function reads (set these in Azure > Function App > Settings > Environment variables):
    BLOB_CONNECTION_STRING  - storage account connection string (falls back to AzureWebJobsStorage)
    BLOB_CONTAINER          - container name that holds the CSV        (default: data)
    BLOB_NAME               - the CSV file name                        (default: All_Diets.csv)
"""

import io
import os
import json
import time
import logging
from datetime import datetime, timezone

import azure.functions as func
import pandas as pd
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

MACROS = ["Protein(g)", "Carbs(g)", "Fat(g)"]
# NOTE: CORS is handled in the Azure portal (Function App > API > CORS).
# Do NOT set Access-Control-Allow-Origin here too, or the browser sees a
# duplicate header and rejects the response. Configure allowed origins in the portal.


def _connection_string() -> str:
    """Prefer a dedicated setting, but fall back to the built-in one so it still works."""
    return os.environ.get("BLOB_CONNECTION_STRING") or os.environ.get("AzureWebJobsStorage", "")


def _load_dataframe() -> pd.DataFrame:
    """Download the CSV from Blob Storage and return it as a DataFrame."""
    conn = _connection_string()
    if not conn:
        raise RuntimeError(
            "No storage connection string found. Set BLOB_CONNECTION_STRING "
            "(or AzureWebJobsStorage) in the Function App's environment variables."
        )
    container = os.environ.get("BLOB_CONTAINER", "data")
    blob_name = os.environ.get("BLOB_NAME", "All_Diets.csv")

    service = BlobServiceClient.from_connection_string(conn)
    blob = service.get_blob_client(container=container, blob=blob_name)
    raw = blob.download_blob().readall()
    return pd.read_csv(io.BytesIO(raw))


def _build_payload(df: pd.DataFrame, diet_filter: str | None) -> dict:
    start = time.perf_counter()

    required = ["Diet_type"] + MACROS
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"Dataset is missing columns {missing}. Columns found: {list(df.columns)}"
        )

    # Clean: fill numeric gaps with the column mean (matches the Phase 1 script)
    num_cols = df.select_dtypes(include="number").columns
    df[num_cols] = df[num_cols].fillna(df[num_cols].mean())

    # Optional filter (drives the dashboard dropdown / search box)
    if diet_filter and diet_filter.lower() != "all":
        df = df[df["Diet_type"].str.lower() == diet_filter.lower()]

    # 1) Average macros per diet type  -> bar chart
    avg = df.groupby("Diet_type")[MACROS].mean().round(2).reset_index()
    avg_macros = avg.to_dict(orient="records")

    # 2) Recipe count per diet type    -> pie chart
    counts = df["Diet_type"].value_counts().reset_index()
    counts.columns = ["Diet_type", "count"]
    diet_counts = counts.to_dict(orient="records")

    # 3) Protein vs Carbs per recipe   -> scatter plot (sampled to keep payload small)
    sc = df[["Diet_type"] + MACROS].dropna()
    if len(sc) > 500:
        sc = sc.sample(500, random_state=1)
    scatter = sc[["Diet_type", "Protein(g)", "Carbs(g)"]].round(2).to_dict(orient="records")

    # 4) Correlation between macros     -> heatmap
    corr = df[MACROS].corr().round(3)
    correlations = {"labels": MACROS, "matrix": corr.values.tolist()}

    # Bonus: top 5 protein-rich recipes per diet type -> table
    keep = ["Diet_type"] + (["Recipe_name"] if "Recipe_name" in df.columns else []) + MACROS
    top = df.sort_values("Protein(g)", ascending=False).groupby("Diet_type").head(5)
    top_protein = top[keep].round(2).to_dict(orient="records")

    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

    return {
        "metadata": {
            "dataset": os.environ.get("BLOB_NAME", "All_Diets.csv"),
            "row_count": int(len(df)),
            "diet_types": sorted(df["Diet_type"].dropna().unique().tolist()),
            "execution_time_ms": elapsed_ms,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "avg_macros": avg_macros,
        "diet_counts": diet_counts,
        "scatter_protein_vs_carbs": scatter,
        "correlations": correlations,
        "top_protein_recipes": top_protein,
    }


@app.route(route="insights", methods=["GET"])
def insights(req: func.HttpRequest) -> func.HttpResponse:
    """Main dashboard endpoint: returns all chart data as JSON."""
    logging.info("insights endpoint called")
    diet_filter = req.params.get("diet_type")
    try:
        df = _load_dataframe()
        payload = _build_payload(df, diet_filter)
        return func.HttpResponse(json.dumps(payload), mimetype="application/json")
    except Exception as exc:  # noqa: BLE001 - return a clean error to the caller
        logging.exception("insights failed")
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=500,
            mimetype="application/json",
        )


@app.route(route="ping", methods=["GET"])
def ping(req: func.HttpRequest) -> func.HttpResponse:
    """Quick health check so you can confirm the app is live before wiring up the dashboard."""
    body = {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
    return func.HttpResponse(json.dumps(body), mimetype="application/json")
