"""
Merge duplicate Strava workouts in FitTrackee.

Detects overlapping activities from different devices (CYCPLUS M2 / Apple Watch)
and merges them into a single workout with combined HR + cadence data.

Usage:
    python3 -m strava_to_fittrackee.merge_duplicates [--dry-run] [--merge-days 90]

Author: Adapted for personal use
"""

import argparse
import json
import logging
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gpxpy
import pytz

logger = logging.getLogger("merge")
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")


# ── Config ────────────────────────────────────────────────────────────────────

FITTRACKEE_TOKEN_FILE = "/opt/fittrackee/strava-to-fittrackee-secrets/_data/.fittrackee.tokens.json"
STRAVA_TOKEN_FILE = "/opt/fittrackee/strava-to-fittrackee-secrets/_data/.strava.tokens.json"
FITTRACKEE_HOST = "fit.wwbb.duia.eu"
FITTRACKEE_CLIENT_ID = "fittrackee"
FITTRACKEE_CLIENT_SECRET = ""  # not needed for token-only auth

# Strict merge criteria (minutes, meters)
MERGE_TIME_WINDOW = 5  # start times must be within this many minutes
MERGE_DISTANCE_TOLERANCE = 100  # distances must differ by less than this


def load_token_file(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def fittrackee_auth(token_path: str) -> str:
    """Read access token and return it."""
    data = load_token_file(token_path)
    return data.get("access_token", data.get("refresh_token", ""))


def strava_auth(token_path: str) -> str:
    """Read Strava access token."""
    data = load_token_file(token_path)
    return data.get("access_token", "")


# ── FitTrackee API ────────────────────────────────────────────────────────────

FT_BASE = f"https://{FITTRACKEE_HOST}/api"


def ft_request(method: str, path: str, token: str, data: dict = None, files: dict = None) -> dict:
    """Generic FitTrackee API request."""
    url = f"{FT_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    
    if files:
        # For multipart file uploads
        boundary = "----GPXUploadBoundary"
        body = b""
        
        # Add data fields
        if data:
            for key, value in data.items():
                body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        
        # Add file
        file_data = files["file"].read()
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{files['filename']}\"\r\nContent-Type: application/gpx+xml\r\n\r\n".encode()
        body += file_data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
    else:
        if data:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode(),
                headers=headers,
                method=method,
            )
        else:
            req = urllib.request.Request(url, headers=headers, method=method)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 204 or resp.status == 201:
                return {}
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"FitTrackee {method} {path} failed: {e.code} - {error_body}")
        raise


def get_all_workouts(token: str, per_page: int = 50) -> list:
    """Paginate through all FitTrackee workouts."""
    all_workouts = []
    page = 1
    has_next = True
    
    while has_next:
        params = f"?per_page={per_page}&page={page}"
        data = ft_request("GET", f"/workouts{params}", token)
        
        if not data or "data" not in data:
            break
        
        workouts = data["data"].get("workouts", [])
        all_workouts.extend(workouts)
        
        pagination = data.get("pagination", {})
        has_next = pagination.get("has_next", False)
        page += 1
        
        logger.info(f"Fetched page {page} of workouts (total so far: {len(all_workouts)})")
        
        if len(workouts) < per_page:
            has_next = False
    
    return all_workouts


def get_workouts_by_date(token: str, date_str: str) -> list:
    """Get workouts for a specific date (YYYY-MM-DD)."""
    params = f"?per_page=50&page=1&from={date_str}&to={date_str}"
    data = ft_request("GET", f"/workouts{params}", token)
    if not data:
        return []
    return data.get("data", {}).get("workouts", [])


def upload_merged_workout(token: str, gpx_content: str, metadata: dict) -> dict:
    """Upload a merged GPX workout to FitTrackee."""
    from io import BytesIO
    
    filename = f"merged_{metadata.get('strava_ids', 'unknown')}.gpx"
    
    gpx_data = metadata.get("notes", "")
    
    files = {"file": BytesIO(gpx_content.encode()), "filename": filename}
    
    result = ft_request(
        "POST",
        "/workouts",
        token,
        data={"sport_id": metadata["sport_id"], "notes": metadata["notes"]},
        files=files,
    )
    
    return result


def delete_workout(token: str, workout_id: str) -> bool:
    """Delete a workout from FitTrackee."""
    try:
        ft_request("DELETE", f"/workouts/{workout_id}", token)
        return True
    except Exception as e:
        logger.error(f"Failed to delete workout {workout_id}: {e}")
        return False


def update_workout_notes(token: str, workout_id: str, notes: str) -> bool:
    """Update notes on a workout."""
    try:
        ft_request("PATCH", f"/workouts/{workout_id}", token, data={"notes": notes})
        return True
    except Exception as e:
        logger.error(f"Failed to update workout {workout_id} notes: {e}")
        return False


# ── Strava API ────────────────────────────────────────────────────────────────

STRAVA_BASE = "https://www.strava.com/api/v3"


def strava_request(method: str, endpoint: str, token: str, params: dict = None) -> dict:
    """Generic Strava API request."""
    url = f"{STRAVA_BASE}{endpoint}"
    headers = {"Authorization": f"Bearer {token}"}
    
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    
    req = urllib.request.Request(url, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        logger.error(f"Strava {endpoint} failed: {e.code} - {error_body}")
        raise


def get_recent_strava_activities(token: str, days: int = 120) -> list:
    """Get Strava activities from the last N days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    activities = []
    page = 1
    
    while True:
        per_page = 200
        params = {"per_page": per_page, "page": page}
        
        try:
            resp = strava_request("GET", "/athlete/activities", token, params)
        except Exception as e:
            logger.error(f"Failed to fetch Strava activities: {e}")
            break
        
        if not resp:
            break
        
        for activity in resp:
            start = datetime.strptime(activity["start_date"], "%Y-%m-%dT%H:%M:%SZ")
            if start < cutoff:
                return activities
            activities.append(activity)
        
        if len(resp) < per_page:
            break
        
        page += 1
        time.sleep(1)  # rate limit protection
    
    return activities


def get_activity_streams(token: str, activity_id: int, keys: List[str]) -> dict:
    """Get activity streams (heartrate, cadence, etc.)."""
    try:
        return strava_request("GET", f"/activities/{activity_id}/streams", token, {
            "keys": ",".join(keys),
            "key_by_type": "true",
        })
    except Exception as e:
        logger.error(f"Failed to get streams for activity {activity_id}: {e}")
        return {}


def get_activity_details(token: str, activity_id: int) -> dict:
    """Get full activity details including device name."""
    try:
        return strava_request("GET", f"/activities/{activity_id}", token)
    except Exception as e:
        logger.error(f"Failed to get activity {activity_id} details: {e}")
        return {}


# ── GPX Builder ───────────────────────────────────────────────────────────────

def build_merged_gpx(
    gpx_points: list,
    hr_data: list,
    cadence_data: list,
    distance: float,
    duration: int,
    start_time: datetime,
    title: str,
    device_names: list,
    strava_ids: list,
) -> str:
    """
    Build a GPX file with embedded HR and cadence data.
    
    Uses Garmin GPX extensions format:
    - HR: <gpxtpx:TrackPointExtension><gpxtpx:HeartRateBpm><gpxx:Value>...</gpxx:Value></gpxtpx:HeartRateBpm></gpxtpx:TrackPointExtension>
    - Cadence: <gpxtpx:TrackPointExtension><gpxtpx:Cadence>...</gpxtpx:Cadence></gpxtpx:TrackPointExtension>
    """
    gpx = gpxpy.gpx.GPX()
    gpx_track = gpxpy.gpx.GPXTrack()
    gpx_track.name = title
    gpx.tracks.append(gpx_track)
    
    gpx_segment = gpxpy.gpx.GPXTrackSegment()
    gpx_track.segments.append(gpx_segment)
    
    for i, point in enumerate(gpx_points):
        lat, lon, time_val, elevation = point
        
        hp = gpxpy.gpx.GPXTrackPoint(lat, lon, elevation=elevation, time=time_val)
        
        # Add HR
        if i < len(hr_data) and hr_data[i] is not None and hr_data[i] > 0:
            hr_elem = gpxpy.gpx.GPXExtension()
            hr_elem.text = str(hr_data[i])
            hp.extensions.append(hr_elem)
        
        # Add cadence
        if i < len(cadence_data) and cadence_data[i] is not None and cadence_data[i] > 0:
            cad_elem = gpxpy.gpx.GPXExtension()
            cad_elem.text = str(cadence_data[i])
            hp.extensions.append(cad_elem)
        
        gpx_segment.points.append(hp)
    
    return gpx.to_xml()


# ── Merge Logic ───────────────────────────────────────────────────────────────

def find_device_name(activity: dict) -> str:
    """Extract device name from Strava activity."""
    # Try the "primary_gps_device" field (most reliable)
    device = activity.get("primary_gps_device", {}).get("name", "")
    if device:
        return device
    
    # Fallback: check gear
    gear_name = activity.get("gear", {}).get("name", "")
    if gear_name:
        return f"Gear: {gear_name}"
    
    return "Unknown"


def activities_overlap(a1: dict, a2: dict) -> bool:
    """
    Check if two Strava activities should be merged.
    
    Criteria:
    - Same day
    - Start times within MERGE_TIME_WINDOW minutes
    - Distances within MERGE_DISTANCE_TOLERANCE meters
    - Different devices
    """
    t1 = datetime.strptime(a1["start_date"], "%Y-%m-%dT%H:%M:%SZ")
    t2 = datetime.strptime(a2["start_date"], "%Y-%m-%dT%H:%M:%SZ")
    
    # Same day check
    if t1.date() != t2.date():
        return False
    
    # Time window check
    time_diff = abs((t1 - t2).total_seconds())
    if time_diff > MERGE_TIME_WINDOW * 60:
        return False
    
    # Distance tolerance
    dist_diff = abs(a1["distance"] - a2["distance"])
    if dist_diff > MERGE_DISTANCE_TOLERANCE:
        return False
    
    # Different devices
    d1 = find_device_name(a1)
    d2 = find_device_name(a2)
    if d1 == d2:
        return False
    
    return True


def get_streams_for_activity(token: str, activity_id: int) -> Tuple[list, list, list, list]:
    """
    Get latlng, time, HR, and cadence streams for an activity.
    Returns (latlng_list, time_offsets, hr_list, cadence_list).
    """
    streams = get_activity_streams(token, activity_id, ["latlng", "time", "heartrate", "cadence"])
    
    latlng = [s for s in streams.values() if s.get("type") == "latlng"][0]["data"] if any(s.get("type") == "latlng" for s in streams.values()) else []
    time_offsets = [s for s in streams.values() if s.get("type") == "time"][0]["data"] if any(s.get("type") == "time" for s in streams.values()) else []
    hr = [s for s in streams.values() if s.get("type") == "heartrate"][0]["data"] if any(s.get("type") == "heartrate" for s in streams.values()) else []
    cadence = [s for s in streams.values() if s.get("type") == "cadence"][0]["data"] if any(s.get("type") == "cadence" for s in streams.values()) else []
    
    return latlng, time_offsets, hr, cadence


def merge_pair(token: str, strava_ids: List[int], dry_run: bool = False) -> bool:
    """
    Merge two Strava activities into a single FitTrackee workout.
    
    Returns True if merge was successful.
    """
    a1 = get_activity_details(token, strava_ids[0])
    a2 = get_activity_details(token, strava_ids[1])
    
    device1 = find_device_name(a1)
    device2 = find_device_name(a2)
    
    logger.info(f"Merging:")
    logger.info(f"  Activity {strava_ids[0]}: {device1}, dist={a1['distance']:.0f}m, dur={a1['moving_time']}s")
    logger.info(f"  Activity {strava_ids[1]}: {device2}, dist={a2['distance']:.0f}m, dur={a2['moving_time']}s")
    
    # Get streams for both activities
    latlng1, time1, hr1, cad1 = get_streams_for_activity(token, strava_ids[0])
    latlng2, time2, hr2, cad2 = get_streams_for_activity(token, strava_ids[1])
    
    # Determine which has HR and which has cadence
    has_hr_a1 = len(hr1) > 0 and max(hr1) > 0
    has_cad_a1 = len(cad1) > 0 and max(cad1) > 0
    has_hr_a2 = len(hr2) > 0 and max(hr2) > 0
    has_cad_a2 = len(cad2) > 0 and max(cad2) > 0
    
    logger.info(f"  A1 HR={has_hr_a1} cad={has_cad_a1}")
    logger.info(f"  A2 HR={has_hr_a2} cad={has_cad_a2}")
    
    # For merging, we need: one with HR + one with cadence
    if not ((has_hr_a1 and has_cad_a2) or (has_hr_a2 and has_cad_a1)):
        logger.warning(f"  Neither pair provides complete data. Skipping.")
        return False
    
    # Choose the better GPS track (the one with more points)
    if len(latlng1) >= len(latlng2):
        main_latlng, main_time, main_hr, main_cad = latlng1, time1, hr1, cad1
        other_hr, other_cad = hr2, cad2
        primary_device = device1
    else:
        main_latlng, main_time, main_hr, main_cad = latlng2, time2, hr2, cad2
        other_hr, other_cad = hr1, cad1
        primary_device = device2
    
    # Merge HR: use the one that exists
    final_hr = main_hr if main_hr else other_hr
    # Merge cadence: use the one that exists
    final_cad = main_cad if main_cad else other_cad
    
    # If we have HR from one and cadence from another but different length streams,
    # we need to interleave them. For simplicity, we just pick the one that exists.
    # A more sophisticated approach would downsample/interpolate.
    
    # Build GPX points
    start_time = datetime.strptime(a1["start_date"], "%Y-%m-%dT%H:%M:%SZ")
    gpx_points = []
    
    for i, (lat, lon) in enumerate(main_latlng):
        if i < len(main_time):
            point_time = start_time + timedelta(seconds=main_time[i])
        else:
            point_time = start_time + timedelta(seconds=i * 1)
        
        gpx_points.append((lat, lon, point_time, None))  # elevation not available
    
    # Build GPX
    title = a1.get("name", "Merged Ride")
    sport_id = 3  # Cycling (Sport) - adjust based on activity type
    
    notes = (
        f"Merged with Strava-to-FitTrackee\n"
        f"Original Strava activities: {', '.join(str(i) for i in strava_ids)}\n"
        f"Devices: {device1}, {device2}\n"
        f"This activity was merged from multiple device recordings.\n"
    )
    
    if dry_run:
        logger.info(f"  [DRY RUN] Would create merged workout:")
        logger.info(f"    Title: {title}")
        logger.info(f"    Points: {len(gpx_points)}")
        logger.info(f"    HR data: {len(final_hr)} points")
        logger.info(f"    Cadence data: {len(final_cad)} points")
        logger.info(f"    GPX size: {len(gpx_points)} points")
        return True
    
    # Generate GPX
    gpx_content = build_merged_gpx(
        gpx_points=gpx_points,
        hr_data=final_hr,
        cadence_data=final_cad,
        distance=max(a1["distance"], a2["distance"]),
        duration=max(a1["moving_time"], a2["moving_time"]),
        start_time=start_time,
        title=title,
        device_names=[device1, device2],
        strava_ids=strava_ids,
    )
    
    # Upload to FitTrackee
    metadata = {
        "sport_id": sport_id,
        "notes": notes,
    }
    
    logger.info("  Uploading merged workout to FitTrackee...")
    try:
        result = upload_merged_workout(token, gpx_content, metadata)
        if result and "data" in result:
            new_workout_id = result["data"]["workouts"][0]["id"]
            logger.info(f"  Created new workout: {new_workout_id}")
            return True
        else:
            logger.error(f"  Upload failed: {result}")
            return False
    except Exception as e:
        logger.error(f"  Upload error: {e}")
        return False


def find_overlapping_pairs(activities: list) -> List[List[int]]:
    """
    Find pairs of activities that should be merged.
    Returns list of [id1, id2] pairs.
    """
    pairs = []
    used = set()
    
    for i, a1 in enumerate(activities):
        if a1["id"] in used:
            continue
        
        for j, a2 in enumerate(activities):
            if i >= j:
                continue
            if a2["id"] in used:
                continue
            
            if activities_overlap(a1, a2):
                pairs.append([a1["id"], a2["id"]])
                used.add(a1["id"])
                used.add(a2["id"])
                logger.info(f"  Found overlapping pair: {a1['id']} + {a2['id']}")
                break
    
    return pairs


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Merge duplicate Strava workouts in FitTrackee")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually merge, just show what would happen")
    parser.add_argument("--merge-days", type=int, default=120, help="Look back N days (default: 120)")
    parser.add_argument("--strava-token", default=STRAVA_TOKEN_FILE)
    parser.add_argument("--fittrackee-token", default=FITTRACKEE_TOKEN_FILE)
    args = parser.parse_args()
    
    strava_token = strava_auth(args.strava_token)
    ft_token = fittrackee_auth(args.fittrackee_token)
    
    if not strava_token or not ft_token:
        logger.error("Failed to load tokens. Check token files.")
        sys.exit(1)
    
    logger.info(f"Looking for duplicate activities in the last {args.merge_days} days...")
    
    # Get Strava activities
    activities = get_recent_strava_activities(strava_token, args.merge_days)
    logger.info(f"Found {len(activities)} Strava activities in last {args.merge_days} days")
    
    # Find overlapping pairs
    pairs = find_overlapping_pairs(activities)
    
    if not pairs:
        logger.info("No overlapping pairs found. All clear!")
        return
    
    logger.info(f"Found {len(pairs)} overlapping pairs. Processing...")
    
    merged_count = 0
    skipped_count = 0
    
    for pair in pairs:
        logger.info(f"\nProcessing pair: {pair[0]} + {pair[1]}")
        
        if merge_pair(strava_token, pair, dry_run=args.dry_run):
            merged_count += 1
        else:
            skipped_count += 1
    
    logger.info(f"\nSummary:")
    logger.info(f"  Merged: {merged_count}")
    logger.info(f"  Skipped (insufficient data): {skipped_count}")
    logger.info(f"  Dry run: {args.dry_run}")
    
    if args.dry_run:
        logger.info("This was a dry run. No changes were made.")


if __name__ == "__main__":
    main()
