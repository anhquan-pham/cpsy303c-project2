"""
Diet Analysis - Azure Function (Phase 3: caching + precomputed results)

Phase 2 recomputed the entire dataset on every single HTTP request. Phase 3
splits that into two halves so the expensive work happens once per file change:

  1. on_csv_change - Event Grid blob trigger. Fires ONLY when All_Diets.csv is
                     written to blob storage. Cleans the data, writes the
                     cleaned CSV back to a separate container, precomputes
                     every visualization payload, and stores the results plus
                     the cleaned recipe rows in Azure SQL.

  2. insights      - HTTP endpoint. Serves the precomputed payload. No blob
                     download, no pandas, no recompute.

  3. rebuild       - HTTP endpoint (function key required). Runs the same
                     pipeline on demand. Used to populate the cache the first
                     time, before the trigger has ever fired.

WHY THE TRIGGER LOOKS LIKE THIS: this Function App runs on the Flex Consumption
plan, which does not support the classic polling blob trigger. The trigger below
uses source=EVENT_GRID, which needs an Event Grid system topic on the storage
account routing BlobCreated events here.

WHY THERE IS A MEMORY CACHE: the SQL database is serverless and auto-pauses when
idle, so a cold connection can take 30-60s to resume. Warm function instances
serve visualization requests straight out of process memory and never open a
connection at all. SQL stays the source of truth; memory is just the fast path.

AUTHENTICATION: the function connects to SQL with its system-assigned managed
identity, so no database password exists anywhere - not in the repo, not in app
settings. Blob access still uses a connection string (Phase 2 behaviour).

App settings this function reads (Function App > Settings > Environment variables):
    BLOB_CONNECTION_STRING - storage connection string (falls back to AzureWebJobsStorage)
    BLOB_CONTAINER         - container holding the raw CSV   (default: data)
    BLOB_NAME              - the raw CSV file name           (default: All_Diets.csv)
    CLEAN_CONTAINER        - container for the cleaned CSV   (default: cleaned)
    CLEAN_BLOB_NAME        - the cleaned CSV file name       (default: All_Diets_clean.csv)
    SQL_SERVER             - e.g. diet-analysis-sql-group9.database.windows.net
    SQL_DATABASE           - database name                   (default: dietdb)
"""

import json
import os
import secrets
import logging
import azure.functions as func
from auth.security import hash_password, verify_password, create_session_token, verify_session_token, get_bearer_token
from auth.user_store import get_user_store
from auth import oauth_github

import os
import io
import json
import time
import struct
import logging
from datetime import datetime, timezone

import azure.functions as func
import pyodbc
from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential

import secrets
from auth.security import hash_password, verify_password, create_session_token, verify_session_token, get_bearer_token
from auth.user_store import get_user_store
from auth import oauth_github

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

_pending_oauth_states = set()

LOCAL_DEV_CORS_ORIGIN = os.environ.get("LOCAL_DEV_CORS_ORIGIN")

def _cors_headers() -> dict:
    if not LOCAL_DEV_CORS_ORIGIN:
        return {}
    return {
        "Access-Control-Allow-Origin": LOCAL_DEV_CORS_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    }


@app.route(route="{*path}", methods=["OPTIONS"])
def cors_preflight(req: func.HttpRequest) -> func.HttpResponse:
    """Answers every OPTIONS preflight. Only adds real headers locally (see above)."""
    return func.HttpResponse(status_code=204, headers=_cors_headers())

def _json_response(body: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(body),
        status_code=status,
        mimetype="application/json",
        headers=_cors_headers(),
    )


@app.route(route="register", methods=["POST"])
def register(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _json_response({"error": "Invalid JSON body"}, 400)

    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    if not name or not email or not password:
        return _json_response({"error": "name, email, and password are required"}, 400)
    if len(password) < 8:
        return _json_response({"error": "Password must be at least 8 characters"}, 400)

    store = get_user_store(_sql_connect)
    if store.get_by_email(email):
        return _json_response({"error": "An account with that email already exists"}, 409)

    user = store.create_user(name=name, email=email, password_hash=hash_password(password))
    token = create_session_token(user["id"], user["name"], user["email"])
    return _json_response({"token": token, "name": user["name"], "email": user["email"]}, 201)


@app.route(route="login", methods=["POST"])
def login(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _json_response({"error": "Invalid JSON body"}, 400)

    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    store = get_user_store(_sql_connect)
    user = store.get_by_email(email)

    # Same error for "no such user" and "wrong password" - don't leak which one
    if not user or not user.get("password_hash") or not verify_password(password, user["password_hash"]):
        return _json_response({"error": "Invalid email or password"}, 401)

    token = create_session_token(user["id"], user["name"], user["email"])
    return _json_response({"token": token, "name": user["name"], "email": user["email"]})


@app.route(route="auth/github/login", methods=["GET"])
def github_login(req: func.HttpRequest) -> func.HttpResponse:
    state = secrets.token_urlsafe(16)
    _pending_oauth_states.add(state)
    redirect_url = oauth_github.build_authorize_url(state)
    return func.HttpResponse(status_code=302, headers={"Location": redirect_url})


@app.route(route="auth/github/callback", methods=["GET"])
def github_callback(req: func.HttpRequest) -> func.HttpResponse:
    code = req.params.get("code")
    state = req.params.get("state")

    if not state or state not in _pending_oauth_states:
        return _json_response({"error": "Invalid or expired OAuth state"}, 400)
    _pending_oauth_states.discard(state)

    if not code:
        return _json_response({"error": "Missing code from GitHub"}, 400)

    try:
        access_token = oauth_github.exchange_code_for_token(code)
        profile = oauth_github.fetch_github_profile(access_token)
    except Exception as exc:
        logging.exception("GitHub OAuth failed")
        return _json_response({"error": "GitHub login failed"}, 502)

    store = get_user_store(_sql_connect)
    user = store.get_by_oauth("github", profile["oauth_id"])
    if not user:
        # If an email/password account already exists with this email, this
        # simple version creates a separate OAuth account - fine for the
        # class project; call this out in the demo as a known simplification.
        user = store.create_user(
            name=profile["name"],
            email=profile["email"],
            oauth_provider="github",
            oauth_id=profile["oauth_id"],
        )

    token = create_session_token(user["id"], user["name"], user["email"])

    # Redirect back to the dashboard with the token in the URL fragment
    # (not query string, so it isn't logged by the server) - frontend reads
    # it from window.location.hash on load, stores it, then clears the hash.
    # dashboard_url = "https://zealous-sea-0a752020f.7.azurestaticapps.net"
    dashboard_url = "http://localhost:5500"  # For local testing; change to the deployed URL in production
    return func.HttpResponse(
        status_code=302,
        headers={"Location": f"{dashboard_url}/#token={token}"},
    )


@app.route(route="me", methods=["GET"])
def me(req: func.HttpRequest) -> func.HttpResponse:
    token = get_bearer_token(dict(req.headers))
    if not token:
        return _json_response({"error": "Missing token"}, 401)

    payload = verify_session_token(token)
    if not payload:
        return _json_response({"error": "Invalid or expired token"}, 401)

    return _json_response({"name": payload["name"], "email": payload["email"]})

MACROS = ["Protein(g)", "Carbs(g)", "Fat(g)"]

# Cosmos-style "all diets" key, kept as the id for the unfiltered payload.
ALL_KEY = "all"

# pyodbc attribute id for handing SQL an Entra access token instead of a password.
SQL_COPT_SS_ACCESS_TOKEN = 1256

# In-process cache: {diet_key: (cached_at_epoch, build_id, payload_dict)}.
#
# Entries expire after _MEM_TTL_SECONDS. The TTL is not a performance knob, it is
# a correctness one: the pipeline runs on whichever instance the trigger lands on
# and can only clear ITS OWN memory, so every other warm instance would keep
# serving the previous build indefinitely. The TTL bounds how long a dashboard
# can show pre-update numbers after All_Diets.csv changes.
_MEM_CACHE: dict[str, tuple[float, str, dict]] = {}
_MEM_TTL_SECONDS = 30.0

# NOTE: CORS is handled in the Azure portal (Function App > API > CORS).
# Do NOT set Access-Control-Allow-Origin here too, or the browser sees a
# duplicate header and rejects the response.


# -----------------------------------------------------------------------------
# Blob access (unchanged from Phase 2)
#
# pandas is imported lazily inside the pipeline functions rather than at module
# scope. The HTTP read path never touches it, and skipping a heavy import keeps
# cold starts on the insights endpoint short.
# -----------------------------------------------------------------------------
def _blob_connection_string() -> str:
    """Prefer a dedicated setting, but fall back to the built-in one so it still works."""
    return os.environ.get("BLOB_CONNECTION_STRING") or os.environ.get("AzureWebJobsStorage", "")


def _blob_service() -> BlobServiceClient:
    conn = _blob_connection_string()
    if not conn:
        raise RuntimeError(
            "No storage connection string found. Set BLOB_CONNECTION_STRING "
            "(or AzureWebJobsStorage) in the Function App's environment variables."
        )
    return BlobServiceClient.from_connection_string(conn)


# -----------------------------------------------------------------------------
# SQL access via managed identity
# -----------------------------------------------------------------------------
def _odbc_driver() -> str:
    """Pick the newest installed SQL Server ODBC driver."""
    available = [d for d in pyodbc.drivers() if "SQL Server" in d]
    for preferred in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if preferred in available:
            return preferred
    if available:
        return available[0]
    raise RuntimeError(
        f"No SQL Server ODBC driver found in this image. pyodbc.drivers() = {pyodbc.drivers()}"
    )


def _sql_connect(timeout: int = 60):
    """
    Open a connection using the function's managed identity.

    The database is serverless and auto-pauses when idle; the first connection
    after a pause fails while it resumes. Retry rather than surface that to the
    dashboard as an error.
    """
    server = os.environ.get("SQL_SERVER")
    if not server:
        raise RuntimeError("SQL_SERVER is not set in the Function App's environment variables.")
    database = os.environ.get("SQL_DATABASE", "dietdb")

    token = DefaultAzureCredential().get_token("https://database.windows.net/.default").token
    token_bytes = token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)

    conn_str = (
        f"Driver={{{_odbc_driver()}}};"
        f"Server=tcp:{server},1433;"
        f"Database={database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60;"
    )

    last_error = None
    for attempt in range(4):
        try:
            return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}, timeout=timeout)
        except pyodbc.Error as exc:  # noqa: PERF203 - resume takes as long as it takes
            last_error = exc
            wait = 5 * (attempt + 1)
            logging.warning("SQL connect attempt %d failed (%s); resuming in %ds", attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Could not connect to SQL after 4 attempts: {last_error}")


def _ensure_schema(conn) -> None:
    """
    Create the tables if they are missing.

    Kept in code rather than a migration script so a fresh database bootstraps
    itself on the first pipeline run. The users table is created empty for
    Person 3's authentication work.
    """
    cur = conn.cursor()
    cur.execute("""
IF OBJECT_ID('dbo.insights', 'U') IS NULL
CREATE TABLE dbo.insights (
    diet_key    NVARCHAR(64)   NOT NULL PRIMARY KEY,
    payload     NVARCHAR(MAX)  NOT NULL,
    build_id    NVARCHAR(64)   NOT NULL,
    updated_at  DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
);

IF OBJECT_ID('dbo.recipes', 'U') IS NULL
CREATE TABLE dbo.recipes (
    id           INT            NOT NULL PRIMARY KEY,
    diet_type    NVARCHAR(64)   NOT NULL,
    recipe_name  NVARCHAR(512)  NULL,
    cuisine_type NVARCHAR(128)  NULL,
    protein_g    FLOAT          NULL,
    carbs_g      FLOAT          NULL,
    fat_g        FLOAT          NULL,
    search_text  NVARCHAR(900)  NULL,
    build_id     NVARCHAR(64)   NOT NULL
);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_recipes_diet_type')
CREATE INDEX IX_recipes_diet_type ON dbo.recipes (diet_type) INCLUDE (recipe_name, protein_g, carbs_g, fat_g);

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_recipes_search_text')
CREATE INDEX IX_recipes_search_text ON dbo.recipes (search_text);

IF OBJECT_ID('dbo.build_status', 'U') IS NULL
CREATE TABLE dbo.build_status (
    id           INT            NOT NULL PRIMARY KEY DEFAULT 1,
    build_id     NVARCHAR(64)   NOT NULL,
    summary      NVARCHAR(MAX)  NOT NULL,
    completed_at DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
);

-- Person 3 owns this table. Created here so the auth work is not blocked on the
-- pipeline. Passwords are never stored - only password_hash - and the column is
-- nullable because OAuth-only accounts have no password at all.
IF OBJECT_ID('dbo.users', 'U') IS NULL
CREATE TABLE dbo.users (
    id             NVARCHAR(64)  NOT NULL PRIMARY KEY,
    email          NVARCHAR(256) NOT NULL UNIQUE,
    display_name   NVARCHAR(256) NULL,
    password_hash  NVARCHAR(512) NULL,
    oauth_provider NVARCHAR(64)  NULL,
    oauth_subject  NVARCHAR(256) NULL,
    created_at     DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
);
""")
    conn.commit()


# -----------------------------------------------------------------------------
# The expensive half - runs once per file change, never per request.
# -----------------------------------------------------------------------------
def _clean(df):
    """Fill numeric gaps with the column mean (matches the Phase 1 script)."""
    num_cols = df.select_dtypes(include="number").columns
    df[num_cols] = df[num_cols].fillna(df[num_cols].mean())
    return df


def _build_payload(df, diet_filter: str | None) -> dict:
    """Compute every visualization result for one slice of the dataset."""
    start = time.perf_counter()

    if diet_filter and diet_filter.lower() != ALL_KEY:
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


def _write_cleaned_csv(df) -> str:
    """Write the cleaned dataset to its own container, per the Phase 3 instructions."""
    container = os.environ.get("CLEAN_CONTAINER", "cleaned")
    blob_name = os.environ.get("CLEAN_BLOB_NAME", "All_Diets_clean.csv")

    service = _blob_service()
    try:
        service.create_container(container)
    except Exception:
        pass  # already exists

    buf = io.StringIO()
    df.to_csv(buf, index=False)
    service.get_blob_client(container=container, blob=blob_name).upload_blob(
        buf.getvalue().encode("utf-8"), overwrite=True
    )
    return f"{container}/{blob_name}"


def _load_recipes(conn, df, build_id: str) -> int:
    """
    Replace the recipes table with the cleaned rows.

    Truncate-and-reload rather than diffing: a new CSV version can have fewer
    rows, and leftover rows would silently pollute Person 2's search results.
    """
    cols = {c: c for c in df.columns}
    has_name = "Recipe_name" in cols
    has_cuisine = "Cuisine_type" in cols

    rows = []
    for i, rec in enumerate(df.to_dict(orient="records")):
        name = str(rec.get("Recipe_name", "")) if has_name else ""
        cuisine = str(rec.get("Cuisine_type", "")) if has_cuisine else ""
        diet = str(rec.get("Diet_type", ""))
        # Precomputed lowercase haystack so Person 2's keyword search is one
        # LIKE against a single column instead of OR-ing across several.
        search_text = f"{name} {cuisine} {diet}".lower()[:900]
        rows.append((
            i, diet, name[:512] or None, cuisine[:128] or None,
            rec.get("Protein(g)"), rec.get("Carbs(g)"), rec.get("Fat(g)"),
            search_text, build_id,
        ))

    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE dbo.recipes;")
    cur.fast_executemany = True
    cur.executemany(
        "INSERT INTO dbo.recipes (id, diet_type, recipe_name, cuisine_type, "
        "protein_g, carbs_g, fat_g, search_text, build_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def _run_pipeline() -> dict:
    """
    The whole expensive path: clean once, calculate once, store the results.

    Called by the blob trigger (on file change) and by the rebuild endpoint
    (manual). Nothing on the HTTP read path calls this.
    """
    import pandas as pd  # lazy: only the pipeline needs pandas

    started = time.perf_counter()
    build_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    container = os.environ.get("BLOB_CONTAINER", "data")
    blob_name = os.environ.get("BLOB_NAME", "All_Diets.csv")
    raw = _blob_service().get_blob_client(container=container, blob=blob_name).download_blob().readall()
    df = pd.read_csv(io.BytesIO(raw))

    required = ["Diet_type"] + MACROS
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"Dataset is missing columns {missing}. Columns found: {list(df.columns)}"
        )

    df = _clean(df)
    cleaned_path = _write_cleaned_csv(df)

    conn = _sql_connect(timeout=120)
    try:
        _ensure_schema(conn)

        # Precompute the unfiltered payload AND one per diet type, so the
        # dashboard's diet dropdown is served from cache too.
        diet_types = sorted(df["Diet_type"].dropna().unique().tolist())
        cur = conn.cursor()
        for key in [ALL_KEY] + diet_types:
            payload = json.dumps(_build_payload(df, key))
            cur.execute("""
MERGE dbo.insights AS target
USING (SELECT ? AS diet_key, ? AS payload, ? AS build_id) AS src
ON target.diet_key = src.diet_key
WHEN MATCHED THEN UPDATE SET payload = src.payload, build_id = src.build_id, updated_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (diet_key, payload, build_id) VALUES (src.diet_key, src.payload, src.build_id);
""", key, payload, build_id)
        conn.commit()

        recipe_count = _load_recipes(conn, df, build_id)

        summary = {
            "build_id": build_id,
            "source_blob": f"{container}/{blob_name}",
            "cleaned_blob": cleaned_path,
            "row_count": int(len(df)),
            "diet_types": diet_types,
            "recipes_loaded": recipe_count,
            "payloads_written": len(diet_types) + 1,
            "pipeline_duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        cur.execute("""
MERGE dbo.build_status AS target
USING (SELECT 1 AS id, ? AS build_id, ? AS summary) AS src
ON target.id = src.id
WHEN MATCHED THEN UPDATE SET build_id = src.build_id, summary = src.summary, completed_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN INSERT (id, build_id, summary) VALUES (1, src.build_id, src.summary);
""", build_id, json.dumps(summary))
        conn.commit()
    finally:
        conn.close()

    # Clear this instance's memory cache immediately. Other instances pick the
    # new build up when their entries expire (see _MEM_TTL_SECONDS).
    global _MEM_CACHE
    _MEM_CACHE = {}

    logging.info("pipeline complete: %s", json.dumps(summary))
    return summary


# =============================================================================
# TRIGGER 1 - blob change. This is the whole point of Phase 3: cleaning and
# result calculation happen here, once, and never on a dashboard request.
# =============================================================================
@app.blob_trigger(
    arg_name="blob",
    path="data/All_Diets.csv",
    connection="BLOB_CONNECTION_STRING",
    source=func.BlobSource.EVENT_GRID,
)
def on_csv_change(blob: func.InputStream) -> None:
    logging.info("blob trigger fired for %s (%s bytes)", blob.name, blob.length)
    try:
        _run_pipeline()
    except Exception:
        logging.exception("pipeline failed")
        raise


# =============================================================================
# TRIGGER 2 - the dashboard read path. Memory first, SQL second, never recompute.
# =============================================================================
@app.route(route="insights", methods=["GET"])
def insights(req: func.HttpRequest) -> func.HttpResponse:
    """Return the precomputed payload for a diet type."""
    start = time.perf_counter()
    key = (req.params.get("diet_type") or ALL_KEY).lower()

    try:
        cached = _MEM_CACHE.get(key)
        if cached and (time.time() - cached[0]) < _MEM_TTL_SECONDS:
            _, build_id, payload = cached
            served_from = "memory"
        else:
            conn = _sql_connect()
            try:
                cur = conn.cursor()
                cur.execute("SELECT payload, build_id FROM dbo.insights WHERE diet_key = ?", key)
                row = cur.fetchone()
            finally:
                conn.close()

            if row is None:
                return func.HttpResponse(
                    json.dumps({
                        "error": f"No cached results for '{key}'.",
                        "detail": "The pipeline has not run yet. POST /api/rebuild to populate it.",
                    }),
                    status_code=503,
                    mimetype="application/json",
                    headers=_cors_headers(),

                )

            payload, build_id = json.loads(row[0]), row[1]
            _MEM_CACHE[key] = (time.time(), build_id, payload)
            served_from = "sql"

        payload.setdefault("metadata", {})
        payload["metadata"]["served_from"] = served_from
        payload["metadata"]["cache_build_id"] = build_id
        payload["metadata"]["response_time_ms"] = round((time.perf_counter() - start) * 1000, 1)

        return func.HttpResponse(json.dumps(payload), mimetype="application/json", headers=_cors_headers())
    except Exception as exc:  # noqa: BLE001 - return a clean error to the caller
        logging.exception("insights failed")
        return func.HttpResponse(
            json.dumps({"error": str(exc)}),
            status_code=500,
            mimetype="application/json",
            headers=_cors_headers()
        )


# =============================================================================
# Maintenance + health
# =============================================================================
@app.route(route="rebuild", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def rebuild(req: func.HttpRequest) -> func.HttpResponse:
    """
    Run the pipeline manually. Needed once to populate the cache before the
    trigger has ever fired. Function-key protected so it is not a public
    endpoint anyone can use to force expensive work.
    """
    try:
        return func.HttpResponse(json.dumps(_run_pipeline()), mimetype="application/json")
    except Exception as exc:  # noqa: BLE001
        logging.exception("rebuild failed")
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500, mimetype="application/json"
        )


@app.route(route="cache-status", methods=["GET"])
def cache_status(req: func.HttpRequest) -> func.HttpResponse:
    """Show when the pipeline last ran. Proves the cache is real, not a claim."""
    try:
        conn = _sql_connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT summary, completed_at FROM dbo.build_status WHERE id = 1")
            row = cur.fetchone()
        finally:
            conn.close()

        if row is None:
            body = {"status": "empty", "detail": "Pipeline has not run yet. POST /api/rebuild."}
        else:
            body = json.loads(row[0])
            body["status"] = "ready"
        return func.HttpResponse(json.dumps(body), mimetype="application/json")
    except Exception as exc:  # noqa: BLE001
        return func.HttpResponse(
            json.dumps({"error": str(exc)}), status_code=500, mimetype="application/json"
        )


@app.route(route="diag", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def diag(req: func.HttpRequest) -> func.HttpResponse:
    """
    Deployment sanity check: which ODBC drivers exist in this image, and can the
    managed identity actually open a connection. Function-key protected.
    """
    body = {"odbc_drivers": pyodbc.drivers()}
    try:
        conn = _sql_connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT SUSER_SNAME(), DB_NAME()")
            who, db = cur.fetchone()
            body["connected_as"] = who
            body["database"] = db
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        body["sql_error"] = str(exc)
    return func.HttpResponse(json.dumps(body), mimetype="application/json")


@app.route(route="ping", methods=["GET"])
def ping(req: func.HttpRequest) -> func.HttpResponse:
    """Quick health check so you can confirm the app is live."""
    body = {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}
    return func.HttpResponse(json.dumps(body), mimetype="application/json")
