# READMEFIRST — AI Agent Context

This file is for AI coding agents. It provides the essential context needed to
understand and modify this project correctly.

## Project Overview

Syncs workouts from **Strava** to a self-hosted **FitTrackee** instance.
Two parallel pipelines exist: the upstream `s2f.py` library and custom scripts.

## Deployment

The production stack runs via `/opt/fittrackee/docker-compose.yaml` (NOT
`strava-sync-src/docker-compose.yml`, which is a deprecated host-network
version). Three containers share the `fittrackee_net` bridge:

| Container | Hostname | Purpose |
|---|---|---|
| `fittrackee_db` (service: `fittrackee-db`) | `fittrackee-db` | PostgreSQL 17 + PostGIS |
| `fittrackee_app` (service: `fittrackee`) | `fittrackee` | FitTrackee v1.2.2 on port 5000 (mapped to host 5001) |
| `strava-sync` (service: `strava-sync`) | — | Sync container, runs every 3600s |

Strava-sync reaches FitTrackee API via `http://fittrackee:5000` and PostgreSQL
via `fittrackee-db:5432`. These hostnames are set via environment variables in
the compose file, NOT the `.env` file.

## Architecture

```
run_sync.sh  ←  CMD in Dockerfile, runs every hour
  ├── sync_raw.py      →  Fetch 2 hardcoded Strava IDs, raw GPX → FT API
  ├── merge_aw.py       →  Pair dual-device rides, merge GPX → FT API
  └── inline SQL        →  Clean speed>100 in FT PostgreSQL DB

s2f.py --sync  ←  Upstream library, NOT called by run_sync.sh
  ├── StravaConnector      OAuth2 + rate-limit handling
  ├── FitTrackeeConnector  OAuth2 + sport type mapping
  └── sync_strava_with_fittrackee()  incremental sync

cleanup_dupes.py / remove_dupes.py  ←  Standalone, PostgreSQL-level dedup
merge_duplicates.py  ←  Standalone, urllib-based merge
```

## Files and Their Roles

### Custom Pipeline (production, run by `run_sync.sh`)

| File | Role |
|---|---|
| `run_sync.sh` | Orchestrator: runs sync_raw.py → merge_aw.py → SQL cleanup |
| `sync_raw.py` | Fetches 2 hardcoded Strava IDs, builds raw GPX (no GPS filter), uploads |
| `merge_aw.py` | Finds cycling pairs on a target date, merges AW GPS + XOSS cadence |

### Upstream Library (`strava_to_fittrackee/`)

| File | Role |
|---|---|
| `s2f.py` | Main module: `StravaConnector`, `FitTrackeeConnector`, `Activity`, sync/upload/download/delete |
| `merge_duplicates.py` | Standalone urllib-based merge with dry-run support |
| `s2f.py.bak*` | Older backup without HR/cadence/namespace fix |

### Standalone Cleanup

| File | Role |
|---|---|
| `cleanup_dupes.py` | Compares Strava vs FT workouts, deletes extras from PostgreSQL directly |
| `remove_dupes.py` | Identical to cleanup_dupes.py |

### Config

| File | Role |
|---|---|
| `docker-compose.yml` (in repo) | Deprecated host-network version |
| `/opt/fittrackee/docker-compose.yaml` | **Active** production stack |
| `Dockerfile` | Builds `shanbs/strava-to-fittrackee:latest` |
| `.env` | OAuth2 client IDs/secrets + `FITTRACKEE_HOST` (overridden by compose env vars in prod) |
| `.env.example` | Template (placeholder values) |
| `.strava.tokens.json` | Live Strava OAuth tokens (gitignored) |
| `.fittrackee.tokens.json` | Live FitTrackee OAuth tokens (gitignored) |

## Environment Variables

| Variable | Default | Set In | Used By |
|---|---|---|---|
| `FITTRACKEE_HOST` | `fittrackee` | compose env, .env | sync_raw.py, merge_aw.py, s2f.py |
| `FITTRACKEE_PORT` | `5000` | compose env | sync_raw.py, merge_aw.py |
| `FITTRACKEE_CLIENT_ID` | — | .env | s2f.py |
| `FITTRACKEE_CLIENT_SECRET` | — | .env | s2f.py |
| `STRAVA_CLIENT_ID` | — | .env | s2f.py |
| `STRAVA_CLIENT_SECRET` | — | .env | s2f.py |
| `POSTGRES_HOST` | `fittrackee-db` | compose env | run_sync.sh, cleanup_dupes.py |
| `POSTGRES_PASSWORD` | — | compose env | run_sync.sh, cleanup_dupes.py |
| `STRAVA_TOKEN_FILE` | `.strava.tokens.json` | .env | s2f.py |
| `FITTRACKEE_TOKEN_FILE` | `.fittrackee.tokens.json` | .env | s2f.py |

**Important**: The compose-file `environment` keys override `.env` values at
runtime. The `.env` `FITTRACKEE_HOST=fit.wwbb.duia.eu` is ignored in Docker
because the compose sets `FITTRACKEE_HOST=fittrackee`.

## Hardcoded Values (Security & Maintenance)

**Live tokens in source code** (committed in git):

- `sync_raw.py` lines 10-11: Strava + FitTrackee bearer tokens
- `merge_aw.py` lines 14-15: Same tokens duplicated

These will expire and break the sync. The upstream `s2f.py` handles OAuth2
refresh properly; the custom scripts do not.

**Other hardcoded values:**
- `sync_raw.py:20`: `TARGET_IDS = [17675801433, 17671394514]`
- `merge_aw.py:21`: `TARGET_DATE = "2026-03-10"`
- `merge_duplicates.py`: Absolute paths (`/opt/fittrackee/...`)
- `docker-compose.yml` (in repo): `POSTGRES_HOST=172.24.0.2` (hardcoded IP)

## GPX Namespace Fix

`s2f.py` has `_fix_gpxtpx_namespace()` that post-processes GPX XML to ensure
FitTrackee-compatible namespace prefixes for HR/cadence extensions.
Without this, FitTrackee rejects `<hr>`/`<cad>` elements.

## Common Connection Issues

- **`127.0.0.1:5001` Connection refused** — Hardcoded localhost URL used
  inside Docker container where `127.0.0.1` points to the container itself.
  Fix: use `FITTRACKEE_HOST`/`FITTRACKEE_PORT` env vars (`fittrackee:5000`).
- **DB "no password supplied"** — Inline SQL in `run_sync.sh` was missing
  the `password=` parameter. Fixed to read `POSTGRES_PASSWORD` env var.
- **FT API not ready** — strava-sync container depends on `condition:
  service_started` for fittrackee, not `service_healthy`. If FT is still
  starting up, HTTP calls will get Connection refused.
