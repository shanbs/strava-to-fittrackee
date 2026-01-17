FROM python:3.11-slim

RUN pip install poetry
COPY pyproject.toml poetry.lock ./
COPY strava_to_fittrackee ./strava_to_fittrackee
COPY .env.example ./

RUN poetry config virtualenvs.create false && poetry install --only main --no-interaction --no-root

WORKDIR /app
CMD ["poetry", "run", "python", "-m", "strava_to_fittrackee.s2f", "--sync", "-v", "2"]