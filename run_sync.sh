#!/bin/bash
# Wrapper script to sync from Strava, merge workouts, and clean bad data

echo "=== Starting Strava sync ==="
cd /app
python3 sync_raw.py

echo "=== Merging workouts ==="
python3 merge_aw.py

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