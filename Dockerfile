# Single Cloud Run service: FastAPI serves /api and the built React app from the same origin.
# Build:  gcloud builds submit --config cloudbuild.yaml   (Cloud Build -> Artifact Registry -> Cloud Run)

FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    STATIC_DIR=/app/frontend/dist
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend/app backend/app
COPY backend/scripts backend/scripts
COPY data data
COPY --from=web /web/dist frontend/dist
RUN useradd --uid 10001 --no-create-home app && mkdir -p /app/var && chown app /app/var
USER app
WORKDIR /app/backend
# One worker: background jobs (forecast refresh, robustness evaluation) run in-process; job state lives in the DB.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
