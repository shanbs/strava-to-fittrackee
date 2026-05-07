#!/usr/bin/env python3
"""Sync from Strava - accepts date range."""

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

# Parse command line args: --from YYYY-MM-DD --to YYYY-MM-DD
FROM_DATE = None
TO_DATE = None
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == '--from' and i+1 < len(args):
        FROM_DATE = args[i+1]
    if arg == '--to' and i+1 < len(args):
        TO_DATE = args[i+1]

if FROM_DATE:
    print(f"Syncing from {FROM_DATE} to {TO_DATE or 'now'}")
else:
    print("No date range specified, doing incremental sync...")

def get_strava_activities(from_date=None, to_date=None):
    """Get Strava activities within date range."""
    activities = []
    page = 1
    
    # Build date filter
    after_ts = None
    before_ts = None
    
    if from_date:
        after_ts = int(datetime.strptime(from_date, "%Y-%m-%d").timestamp())
    if to_date:
        before_ts = int((datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)).timestamp())
    
    while True:
        params = {"per_page": 100, "page": page}
        if after_ts:
            params['after'] = after_ts
        if before_ts:
            params['before'] = before_ts
        
        r = requests.get("https://www.strava.com/api/v3/athlete/activities", 
                         headers=strava_headers, params=params)
        if r.status_code != 200:
            print(f"Error fetching Strava activities: {r.status_code}")
            break
        data = r.json()
        if not data:
            break
        activities.extend(data)
        page += 1
        if page > 20:  # Safety limit
            break
    
    return activities

def get_streams(activity_id):
    r = requests.get(f"https://www.strava.com/api/v3/activities/{activity_id}/streams", 
                      headers=strava_headers, 
                      params={"keys": "latlng,time,heartrate,cadence,velocity_smooth", "key_by_type": "true"})
    if r.status_code != 200: return None
    return r.json()

def create_gpx(streams, activity_date, activity_name):
    latlng = streams.get('latlng', {}).get('data', [])
    times = streams.get('time', {}).get('data', [])
    hrs = streams.get('heartrate', {}).get('data', [])
    cads = streams.get('cadence', {}).get('data', [])
    vels = streams.get('velocity_smooth', {}).get('data', [])
    
    if not latlng: return None
    
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', 
             '<gpx version="1.1" creator="sync" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
             f'  <trk><name>{activity_name}</name><trkseg>']
    
    dt = datetime.fromisoformat(activity_date.replace('Z', '+00:00'))
    base = dt.replace(tzinfo=None)
    
    for i in range(len(latlng)):
        lat, lon = latlng[i]
        t = base + timedelta(seconds=times[i])
        
        hr = hrs[i] if i < len(hrs) and hrs[i] else None
        cad = cads[i] if i < len(cads) and cads[i] else None
        vel = vels[i] if i < len(vels) and vels[i] is not None else None
        
        speed_attr = f' speed="{vel:.2f}"' if vel and vel > 0 and vel < 50 else ''
        
        lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"{speed_attr}><time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>')
        if hr or cad:
            lines.append('      <extensions><gpxtpx:TrackPointExtension>')
            if hr: lines.append(f'        <gpxtpx:hr>{int(hr)}</gpxtpx:hr>')
            if cad: lines.append(f'        <gpxtpx:cad>{int(cad)}</gpxtpx:cad>')
            lines.append('        </gpxtpx:TrackPointExtension></extensions>')
        lines.append('    </trkpt>')
    
    lines.append('  </trkseg></trk></gpx>')
    return '\n'.join(lines)

def upload_gpx(gpx_content, sport_id=1):
    files = {'file': ('activity.gpx', gpx_content, 'application/gpx+xml')}
    data = {'data': json.dumps({"sport_id": sport_id})}
    r = requests.post(f'{FITTRACKEE_URL}/api/workouts', headers=ft_headers, files=files, data=data)
    return r.status_code == 201

# Main
print("=== Fetching Strava activities ===")
activities = get_strava_activities(FROM_DATE, TO_DATE)
print(f"Found {len(activities)} activities")

# Get existing strava IDs from FitTrackee to avoid duplicates
existing_ids = set()
page = 1
while True:
    r = requests.get(f'{FITTRACKEE_URL}/api/workouts?per_page=100&page={page}', headers=ft_headers)
    if r.status_code != 200:
        break
    ws = r.json()['data']['workouts']
    if not ws: break
    for w in ws:
        notes = w.get('notes', '')
        if 'strava.com/activities/' in notes:
            sid = notes.split('strava.com/activities/')[1].split()[0]
            existing_ids.add(int(sid))
    page += 1
    if page > 10: break

print(f"Existing workouts: {len(existing_ids)}")

# Only sync cycling activities
cycling = [a for a in activities if a.get('type') in ['Ride', 'VirtualRide']]
print(f"Cycling activities: {len(cycling)}")

synced = 0
for act in cycling:
    if act['id'] in existing_ids:
        continue
    
    streams = get_streams(act['id'])
    if not streams or not streams.get('latlng'):
        continue
    
    gpx = create_gpx(streams, act['start_date'], act.get('name', 'Ride'))
    if not gpx:
        continue
    
    if upload_gpx(gpx):
        synced += 1
        print(f"  Synced: {act['id']} - {act.get('name')[:40]}")
    
    time.sleep(0.3)

print(f"=== Done! Synced {synced} new workouts ===")