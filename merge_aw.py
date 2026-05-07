#!/usr/bin/env python3
"""Merge workouts - accepts date range, default last 90 days."""

import requests
import json
import time
import os
import sys
from datetime import datetime, timedelta

STRAVA_TOKEN = open("/app/.strava.tokens.json").read()
STRAVA_TOKEN = json.loads(STRAVA_TOKEN)["access_token"]
FT_TOKEN = open("/app/.fittrackee.tokens.json").read()
FT_TOKEN = json.loads(FT_TOKEN)["access_token"]

strava_headers = {"Authorization": f"Bearer {STRAVA_TOKEN}"}
ft_headers = {"Authorization": f"Bearer {FT_TOKEN}"}

FITTRACKEE_HOST = os.environ.get("FITTRACKEE_HOST", "fittrackee_app")
FITTRACKEE_PORT = os.environ.get("FITTRACKEE_PORT", "5001")
FITTRACKEE_URL = f"http://{FITTRACKEE_HOST}:{FITTRACKEE_PORT}"

MERGE_WINDOW_MINUTES = int(os.environ.get("MERGE_WINDOW_MINUTES", "5"))

# Parse command line args: --from YYYY-MM-DD --to YYYY-MM-DD
FROM_DATE = None
TO_DATE = None
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == '--from' and i+1 < len(args):
        FROM_DATE = args[i+1]
    if arg == '--to' and i+1 < len(args):
        TO_DATE = args[i+1]

# Default to last 90 days if not specified
if not FROM_DATE:
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=90)
    FROM_DATE = from_dt.strftime("%Y-%m-%d")
    TO_DATE = to_dt.strftime("%Y-%m-%d")
    print(f"Merging last 90 days: {FROM_DATE} to {TO_DATE}")
elif TO_DATE:
    print(f"Merging: {FROM_DATE} to {TO_DATE}")
else:
    print(f"Merging from: {FROM_DATE}")

def get_strava_activities_by_date_range(from_date, to_date):
    """Get Strava activities within date range."""
    start_dt = datetime.strptime(from_date, "%Y-%m-%d")
    if to_date:
        end_dt = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)
    else:
        end_dt = datetime.now()
    
    activities = []
    page = 1
    while True:
        r = requests.get("https://www.strava.com/api/v3/athlete/activities", 
                         headers=strava_headers, 
                         params={"per_page": 100, "page": page,
                                "after": int(start_dt.timestamp()),
                                "before": int(end_dt.timestamp())})
        if r.status_code != 200: return []
        data = r.json()
        if not data: break
        activities.extend(data)
        page += 1
    return [a for a in activities if a.get('type') in ['Ride', 'VirtualRide']]

def get_streams(activity_id):
    r = requests.get(f"https://www.strava.com/api/v3/activities/{activity_id}/streams", 
                      headers=strava_headers, 
                      params={"keys": "latlng,time,heartrate,cadence,velocity_smooth", "key_by_type": "true"})
    if r.status_code != 200: return None
    return r.json()

def merge_gpx(streams_aw, streams_other, base_time):
    """Merge: Apple Watch GPS/speed + other device cadence + HR from both if available."""
    
    latlng = streams_aw.get('latlng', {}).get('data', [])
    times = streams_aw.get('time', {}).get('data', [])
    vels = streams_aw.get('velocity_smooth', {}).get('data', [])
    
    hrs_aw = streams_aw.get('heartrate', {}).get('data', [])
    hrs_other = streams_other.get('heartrate', {}).get('data', []) if streams_other else []
    hrs = hrs_aw if hrs_aw else hrs_other
    
    cads = streams_other.get('cadence', {}).get('data', []) if streams_other else []
    
    if not latlng: return None
    
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', 
             '<gpx version="1.1" creator="merge" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
             f'  <trk><name>Merged Ride (AW GPS + XOSS Cadence)</name><trkseg>']
    
    for i in range(len(latlng)):
        lat, lon = latlng[i]
        t = base_time + timedelta(seconds=times[i])
        
        hr = None
        if i < len(hrs) and hrs[i]:
            hr = int(hrs[i])
        
        cad = None
        if i < len(cads) and cads[i]:
            cad = int(cads[i])
        
        vel = vels[i] if i < len(vels) and vels[i] is not None else None
        speed_attr = f' speed="{vel:.2f}"' if vel and vel > 0 and vel < 50 else ''
        
        lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"{speed_attr}><time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>')
        if hr or cad:
            lines.append('      <extensions><gpxtpx:TrackPointExtension>')
            if hr: lines.append(f'        <gpxtpx:hr>{hr}</gpxtpx:hr>')
            if cad: lines.append(f'        <gpxtpx:cad>{cad}</gpxtpx:cad>')
            lines.append('        </gpxtpx:TrackPointExtension></extensions>')
        lines.append('    </trkpt>')
    
    lines.append('  </trkseg></trk></gpx>')
    return '\n'.join(lines)

def upload_gpx(gpx_content, sport_id=1):
    files = {'file': ('merged.gpx', gpx_content, 'application/gpx+xml')}
    data = {'data': json.dumps({"sport_id": sport_id})}
    r = requests.post(f'{FITTRACKEE_URL}/api/workouts', headers=ft_headers, files=files, data=data)
    return r.status_code == 201

def delete_workout(strava_id):
    """Delete a workout from FitTrackee by Strava ID."""
    print(f"    Trying to delete strava_id {strava_id}...")
    # Find workout by Strava ID in notes
    r = requests.get(f'{FITTRACKEE_URL}/api/workouts?per_page=100', headers=ft_headers)
    if r.status_code != 200:
        print(f"    Failed to get workouts: {r.status_code}")
        return False
    workouts = r.json()['data']['workouts']
    print(f"    Checking {len(workouts)} workouts...")
    for w in workouts:
        notes = w.get('notes', '')
        if f'strava.com/activities/{strava_id}' in notes:
            print(f"    Found workout {w['id']}, deleting...")
            del_r = requests.delete(f'{FITTRACKEE_URL}/api/workouts/{w["id"]}', headers=ft_headers)
            print(f"    Delete status: {del_r.status_code}")
            return del_r.status_code == 200 or del_r.status_code == 204
    print(f"    Not found!")
    return False

# Main - get activities for date range
print("=== Fetching Strava activities for merge ===")
activities = get_strava_activities_by_date_range(FROM_DATE, TO_DATE)
print(f"Found {len(activities)} cycling activities in range")

# Group by start time (within MERGE_WINDOW_MINUTES = same ride)
pairs = []
used = set()
for i, act1 in enumerate(activities):
    if act1['id'] in used:
        continue
    for j, act2 in enumerate(activities):
        if i == j or act2['id'] in used:
            continue
        t1 = datetime.fromisoformat(act1['start_date'].replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(act2['start_date'].replace('Z', '+00:00'))
        if abs((t1 - t2).total_seconds()) < MERGE_WINDOW_MINUTES * 60:
            pairs.append((act1, act2))
            used.add(act1['id'])
            used.add(act2['id'])
            break

print(f"Found {len(pairs)} pairs to merge")

merged_count = 0
for act1, act2 in pairs:
    streams1 = get_streams(act1['id'])
    streams2 = get_streams(act2['id'])
    
    if not streams1 or not streams2:
        continue
    
    has_hr1 = bool(streams1.get('heartrate', {}).get('data', []))
    has_cad1 = bool(streams1.get('cadence', {}).get('data', []))
    has_hr2 = bool(streams2.get('heartrate', {}).get('data', []))
    has_cad2 = bool(streams2.get('cadence', {}).get('data', []))
    
    # Apple Watch: has HR, no cadence; XOSS/CYCPLUS: has cadence
    if has_hr1 and not has_cad1:
        aw, other = streams1, streams2
        aw_act = act1
    elif has_hr2 and not has_cad2:
        aw, other = streams2, streams1
        aw_act = act2
    else:
        aw, other = streams1, streams2
        aw_act = act1
    
    base_time = datetime.fromisoformat(aw_act['start_date'].replace('Z', '+00:00')).replace(tzinfo=None)
    
    gpx = merge_gpx(aw, other, base_time)
    if gpx and upload_gpx(gpx):
        merged_count += 1
        print(f"  Merged: {act1['id']} + {act2['id']}")
        
        # Delete original workouts after successful merge
        delete_workout(act1['id'])
        delete_workout(act2['id'])
        print(f"    Deleted originals: {act1['id']}, {act2['id']}")
    
    time.sleep(0.5)

print(f"=== Done! Merged {merged_count} workouts ===")