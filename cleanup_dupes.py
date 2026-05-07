#!/usr/bin/env python3
"""Clean up duplicate workouts - remove originals that have been merged."""

import requests
import json
import os

STRAVA_TOKEN = open("/app/.strava.tokens.json").read()
STRAVA_TOKEN = json.loads(STRAVA_TOKEN)["access_token"]
FT_TOKEN = open("/app/.fittrackee.tokens.json").read()
FT_TOKEN = json.loads(FT_TOKEN)["access_token"]

ft_headers = {"Authorization": f"Bearer {FT_TOKEN}"}

FITTRACKEE_HOST = os.environ.get("FITTRACKEE_HOST", "fittrackee_app")
FITTRACKEE_PORT = os.environ.get("FITTRACKEE_PORT", "5001")
FITTRACKEE_URL = f"http://{FITTRACKEE_HOST}:{FITTRACKEE_PORT}"

print("=== Fetching all workouts ===")
all_workouts = []
page = 1
while page <= 20:
    r = requests.get(f'{FITTRACKEE_URL}/api/workouts?per_page=100&page={page}', headers=ft_headers)
    if r.status_code != 200: break
    ws = r.json()['data']['workouts']
    if not ws: break
    all_workouts.extend(ws)
    page += 1

print(f"Found {len(all_workouts)} workouts")

# Group by date
from collections import defaultdict
by_date = defaultdict(list)
for w in all_workouts:
    date = w.get('workout_date', '')[:10]
    by_date[date].append(w)

# For each date, find workouts that have "Merged" and their corresponding originals
merged_dates = set()
originals_to_delete = []

for date, workouts in by_date.items():
    merged = [w for w in workouts if 'Merged' in w.get('title', '')]
    if not merged:
        continue
    
    merged_dates.add(date)
    
    # Get Strava IDs from merged workouts
    merged_ids = set()
    for m in merged:
        notes = m.get('notes', '')
        # Note: merged workouts don't have Strava ID in notes
        pass
    
    # For non-merged workouts on same date with same time (within 5 min), delete
    for w in workouts:
        if 'Merged' in w.get('title', ''):
            continue
        time_str = w.get('workout_date', '')
        w_time = time_str[11:16]  # HH:MM
        
        # Check if this is likely an original that was merged
        # Originals don't have "Ride" in title properly, or have specific names
        # We'll delete all non-merged that are within 10 min of a merged workout
        for m in merged:
            m_time = m.get('workout_date', '')[11:16]
            if abs(int(w_time.replace(':','')) - int(m_time.replace(':',''))) <= 10:
                originals_to_delete.append(w['id'])
                break

print(f"Dates with merges: {merged_dates}")
print(f"Originals to delete: {len(originals_to_delete)}")

# Delete originals
for workout_id in originals_to_delete:
    r = requests.delete(f'{FITTRACKEE_URL}/api/workouts/{workout_id}', headers=ft_headers)
    if r.status_code in [200, 204]:
        print(f"  Deleted: {workout_id}")
    else:
        print(f"  Failed to delete {workout_id}: {r.status_code}")

print(f"=== Done! Deleted {len(originals_to_delete)} original workouts ===")