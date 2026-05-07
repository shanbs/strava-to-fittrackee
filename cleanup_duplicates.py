#!/usr/bin/env python3
"""One-time cleanup: keep 1 merged workout per ride, delete originals and excess merged copies."""

import os
import json
import time
from datetime import datetime
import requests

FT_HOST = os.environ.get("FITTRACKEE_HOST", "127.0.0.1")
FT_PORT = os.environ.get("FITTRACKEE_PORT", "5001")
FT_URL = f"http://{FT_HOST}:{FT_PORT}"
FITTRACKEE_TOKEN_PATH = "/app/.fittrackee.tokens.json"

def load_token(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

token = load_token(FITTRACKEE_TOKEN_PATH)
if not token:
    print("No token found")
    exit(1)

headers = {"Authorization": f"Bearer {token['access_token']}"}

all_workouts = []
page = 1
while True:
    r = requests.get(f"{FT_URL}/api/workouts", headers=headers, params={"page": page, "limit": 50})
    if r.status_code != 200:
        print(f"API error: {r.status_code}")
        exit(1)
    data = r.json()
    ws = data["data"]["workouts"]
    if not ws:
        break
    all_workouts.extend(ws)
    page += 1

print(f"Total FT workouts: {len(all_workouts)}")

from datetime import timezone
cutoff = datetime(2026, 4, 30, tzinfo=timezone.utc)
recent = []
for w in all_workouts:
    wd = w["workout_date"]
    wdt = datetime.strptime(wd, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    if wdt >= cutoff:
        w["_dt"] = wdt
        recent.append(w)

print(f"Recent workouts (since Apr 30): {len(recent)}")

from collections import defaultdict
groups = defaultdict(list)
for w in recent:
    key = w["_dt"].strftime("%Y-%m-%d %H:%M")
    groups[key].append(w)

to_delete = []
for key, ws in sorted(groups.items()):
    merged = [w for w in ws if "Merged" in w.get("title", "")]
    individual = [w for w in ws if "Merged" not in w.get("title", "")]
    if merged:
        keep = merged[0]
        excess_merged = merged[1:]
        to_delete.extend(excess_merged)
        to_delete.extend(individual)
        print(f"  {key}: keep {keep['id']} ({keep['title'][:40]}), delete {len(excess_merged)} excess merged + {len(individual)} originals")

print(f"\nTotal to delete: {len(to_delete)}")
if not to_delete:
    print("Nothing to clean up!")
    exit(0)

for w in to_delete:
    rid = w["id"]
    title = w.get("title", "")[:50]
    r = requests.delete(f"{FT_URL}/api/workouts/{rid}", headers=headers)
    if r.status_code == 204:
        print(f"  Deleted {rid}: {title}")
    elif r.status_code == 404:
        print(f"  {rid}: already gone")
    else:
        print(f"  {rid}: delete failed ({r.status_code})")
    time.sleep(0.2)

print("Done!")
