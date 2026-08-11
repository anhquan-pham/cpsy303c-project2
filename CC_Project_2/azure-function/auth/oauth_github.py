"""
GitHub OAuth flow.

Env vars required (set in Azure Function App > Settings):
    GITHUB_CLIENT_ID
    GITHUB_CLIENT_SECRET
    GITHUB_CALLBACK_URL   - must exactly match what you registered on GitHub
                             e.g. https://<your-func>.azurewebsites.net/api/auth/github/callback
"""

import os
import urllib.parse
import requests

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_USER_EMAILS_URL = "https://api.github.com/user/emails"


def build_authorize_url(state: str) -> str:
    """URL to redirect the browser to when the user clicks 'Login with GitHub'."""
    params = {
        "client_id": os.environ["GITHUB_CLIENT_ID"],
        "redirect_uri": os.environ["GITHUB_CALLBACK_URL"],
        "scope": "read:user user:email",
        "state": state,  # CSRF protection - generate + store per login attempt
    }
    return f"{GITHUB_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(code: str) -> str:
    """Step 2: swap the temporary code GitHub gave us for an access token."""
    resp = requests.post(
        GITHUB_TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": os.environ["GITHUB_CLIENT_ID"],
            "client_secret": os.environ["GITHUB_CLIENT_SECRET"],
            "code": code,
            "redirect_uri": os.environ["GITHUB_CALLBACK_URL"],
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"GitHub did not return an access token: {data}")
    return data["access_token"]


def fetch_github_profile(access_token: str) -> dict:
    """Step 3: use the access token to get the user's GitHub id/name/email."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    profile = requests.get(GITHUB_USER_URL, headers=headers, timeout=10).json()

    email = profile.get("email")
    if not email:
        # Email can be private - fetch it separately and take the primary one
        emails = requests.get(GITHUB_USER_EMAILS_URL, headers=headers, timeout=10).json()
        primary = next((e for e in emails if e.get("primary")), None)
        email = primary["email"] if primary else emails[0]["email"] if emails else None

    if not email:
        raise ValueError("Could not get an email address from GitHub for this user")

    return {
        "oauth_id": str(profile["id"]),
        "name": profile.get("name") or profile.get("login"),
        "email": email,
    }