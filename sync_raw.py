#!/usr/bin/env python3
"""Incremental sync: fetch new Strava activities after last FT workout, upload GPX."""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import requests

FT_HOST = os.environ.get("FITTRACKEE_HOST", "fittrackee")
FT_PORT = os.environ.get("FITTRACKEE_PORT", "5000")
FT_URL = f"http://{FT_HOST}:{FT_PORT}"

STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "154332")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "febeecd8bc55f11afac5c477e4008ff35d05e3b3")
FITTRACKEE_CLIENT_ID = os.environ.get("FITTRACKEE_CLIENT_ID", "hxyg6fVt71WzAbKO6M6OwiEB")
FITTRACKEE_CLIENT_SECRET = os.environ.get("FITTRACKEE_CLIENT_SECRET", "PVbysRv51uaPot9XQB2PsEZfIdjWrA0O2zPELYqfdi8fHJQx")

STRAVA_TOKEN_PATH = "/app/.strava.tokens.json"
FITTRACKEE_TOKEN_PATH = "/app/.fittrackee.tokens.json"


def load_token(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_token(path, token):
    with open(path, "w") as f:
        json.dump(token, f, indent=2)


def refresh_strava_token(token):
    r = requests.post("https://www.strava.com/api/v3/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
    })
    if r.status_code != 200:
        print(f"  Strava token refresh failed: {r.status_code}")
        return None
    new_token = r.json()
    save_token(STRAVA_TOKEN_PATH, new_token)
    return new_token


def refresh_fittrackee_token(token):
    r = requests.post(f"{FT_URL}/api/oauth/token", data={
        "client_id": FITTRACKEE_CLIENT_ID,
        "client_secret": FITTRACKEE_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
    })
    if r.status_code != 200:
        print(f"  FitTrackee token refresh failed: {r.status_code}")
        return None
    new_token = r.json()
    save_token(FITTRACKEE_TOKEN_PATH, new_token)
    return new_token


def get_valid_token(path, client_id, client_secret, refresh_fn):
    token = load_token(path)
    if not token:
        return None
    expires_at = token.get("expires_at", 0)
    if expires_at and expires_at < time.time():
        token = refresh_fn(token)
    return token


SPORT_ID_MAP = {
    "Ride": 1, "VirtualRide": 1, "MountainBikeRide": 1, "EMountainBikeRide": 1,
    "Run": 2, "TrailRun": 2, "Hike": 3, "Walk": 4,
    "Swim": 5, "Workout": 6, "WeightTraining": 6,
}


def get_sport_id(activity_type):
    return SPORT_ID_MAP.get(activity_type, 1)


def get_streams(activity_id, headers):
    r = requests.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}/streams",
        headers=headers,
        params={"keys": "latlng,time,heartrate,cadence,velocity_smooth", "key_by_type": "true"},
    )
    if r.status_code != 200:
        return None
    return r.json()


def create_gpx(streams, activity_date, activity_name):
    latlng = streams.get("latlng", {}).get("data", [])
    times = streams.get("time", {}).get("data", [])
    hrs = streams.get("heartrate", {}).get("data", [])
    cads = streams.get("cadence", {}).get("data", [])
    vels = streams.get("velocity_smooth", {}).get("data", [])

    if not latlng:
        return None

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="sync" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        f'  <trk><name>{activity_name}</name><trkseg>',
    ]

    dt = datetime.fromisoformat(activity_date.replace("Z", "+00:00"))
    base = dt.replace(tzinfo=None)

    for i in range(len(latlng)):
        lat, lon = latlng[i]
        t = base + timedelta(seconds=times[i])

        hr = hrs[i] if i < len(hrs) and hrs[i] else None
        cad = cads[i] if i < len(cads) and cads[i] else None
        vel = vels[i] if i < len(vels) and vels[i] is not None else None

        speed_attr = f' speed="{vel:.2f}"' if vel and vel > 0 and vel < 50 else ""

        lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"{speed_attr}><time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>')
        if hr or cad:
            lines.append("      <extensions><gpxtpx:TrackPointExtension>")
            if hr:
                lines.append(f"        <gpxtpx:hr>{int(hr)}</gpxtpx:hr>")
            if cad:
                lines.append(f"        <gpxtpx:cad>{int(cad)}</gpxtpx:cad>")
            lines.append("        </gpxtpx:TrackPointExtension></extensions>")
        lines.append("    </trkpt>")

    lines.append("  </trkseg></trk></gpx>")
    return "\n".join(lines)


def upload_gpx(gpx_content, sport_id, headers):
    files = {"file": ("activity.gpx", gpx_content, "application/gpx+xml")}
    data = {"data": json.dumps({"sport_id": sport_id})}
    r = requests.post(f"{FT_URL}/api/workouts", headers=headers, files=files, data=data)
    return r.status_code == 201


def get_latest_workout_date(headers):
    r = requests.get(f"{FT_URL}/api/workouts?per_page=1&order=desc", headers=headers)
    if r.status_code != 200:
        return None
    data = r.json().get("data", {}).get("workouts", [])
    if not data:
        return None
    return data[0]["workout_date"]


def fetch_strava_activities(after_ts, headers):
    activities = []
    page = 1
    while True:
        r = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers=headers,
            params={"per_page": 100, "page": page, "after": int(after_ts)},
        )
        if r.status_code != 200:
            print(f"  Strava API error: {r.status_code}")
            return activities
        data = r.json()
        if not data:
            break
        activities.extend(data)
        page += 1
    return activities


def main():
    strava_token = get_valid_token(STRAVA_TOKEN_PATH, STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, refresh_strava_token)
    if not strava_token:
        print("No valid Strava token")
        return
    strava_headers = {"Authorization": f"Bearer {strava_token['access_token']}"}

    ft_token = get_valid_token(FITTRACKEE_TOKEN_PATH, FITTRACKEE_CLIENT_ID, FITTRACKEE_CLIENT_SECRET, refresh_fittrackee_token)
    if not ft_token:
        print("No valid FitTrackee token")
        return
    ft_headers = {"Authorization": f"Bearer {ft_token['access_token']}"}

    last_date = get_latest_workout_date(ft_headers)
    if last_date:
        last_dt = parsedate_to_datetime(last_date)
        after_ts = last_dt.timestamp()
        print(f"Last workout: {last_date}")
    else:
        after_ts = 0
        print("No existing workouts, syncing all")

    activities = fetch_strava_activities(after_ts, strava_headers)
    print(f"Found {len(activities)} new activities")

    for act in activities:
        act_id = act["id"]
        act_type = act.get("type", "")
        sport_id = get_sport_id(act_type)
        name = act.get("name", "").replace('"', "")
        print(f"  Syncing {act_id}: {name} ({act_type})")

        streams = get_streams(act_id, strava_headers)
        if not streams or not streams.get("latlng"):
            print("    No GPS, skipping")
            continue

        gpx = create_gpx(streams, act["start_date"], name)
        if not gpx:
            print("    GPX generation failed")
            continue

        if upload_gpx(gpx, sport_id, ft_headers):
            print("    Uploaded")
        else:
            print("    Upload failed")
        time.sleep(0.5)

    print("Done!")


main()
