#!/bin/bash
# Wrapper script to sync from Strava, merge workouts, and clean bad data

echo "=== Incremental sync (s2f) ==="
cd /app
rm -f strava_to_fittrackee/s2f.pid
export OAUTHLIB_INSECURE_TRANSPORT=1
python3 -m strava_to_fittrackee.s2f --sync -v 2 2>&1 || echo "s2f sync failed"

echo "=== Starting Strava sync (raw) ==="
python3 sync_raw.py

echo "=== Merging workouts ==="
python3 merge_aw.py

echo "=== Cleaning bad speed points ==="
python3 -c "
import os, psycopg2
conn = psycopg2.connect(host='fittrackee_db', port='5432', dbname='fittrackee', user='fittrackee', password=os.environ.get('POSTGRES_PASSWORD', ''))
cur = conn.cursor()
cur.execute('''
WITH cleaned AS (
    SELECT cp.workout_id as wid, 
           (SELECT json_agg(p) FROM json_array_elements(cp.points::json) p 
            WHERE (p->>'speed')::numeric <= 100) as new_pts
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