#!/bin/bash
# Wrapper script to sync from Strava, merge workouts, and clean bad data

cd /app

# File to store last sync/merge timestamps
LAST_SYNC_FILE="/app/.last_sync_date"
LAST_MERGE_FILE="/app/.last_merge_date"

# Get last sync date from FitTrackee (incremental sync)
echo "=== Getting last sync time ==="
python3 -c "
import requests, json
ft_token = json.loads(open('/app/.fittrackee.tokens.json').read())['access_token']
r = requests.get('http://127.0.0.1:5001/api/workouts?per_page=1&order=desc', headers={'Authorization': f'Bearer {ft_token}'})
if r.status_code == 200 and r.json()['data']['workouts']:
    dt = r.json()['data']['workouts'][0]['workout_date']
    from email.utils import parsedate_to_datetime
    d = parsedate_to_datetime(dt).strftime('%Y-%m-%d')
    print(d)
    open('$LAST_SYNC_FILE', 'w').write(d)
"

# Get last merge date
if [ -f "$LAST_MERGE_FILE" ]; then
    LAST_MERGE_DATE=$(cat $LAST_MERGE_FILE)
    echo "Last merge: $LAST_MERGE_DATE"
    MERGE_FROM="--from $LAST_MERGE_DATE"
else
    # Default to 90 days
    MERGE_FROM="--from $(date -d '90 days ago' +%Y-%m-%d)"
fi

echo "=== Starting Strava sync (incremental) ==="
python3 sync_raw.py

echo "=== Merging workouts (from $MERGE_FROM) ==="
python3 merge_aw.py $MERGE_FROM --to $(date +%Y-%m-%d)

# Save last merge date
date +%Y-%m-%d > $LAST_MERGE_FILE

echo "=== Cleaning up duplicates ==="
python3 cleanup_dupes.py

echo "=== Cleaning bad speed points ==="
python3 -c "
import psycopg2
import os
DB_HOST = os.environ.get('POSTGRES_HOST', '172.24.0.2')
DB_PASSWORD = os.environ.get('POSTGRES_PASSWORD', 'mysecretpassword')
conn = psycopg2.connect(host=DB_HOST, port='5432', dbname='fittrackee', user='fittrackee', password=DB_PASSWORD)
cur = conn.cursor()
cur.execute('''
WITH cleaned AS (
    SELECT cp.workout_id as wid, 
           (SELECT json_agg(p) FROM json_array_elements(cp.points::json) p 
            WHERE (p->>\\'speed\\')::numeric <= 100) as new_pts
    FROM workout_segments cp
)
UPDATE workout_segments 
SET points = cleaned.new_pts::json
FROM cleaned
WHERE workout_segments.workout_id = cleaned.wid;
''')
conn.commit()
print(f'Cleaned {cur.rowcount} segments')
conn.close()
"

echo "=== Done ==="