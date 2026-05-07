#!/usr/bin/env python3
"""Remove duplicate workouts by comparing Strava activity times with FitTrackee workouts."""

import requests
import json
import psycopg2
import os
from datetime import datetime, timedelta

# Load tokens
FT_TOKEN = json.loads(open("/app/.fittrackee.tokens.json").read())["access_token"]
STRAVA_TOKEN = json.loads(open("/app/.strava.tokens.json").read())["access_token"]

strava_headers = {"Authorization": f"Bearer {STRAVA_TOKEN}"}
ft_headers = {"Authorization": f"Bearer {FT_TOKEN}"}

FITTRACKEE_HOST = os.environ.get("FITTRACKEE_HOST", "127.0.0.1")
FITTRACKEE_PORT = os.environ.get("FITTRACKEE_PORT", "5001")
FITTRACKEE_URL = f"http://{FITTRACKEE_HOST}:{FITTRACKEE_PORT}"

DB_HOST = os.environ.get("POSTGRES_HOST", "172.24.0.2")
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "mysecretpassword")

def get_strava_activities(days_back=90):
    """Get Strava activities from last N days."""
    after_ts = int((datetime.now() - timedelta(days=days_back)).timestamp())
    activities = []
    page = 1
    while True:
        r = requests.get("https://www.strava.com/api/v3/athlete/activities",
                         headers=strava_headers,
                         params={"per_page": 100, "page": page, "after": after_ts})
        if r.status_code != 200:
            break
        data = r.json()
        if not data:
            break
        activities.extend(data)
        page += 1
        if page > 20:
            break
    return {a['id']: a for a in activities}

def get_fittrackee_workouts():
    """Get all workouts from FitTrackee."""
    conn = psycopg2.connect(host=DB_HOST, port='5432', dbname='fittrackee', user='fittrackee', password=DB_PASSWORD)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, workout_date, title, notes, description, 
               COALESCE(ave_cadence, 0) as ave_cadence, 
               COALESCE(ave_hr, 0) as ave_hr
        FROM workouts 
        ORDER BY workout_date DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def parse_strava_time(strava_activities):
    """Parse Strava activity times."""
    strava_times = {}
    for sid, act in strava_activities.items():
        dt = datetime.fromisoformat(act['start_date'].replace('Z', '+00:00'))
        strava_times[sid] = dt.replace(tzinfo=None)
    return strava_times

print("=== Fetching Strava activities ===")
strava_activities = get_strava_activities(180)
print(f"Found {len(strava_activities)} Strava activities")

print("=== Fetching FitTrackee workouts ===")
ft_workouts = get_fittrackee_workouts()
print(f"Found {len(ft_workouts)} FitTrackee workouts")

strava_times = parse_strava_time(strava_activities)

# Group FitTrackee workouts by approximate time (within 2 minutes)
from collections import defaultdict
time_groups = defaultdict(list)

for w in ft_workouts:
    w_id, w_date, title, notes, desc, ave_cad, ave_hr = w
    # Round to nearest minute
    w_date = w_date.replace(second=0, microsecond=0)
    time_key = w_date.strftime("%Y-%m-%d %H:%M")
    time_groups[time_key].append(w)

# Find duplicates (groups with more than 1 workout)
duplicates = {k: v for k, v in time_groups.items() if len(v) > 1}

print(f"\n=== Found {len(duplicates)} time groups with duplicates ===")

# For each duplicate group, keep the best one (merged has cadence)
# Delete the rest
deleted_count = 0
for time_key, workouts in sorted(duplicates.items()):
    # Sort by: has cadence > has notes > older id
    scored = []
    for w in workouts:
        w_id, w_date, title, notes, desc, ave_cad, ave_hr = w
        score = 0
        if ave_cad and ave_cad > 0:
            score += 100  # Has cadence = likely merged
        if notes and 'strava.com/activities/' in notes:
            score += 10  # Has Strava link
        scored.append((score, w_id, w))
    
    scored.sort(reverse=True)
    
    # Keep the best one, delete others
    keep = scored[0]
    delete = scored[1:]
    
    print(f"\n{time_key}: {len(workouts)} workouts")
    print(f"  KEEP: ID={keep[1]}, title={keep[2][2]}, cadence={keep[2][5]}, notes={keep[2][3]}")
    for s in delete:
        print(f"  DELETE: ID={s[1]}, title={s[2][2]}, cadence={s[2][5]}, notes={s[2][3]}")
    
    # Delete from DB
    for _, w_id, _ in delete:
        conn = psycopg2.connect(host=DB_HOST, port='5432', dbname='fittrackee', user='fittrackee', password=DB_PASSWORD)
        cur = conn.cursor()
        cur.execute("DELETE FROM records WHERE workout_id = %s", (w_id,))
        cur.execute("DELETE FROM workout_segments WHERE workout_id = %s", (w_id,))
        cur.execute("DELETE FROM workouts WHERE id = %s", (w_id,))
        conn.commit()
        cur.close()
        conn.close()
        deleted_count += 1

print(f"\n=== Deleted {deleted_count} duplicate workouts ===")