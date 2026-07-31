#!/usr/bin/env python3
"""Apple Health webhook for FitTrackee.

Receives workout JSON pushed from an iPhone Shortcut (HealthKit) and creates
workouts in FitTrackee via POST /api/workouts/no_gpx (metric-only, no map).

Endpoints:
  GET  /healthz            health check -> 200
  POST /ingest             JSON array (or {"workouts": [...]}) of workouts
                           Header: X-API-Key: <INGEST_API_KEY>
                           Body per workout:
                             type:       Apple workout type ("Running", ...)
                             start:      ISO8601 start datetime (UTC or offset)
                             end:        ISO8601 end datetime (optional)
                             distance:   number (km) or unit string ("5.2 km",
                                         "5200 m")
                             duration:   seconds, "1:23:45", or "1h 23m"
                                         (optional; falls back to end-start)
                             calories:   number or "350 kcal" (optional)
                             title:      optional display title

Run inside the strava-sync container:
  python3 health_webhook.py [port]
"""

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import requests

FT_HOST = os.environ.get("FITTRACKEE_HOST", "fittrackee")
FT_PORT = os.environ.get("FITTRACKEE_PORT", "5000")
FT_URL = f"http://{FT_HOST}:{FT_PORT}"

FITTRACKEE_CLIENT_ID = os.environ.get(
    "FITTRACKEE_CLIENT_ID", "hxyg6fVt71WzAbKO6M6OwiEB"
)
FITTRACKEE_CLIENT_SECRET = os.environ.get(
    "FITTRACKEE_CLIENT_SECRET",
    "PVbysRv51uaPot9XQB2PsEZfIdjWrA0O2zPELYqfdi8fHJQx",
)
FITTRACKEE_TOKEN_PATH = "/app/.fittrackee.tokens.json"
USER_TZ = os.environ.get("FITTRACKEE_USER_TZ", "Europe/Paris")
API_KEY = os.environ.get("INGEST_API_KEY", "")
DEFAULT_PORT = int(os.environ.get("HEALTH_WEBHOOK_PORT", "8090"))

SPORT_LABELS = {
    1: "Cycling (Sport)", 2: "Cycling (Transport)", 3: "Hiking",
    4: "Mountain Biking", 5: "Running", 6: "Walking",
    7: "Mountain Biking (Electric)", 8: "Trail", 9: "Skiing (Alpine)",
    10: "Skiing (Cross Country)", 11: "Rowing", 12: "Snowshoes",
    13: "Cycling (Virtual)", 14: "Mountaineering", 15: "Paragliding",
    16: "Open Water Swimming", 17: "Cycling (Trekking)", 18: "Swimrun",
    19: "Kayaking", 20: "Canoeing", 21: "Halfbike", 22: "Windsurfing",
    23: "Standup Paddleboarding", 24: "Tennis (Outdoor)",
    25: "Padel (Outdoor)", 26: "Canoeing (Whitewater)",
    27: "Kayaking (Whitewater)", 28: "Ice Skating", 29: "Inline Skating",
}

APPLE_SPORT_MAP = {
    "cycling": 1,
    "cyclingmtb": 4,
    "cyclingelectric": 7,
    "handcycling": 1,
    "hiking": 3,
    "running": 5,
    "trailrunning": 8,
    "walking": 6,
    "stairclimbing": 6,
    "elliptical": 6,
    "swimming": 16,
    "poolswimming": 16,
    "openwaterswimming": 16,
    "swimrun": 18,
    "rowing": 11,
    "kayaking": 19,
    "canoeing": 20,
    "standuppaddleboarding": 23,
    "windsurfing": 22,
    "tennis": 24,
    "padel": 25,
    "iceskating": 28,
    "inlineskating": 29,
    "skiing": 9,
    "downhillskiing": 9,
    "crosscountryskiing": 10,
    "snowboarding": 9,
    "snowshoeing": 12,
    "mountaineering": 14,
    "paragliding": 15,
    "trail": 8,
}


def normalize_type(t):
    return re.sub(r"[^a-z0-9]", "", str(t).lower())


def map_sport(apple_type):
    return APPLE_SPORT_MAP.get(normalize_type(apple_type))


def load_env():
    env = {}
    path = os.environ.get("ENV_FILE", "/app/.env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k] = v.strip()
    return env


def load_token(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_token(path, token):
    with open(path, "w") as f:
        json.dump(token, f, indent=2)


def refresh_fittrackee_token(token):
    r = requests.post(
        f"{FT_URL}/api/oauth/token",
        data={
            "client_id": FITTRACKEE_CLIENT_ID,
            "client_secret": FITTRACKEE_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
        },
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  FitTrackee token refresh failed: {r.status_code} {r.text[:200]}")
        return None
    new_token = r.json()
    new_token.setdefault("expires_at", int(time.time()) + new_token.get("expires_in", 0))
    save_token(FITTRACKEE_TOKEN_PATH, new_token)
    return new_token


def get_valid_ft_token():
    token = load_token(FITTRACKEE_TOKEN_PATH)
    if not token:
        return None
    if token.get("expires_at", 0) and token["expires_at"] < time.time():
        token = refresh_fittrackee_token(token)
    return token


def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    s = str(value).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    m = re.match(
        r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*([+-]\d{2}):?(\d{2})?",
        s,
    )
    if m:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
        offset = int(m.group(3)) * 3600 + (int(m.group(4) or 0)) * 60
        return dt.replace(tzinfo=timezone(timedelta(seconds=offset)))
    return None


def parse_distance_km(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower().replace(",", ".")
    m = re.match(r"([-+]?\d+(?:\.\d+)?)\s*(km|k|m|meters?|kilometers?)?$", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    if unit in ("m", "meters", "meter"):
        return num / 1000.0
    return num


def parse_duration_seconds(value, start=None, end=None):
    if value is None or value == "":
        if start and end:
            return int((end - start).total_seconds())
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    parts = re.split(r"\s+", s)
    total = 0.0
    matched = False
    for part in parts:
        m = re.match(r"([-+]?\d+(?:\.\d+)?)(h|hr|hrs|hours?|m|min|mins|minutes?|s|sec|secs|seconds?)?$", part)
        if not m:
            continue
        num = float(m.group(1))
        unit = m.group(2) or ""
        matched = True
        if unit.startswith("h"):
            total += num * 3600
        elif unit.startswith("m") or unit.startswith("min"):
            total += num * 60
        else:
            total += num
    if matched:
        return int(total)
    m = re.match(r"^(\d+):(\d+)(?::(\d+))?$", s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        sec = int(m.group(3) or 0)
        return h * 3600 + mi * 60 + sec
    return None


def parse_calories(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.match(r"([-+]?\d+(?:\.\d+)?)", str(value).strip())
    return int(float(m.group(1))) if m else None


def find_existing_ft_workout(start_utc, sport_id, headers):
    local = start_utc.astimezone(ZoneInfo(USER_TZ))
    date_str = local.strftime("%Y-%m-%d")
    r = requests.get(
        f"{FT_URL}/api/workouts",
        headers=headers,
        params={"per_page": 50, "page": 1, "from": date_str, "to": date_str},
        timeout=15,
    )
    if r.status_code != 200:
        return None
    workouts = r.json().get("data", {}).get("workouts", [])
    ts = start_utc.timestamp()
    best, best_delta = None, 120
    for w in workouts:
        wd = w.get("workout_date")
        if not wd:
            continue
        try:
            w_ts = datetime.strptime(wd, "%a, %d %b %Y %H:%M:%S %Z").timestamp()
        except ValueError:
            continue
        delta = abs(w_ts - ts)
        if delta < best_delta:
            best_delta = delta
            best = w["id"]
    return best


def create_workout(item, headers):
    apple_type = item.get("type") or item.get("workout_type")
    sport_id = map_sport(apple_type) if apple_type else None
    if not sport_id:
        return {"error": f"unknown Apple workout type: {apple_type!r}"}

    start = parse_datetime(item.get("start") or item.get("start_date"))
    if not start:
        return {"error": "missing/invalid 'start'"}
    end = parse_datetime(item.get("end") or item.get("end_date"))

    distance = parse_distance_km(item.get("distance"))
    duration = parse_duration_seconds(item.get("duration"), start, end)
    calories = parse_calories(item.get("calories"))

    if distance is None:
        return {"error": "missing/invalid 'distance'"}
    if duration is None or duration <= 0:
        return {"error": "missing/invalid 'duration'"}
    if distance > 999.9:
        return {"error": "distance exceeds FT limit (999.9 km)"}

    local = start.astimezone(ZoneInfo(USER_TZ))
    workout_date = local.strftime("%Y-%m-%d %H:%M")

    existing = find_existing_ft_workout(start, sport_id, headers)
    if existing:
        return {"skipped": f"already exists (workout {existing})"}

    payload = {
        "sport_id": sport_id,
        "workout_date": workout_date,
        "duration": duration,
        "distance": distance,
    }
    if calories:
        payload["calories"] = calories
    title = item.get("title")
    if title:
        payload["title"] = str(title)[:255]

    r = requests.post(
        f"{FT_URL}/api/workouts/no_gpx",
        headers=headers,
        json=payload,
        timeout=15,
    )
    if r.status_code == 201:
        try:
            wid = r.json()["data"]["workouts"][0]["id"]
        except (KeyError, IndexError, ValueError):
            wid = None
        return {"created": wid}
    return {
        "error": f"FT returned {r.status_code}: {r.text[:200]}",
        "payload": payload,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "health-webhook/1.0"

    def _reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauthorized(self):
        self._reply(401, {"error": "invalid or missing X-API-Key"})

    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/ingest":
            self._reply(404, {"error": "not found"})
            return
        key = self.headers.get("X-API-Key", "")
        if not API_KEY or key != API_KEY:
            self._unauthorized()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "invalid JSON body"})
            return

        items = payload if isinstance(payload, list) else payload.get("workouts", [])
        if not isinstance(items, list) or not items:
            self._reply(400, {"error": "expected a JSON array of workouts"})
            return

        ft_token = get_valid_ft_token()
        if not ft_token:
            self._reply(500, {"error": "no valid FitTrackee token"})
            return
        headers = {"Authorization": f"Bearer {ft_token['access_token']}"}

        created, skipped, errors = [], [], []
        for item in items:
            result = create_workout(item, headers)
            if "created" in result:
                created.append(result["created"])
            elif "skipped" in result:
                skipped.append(result["skipped"])
            else:
                errors.append(result)
        print(
            f"[ingest] received {len(items)}: created={len(created)} "
            f"skipped={len(skipped)} errors={len(errors)}"
        )
        self._reply(200, {
            "status": "ok",
            "received": len(items),
            "created": created,
            "skipped": skipped,
            "errors": errors,
        })


def main():
    global API_KEY
    env = load_env()
    API_KEY = os.environ.get("INGEST_API_KEY") or env.get("INGEST_API_KEY", "")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    if not API_KEY:
        print("INGEST_API_KEY not set - refusing to start")
        sys.exit(1)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"health webhook listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
