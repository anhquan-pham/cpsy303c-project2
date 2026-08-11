/*
 * Login page logic. Depends on auth.js being loaded first (API_BASE, setToken,
 * setUser). Two modes on one form: login (default) and register, toggled by
 * showing/hiding the name field and swapping which endpoint gets called.
 */

let mode = 'login'; // 'login' | 'register'

const statusEl = () => document.getElementById('status-banner');

function showStatus(kind, message) {
  const el = statusEl();
  el.textContent = message;
  el.className =
    'status-banner ' +
    (kind === 'error'
      ? 'bg-red-100 text-red-800 border border-red-300'
      : 'bg-blue-100 text-blue-800 border border-blue-300');
  el.style.display = 'block';
}

function hideStatus() {
  statusEl().style.display = 'none';
}

function applyMode() {
  const isRegister = mode === 'register';
  document.getElementById('name-field').style.display = isRegister ? 'block' : 'none';
  document.getElementById('name').required = isRegister;
  document.getElementById('submit-btn').textContent = isRegister ? 'Create account' : 'Log in';
  document.getElementById('toggle-prompt').textContent = isRegister
    ? 'Already have an account?'
    : "Don't have an account?";
  document.getElementById('toggle-mode').textContent = isRegister ? 'Log in' : 'Register';
  document.getElementById('password').autocomplete = isRegister ? 'new-password' : 'current-password';
}

function toggleMode() {
  mode = mode === 'login' ? 'register' : 'login';
  hideStatus();
  applyMode();
}

async function handleSubmit(e) {
  e.preventDefault();
  hideStatus();

  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  const name = document.getElementById('name').value.trim();
  const submitBtn = document.getElementById('submit-btn');

  if (mode === 'register' && !name) {
    showStatus('error', 'Please enter your name.');
    return;
  }

  const endpoint = mode === 'register' ? '/api/register' : '/api/login';
  const body = mode === 'register' ? { name, email, password } : { email, password };

  submitBtn.disabled = true;
  submitBtn.textContent = mode === 'register' ? 'Creating account...' : 'Logging in...';

  try {
    const res = await fetch(`${API_BASE}${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();

    if (!res.ok) {
      showStatus('error', data.error || 'Something went wrong. Please try again.');
      return;
    }

    setToken(data.token);
    setUser({ name: data.name, email: data.email });
    window.location.href = 'index.html';
  } catch (err) {
    showStatus('error', `Could not reach the server: ${err.message}`);
  } finally {
    submitBtn.disabled = false;
    applyMode();
  }
}

function goToGithubLogin() {
  window.location.href = `${API_BASE}/api/auth/github/login`;
}

function init() {
  applyMode();
  document.getElementById('auth-form').addEventListener('submit', handleSubmit);
  document.getElementById('toggle-mode').addEventListener('click', toggleMode);
  document.getElementById('github-login-btn').addEventListener('click', goToGithubLogin);

  // If someone already has a valid session and lands on login.html directly,
  // send them straight to the dashboard instead of making them log in again.
  if (getToken()) {
    fetchCurrentUser(getToken()).then((user) => {
      if (user) window.location.href = 'index.html';
    });
  }
}

document.addEventListener('DOMContentLoaded', init);