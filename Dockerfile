FROM python:3.11-slim

RUN pip install poetry
WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
COPY strava_to_fittrackee ./strava_to_fittrackee
RUN poetry config virtualenvs.create false && poetry install --only main --no-interaction
COPY .env.example ./
CMD ["poetry", "run", "python", "-m", "strava_to_fittrackee.s2f", "--sync", "-v", "2"]