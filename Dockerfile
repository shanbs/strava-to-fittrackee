FROM python:3.11-slim

RUN pip install poetry && pip install psycopg2-binary
WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
COPY strava_to_fittrackee ./strava_to_fittrackee
RUN poetry config virtualenvs.create false && poetry install --only main --no-interaction
COPY .env.example ./
COPY sync_raw.py merge_aw.py run_sync.sh cleanup_dupes.py remove_dupes.py ./
RUN chmod +x run_sync.sh
CMD ["bash", "run_sync.sh"]