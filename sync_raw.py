#!/usr/bin/env python3
"""Sync WITHOUT GPS filter - use raw Strava data."""

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

TARGET_IDS = [17675801433, 17671394514]

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

for act_id in TARGET_IDS:
    r = requests.get(f"https://www.strava.com/api/v3/activities/{act_id}", headers=strava_headers)
    act = r.json()
    print(f"Syncing {act_id}: {act.get('name')}")
    
    streams = get_streams(act_id)
    if not streams or not streams.get('latlng'):
        print(f"  No GPS")
        continue
    
    gpx = create_gpx(streams, act['start_date'], act.get('name', 'Ride'))
    if upload_gpx(gpx):
        print(f"  Done!")
    time.sleep(0.5)

print("Done!")