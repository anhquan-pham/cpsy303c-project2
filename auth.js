/*
 * Auth helpers for the Nutritional Insights dashboard.
 *
 * Loaded BEFORE app.js on index.html (see index.html's <script> order) and
 * loaded on login.html too - this is the one shared auth module both pages use.
 *
 * API_BASE lives here (not in app.js) because both login.html and index.html
 * need it, and a `const` declared in two separately-loaded classic <script>
 * tags on the same page throws a SyntaxError. app.js's own `const API_BASE`
 * line was removed - it now just uses this one.
 */

// LOCAL TESTING: pointed at localhost:7071 (your func start). Switch this
// back to the deployed URL before pushing/deploying for real:
// 'https://diet-analysis-func-group9.azurewebsites.net'
const API_BASE = 'https://diet-analysis-func-group9.azurewebsites.net';

const TOKEN_KEY = 'auth_token';
const USER_KEY = 'auth_user';

// -----------------------------------------------------------------------------
// Storage - sessionStorage, not localStorage. Session-scoped is a reasonable
// default for a class project login; swap to localStorage if you want "stay
// logged in across browser restarts" instead.
// -----------------------------------------------------------------------------
function getToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

function setToken(token) {
  sessionStorage.setItem(TOKEN_KEY, token);
}

function getUser() {
  const raw = sessionStorage.getItem(USER_KEY);
  return raw ? JSON.parse(raw) : null;
}

function setUser(user) {
  sessionStorage.setItem(USER_KEY, JSON.stringify(user));
}

function clearAuth() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
}

function logout() {
  clearAuth();
  window.location.href = 'login.html';
}

// -----------------------------------------------------------------------------
// GitHub OAuth lands back here with the token in the URL fragment (see
// auth_routes.py's github_callback: redirects to `${dashboard_url}/#token=...`).
// Pull it out and scrub it from the URL so it doesn't linger in browser history.
// -----------------------------------------------------------------------------
function consumeTokenFromUrlHash() {
  if (!window.location.hash.startsWith('#token=')) return null;
  const token = decodeURIComponent(window.location.hash.slice('#token='.length));
  history.replaceState(null, '', window.location.pathname + window.location.search);
  return token;
}

// Ask the backend who this token actually belongs to. This also acts as a
// liveness check - an expired or tampered token gets rejected here instead of
// the frontend just trusting whatever's sitting in storage.
async function fetchCurrentUser(token) {
  try {
    const res = await fetch(`${API_BASE}/api/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

// -----------------------------------------------------------------------------
// Call this first thing in any protected page's init(). Returns true and
// leaves the user/token in storage if the session is valid; otherwise sends
// the browser to login.html and returns false.
// -----------------------------------------------------------------------------
async function requireAuth() {
  const hashToken = consumeTokenFromUrlHash();
  if (hashToken) setToken(hashToken);

  const token = getToken();
  if (!token) {
    window.location.href = 'login.html';
    return false;
  }

  const user = await fetchCurrentUser(token);
  if (!user) {
    clearAuth();
    window.location.href = 'login.html';
    return false;
  }

  setUser(user);
  return true;
}