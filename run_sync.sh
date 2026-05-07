#!/bin/bash
# Wrapper script to sync from Strava, merge workouts, and clean bad data

# Parse args: --sync-from YYYY-MM-DD --sync-to YYYY-MM-DD --merge-from YYYY-MM-DD --merge-to YYYY-MM-DD
SYNC_FROM=""
SYNC_TO=""
MERGE_FROM=""
MERGE_TO=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --sync-from)
            SYNC_FROM="$2"
            shift 2
            ;;
        --sync-to)
            SYNC_TO="$2"
            shift 2
            ;;
        --merge-from)
            MERGE_FROM="$2"
            shift 2
            ;;
        --merge-to)
            MERGE_TO="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

cd /app

# Build sync args
SYNC_ARGS=""
if [ -n "$SYNC_FROM" ]; then
    SYNC_ARGS="--from $SYNC_FROM"
    if [ -n "$SYNC_TO" ]; then
        SYNC_ARGS="$SYNC_ARGS --to $SYNC_TO"
    fi
fi

# Build merge args
MERGE_ARGS=""
if [ -n "$MERGE_FROM" ]; then
    MERGE_ARGS="--from $MERGE_FROM"
    if [ -n "$MERGE_TO" ]; then
        MERGE_ARGS="$MERGE_ARGS --to $MERGE_TO"
    fi
fi

echo "=== Starting Strava sync ==="
python3 sync_raw.py $SYNC_ARGS

echo "=== Merging workouts ==="
python3 merge_aw.py $MERGE_ARGS

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