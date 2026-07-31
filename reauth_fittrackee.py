#!/usr/bin/env python3
"""Re-authorize the Strava-to-FitTrackee OAuth app and write a fresh token file.

FitTrackee v1.2.2 expires refresh tokens at issued_at + 2*expires_in (~20 days).
When that passes, refresh returns invalid_grant forever and the app must be
re-authorized. This script performs the OAuth2 authorization-code flow with
PKCE against the FitTrackee web UI and writes the new token file.

Two-step usage (run inside the strava-sync container):

  Step 1 - print the authorize URL (saves the PKCE verifier/state to /tmp):
      python3 reauth_fittrackee.py --generate

  Step 2 - after the user authorizes in a browser and copies the redirected
  callback URL, exchange the code for tokens:
      python3 reauth_fittrackee.py --exchange 'https://fit.wwbb.duia.eu/?code=...&state=...'
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.parse

import requests

TOKEN_FILE = os.environ.get("FITTRACKEE_TOKEN_FILE", ".fittrackee.tokens.json")
PKCE_STATE_FILE = os.environ.get("PKCE_STATE_FILE", "/tmp/reauth_pkce.json")


def load_env():
    env = {}
    path = os.environ.get("ENV_FILE", "/app/.env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k] = v
    return env


def get_config():
    env = load_env()
    client_id = os.environ.get("FITTRACKEE_CLIENT_ID") or env.get("FITTRACKEE_CLIENT_ID")
    client_secret = os.environ.get("FITTRACKEE_CLIENT_SECRET") or env.get("FITTRACKEE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("FITTRACKEE_CLIENT_ID/SECRET not found in env or /app/.env")
        sys.exit(1)

    public_host = os.environ.get("FITTRACKEE_PUBLIC_HOST") or env.get(
        "FITTRACKEE_PUBLIC_HOST"
    ) or "fit.wwbb.duia.eu"
    redirect_uri = env.get("FITTRACKEE_REDIRECT_URI") or f"https://{public_host}"
    authorize_page = f"https://{public_host}/profile/apps/authorize"

    host = os.environ.get("FITTRACKEE_HOST") or env.get("FITTRACKEE_HOST") or public_host
    port = os.environ.get("FITTRACKEE_PORT") or env.get("FITTRACKEE_PORT")
    if port:
        token_url = f"http://{host}:{port}/api/oauth/token"
    else:
        token_url = f"https://{host}/api/oauth/token"

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "authorize_page": authorize_page,
        "token_url": token_url,
        "scope": "profile:read workouts:read workouts:write",
    }


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate(cfg):
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    with open(PKCE_STATE_FILE, "w") as f:
        json.dump({"verifier": verifier, "state": state}, f)

    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": cfg["redirect_uri"],
        "scope": cfg["scope"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{cfg['authorize_page']}?{urllib.parse.urlencode(params)}"
    print("AUTHORIZE_URL:" + url)


def exchange(cfg, callback):
    if not os.path.exists(PKCE_STATE_FILE):
        print(f"No PKCE state at {PKCE_STATE_FILE} - run --generate first")
        sys.exit(1)
    with open(PKCE_STATE_FILE) as f:
        saved = json.load(f)

    parsed = urllib.parse.urlparse(callback)
    query = urllib.parse.parse_qs(parsed.query)
    if "code" not in query:
        print("No code found in that URL.")
        sys.exit(1)
    code = query["code"][0]
    if query.get("state", [""])[0] != saved["state"]:
        print("state mismatch (possible CSRF), aborting.")
        sys.exit(1)

    data = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": saved["verifier"],
        "redirect_uri": cfg["redirect_uri"],
    }
    r = requests.post(cfg["token_url"], data=data, timeout=15)
    if r.status_code != 200:
        print(f"Token exchange failed: {r.status_code} {r.text[:300]}")
        sys.exit(1)
    tokens = r.json()

    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    print(f"OK: wrote {TOKEN_FILE}")
    print("expires_at:", tokens.get("expires_at"))
    print("Sync should work again on the next hourly run.")


def main():
    cfg = get_config()
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "--generate":
        generate(cfg)
    elif sys.argv[1] == "--exchange":
        if len(sys.argv) < 3:
            print("usage: reauth_fittrackee.py --exchange '<callback_url>'")
            sys.exit(1)
        exchange(cfg, sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
