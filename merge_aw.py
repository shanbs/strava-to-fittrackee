#!/usr/bin/env python3
"""Merge workouts - prefer Apple Watch GPS/speed, use other for cadence."""

import requests
import json
import time
import os
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

# Target date
TARGET_DATE = "2026-03-10"

def get_strava_activities():
    activities = []
    page = 1
    while True:
        r = requests.get("https://www.strava.com/api/v3/athlete/activities", headers=strava_headers, params={"per_page": 100, "page": page})
        if r.status_code != 200: return []
        data = r.json()
        if not data: break
        activities.extend(data)
        page += 1
    return activities

def get_streams(activity_id):
    r = requests.get(f"https://www.strava.com/api/v3/activities/{activity_id}/streams", 
                      headers=strava_headers, 
                      params={"keys": "latlng,time,heartrate,cadence,velocity_smooth", "key_by_type": "true"})
    if r.status_code != 200: return None
    return r.json()

def merge_gpx(streams_aw, streams_other, base_time):
    """Merge: Apple Watch GPS/speed + other device cadence + HR from both if available."""
    
    # Apple Watch has better GPS - use it as base
    base_streams = streams_aw
    other_streams = streams_other
    
    latlng = base_streams.get('latlng', {}).get('data', [])
    times = base_streams.get('time', {}).get('data', [])
    vels = base_streams.get('velocity_smooth', {}).get('data', [])
    
    # Get HR from both - prefer Apple Watch
    hrs_aw = streams_aw.get('heartrate', {}).get('data', [])
    hrs_other = streams_other.get('heartrate', {}).get('data', [])
    hrs = hrs_aw if hrs_aw else hrs_other
    
    # Get cadence from other device (XOSS/CYCPLUS has cadence, AW may not)
    cads = other_streams.get('cadence', {}).get('data', []) if other_streams else []
    
    if not latlng: return None
    
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', 
             '<gpx version="1.1" creator="merge" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
             f'  <trk><name>Merged Ride (AW GPS + XOSS Cadence)</name><trkseg>']
    
    for i in range(len(latlng)):
        lat, lon = latlng[i]
        t = base_time + timedelta(seconds=times[i])
        
        # HR from AW or other
        hr = None
        if i < len(hrs) and hrs[i]:
            hr = int(hrs[i])
        
        # Cadence from XOSS/CYCPLUS
        cad = None
        if i < len(cads) and cads[i]:
            cad = int(cads[i])
        
        # Speed from Apple Watch
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

# Main - get activities for target date
activities = get_strava_activities()
cycling = [a for a in activities if a.get('type') in ['Ride', 'VirtualRide'] and a['start_date'][:10] == TARGET_DATE]

# Group by start time (within 5 minutes = same ride)
pairs = []
used = set()
for i, act1 in enumerate(cycling):
    if act1['id'] in used:
        continue
    for j, act2 in enumerate(cycling):
        if i == j or act2['id'] in used:
            continue
        # Check if within 5 minutes
        t1 = datetime.fromisoformat(act1['start_date'].replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(act2['start_date'].replace('Z', '+00:00'))
        if abs((t1 - t2).total_seconds()) < 300:
            pairs.append((act1, act2))
            used.add(act1['id'])
            used.add(act2['id'])
            break

print(f"Found {len(pairs)} pairs to merge")

for act1, act2 in pairs:
    # Determine which is Apple Watch (has HR but no cadence)
    streams1 = get_streams(act1['id'])
    streams2 = get_streams(act2['id'])
    
    has_hr1 = bool(streams1.get('heartrate', {}).get('data', []))
    has_cad1 = bool(streams1.get('cadence', {}).get('data', []))
    has_hr2 = bool(streams2.get('heartrate', {}).get('data', []))
    has_cad2 = bool(streams2.get('cadence', {}).get('data', []))
    
    print(f"  {act1['id']}: HR={has_hr1}, Cad={has_cad1}")
    print(f"  {act2['id']}: HR={has_hr2}, Cad={has_cad2}")
    
    # Apple Watch: has HR, no cadence
    # XOSS/CYCPLUS: may have cadence, no HR
    if has_hr1 and not has_cad1:
        aw, other = streams1, streams2
        aw_act, other_act = act1, act2
    elif has_hr2 and not has_cad2:
        aw, other = streams2, streams1
        aw_act, other_act = act2, act1
    else:
        # Can't determine, use first as base
        aw, other = streams1, streams2
        aw_act = act1
    
    base_time = datetime.fromisoformat(aw_act['start_date'].replace('Z', '+00:00')).replace(tzinfo=None)
    
    gpx = merge_gpx(aw, other, base_time)
    if gpx and upload_gpx(gpx):
        print(f"  Merged and uploaded!")
    else:
        print(f"  Failed")
    
    time.sleep(0.5)

print("Done!")