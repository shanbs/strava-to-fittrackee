#!/usr/bin/env python3
"""Find cycling activity pairs (same ride, two devices), merge GPS+HR+cadence, upload."""

import os
import json
import time
from datetime import datetime, timedelta, timezone
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
LAST_MERGE_FILE = "/app/.last_merge"


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


def get_streams(activity_id, headers):
    r = requests.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}/streams",
        headers=headers,
        params={"keys": "latlng,time,heartrate,cadence,velocity_smooth", "key_by_type": "true"},
    )
    if r.status_code != 200:
        return None
    return r.json()


def merge_gpx(streams_aw, streams_other, base_time):
    latlng = streams_aw.get("latlng", {}).get("data", [])
    times = streams_aw.get("time", {}).get("data", [])
    vels = streams_aw.get("velocity_smooth", {}).get("data", [])

    hrs_aw = streams_aw.get("heartrate", {}).get("data", [])
    hrs_other = streams_other.get("heartrate", {}).get("data", [])
    hrs = hrs_aw if hrs_aw else hrs_other
    cads = streams_other.get("cadence", {}).get("data", []) if streams_other else []

    if not latlng:
        return None

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="merge" xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
        "  <trk><name>Merged Ride (AW GPS + XOSS Cadence)</name><trkseg>",
    ]

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
        speed_attr = f' speed="{vel:.2f}"' if vel and vel > 0 and vel < 50 else ""

        lines.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"{speed_attr}><time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>')
        if hr or cad:
            lines.append("      <extensions><gpxtpx:TrackPointExtension>")
            if hr:
                lines.append(f"        <gpxtpx:hr>{hr}</gpxtpx:hr>")
            if cad:
                lines.append(f"        <gpxtpx:cad>{cad}</gpxtpx:cad>")
            lines.append("        </gpxtpx:TrackPointExtension></extensions>")
        lines.append("    </trkpt>")

    lines.append("  </trkseg></trk></gpx>")
    return "\n".join(lines)


def upload_gpx(gpx_content, headers):
    files = {"file": ("merged.gpx", gpx_content, "application/gpx+xml")}
    data = {"data": json.dumps({"sport_id": 1})}
    r = requests.post(f"{FT_URL}/api/workouts", headers=headers, files=files, data=data)
    return r.status_code == 201


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

    try:
        with open(LAST_MERGE_FILE) as f:
            last_merge = f.read().strip()
        if last_merge:
            after_ts = datetime.fromisoformat(last_merge).timestamp()
        else:
            after_ts = (datetime.now() - timedelta(days=7)).timestamp()
    except FileNotFoundError:
        after_ts = (datetime.now() - timedelta(days=7)).timestamp()

    print(f"Merging activities after {datetime.fromtimestamp(after_ts).strftime('%Y-%m-%d')}")

    activities = fetch_strava_activities(after_ts, strava_headers)
    cycling = [a for a in activities if a.get("type") in ("Ride", "VirtualRide")]
    print(f"Found {len(cycling)} cycling activities in range")

    pairs = []
    used = set()
    for i, act1 in enumerate(cycling):
        if act1["id"] in used:
            continue
        for j, act2 in enumerate(cycling):
            if i == j or act2["id"] in used:
                continue
            t1 = datetime.fromisoformat(act1["start_date"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(act2["start_date"].replace("Z", "+00:00"))
            if abs((t1 - t2).total_seconds()) < 300:
                pairs.append((act1, act2))
                used.add(act1["id"])
                used.add(act2["id"])
                break

    print(f"Found {len(pairs)} pairs to merge")

    for act1, act2 in pairs:
        streams1 = get_streams(act1["id"], strava_headers)
        streams2 = get_streams(act2["id"], strava_headers)

        has_hr1 = bool(streams1.get("heartrate", {}).get("data", []))
        has_cad1 = bool(streams1.get("cadence", {}).get("data", []))
        has_hr2 = bool(streams2.get("heartrate", {}).get("data", []))
        has_cad2 = bool(streams2.get("cadence", {}).get("data", []))

        name1 = act1.get("name", "").replace('"', "")
        name2 = act2.get("name", "").replace('"', "")
        print(f"  {act1['id']} ({name1}): HR={has_hr1}, Cad={has_cad1}")
        print(f"  {act2['id']} ({name2}): HR={has_hr2}, Cad={has_cad2}")

        if has_hr1 and not has_cad1:
            aw, other = streams1, streams2
            aw_act = act1
        elif has_hr2 and not has_cad2:
            aw, other = streams2, streams1
            aw_act = act2
        else:
            aw, other = streams1, streams2
            aw_act = act1

        base_time = datetime.fromisoformat(aw_act["start_date"].replace("Z", "+00:00")).replace(tzinfo=None)

        gpx = merge_gpx(aw, other, base_time)
        if gpx and upload_gpx(gpx, ft_headers):
            print(f"  Merged and uploaded!")
        else:
            print(f"  Failed")
        time.sleep(0.5)

    with open(LAST_MERGE_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())
    print("Done!")


main()
