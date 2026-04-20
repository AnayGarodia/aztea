FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/registry.db \
    WEB_CONCURRENCY=1 \
    GUNICORN_TIMEOUT=120 \
    GUNICORN_MAX_REQUESTS=1000 \
    GUNICORN_MAX_REQUESTS_JITTER=100

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["sh", "-c", "python -m core.migrate && exec gunicorn server:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers ${WEB_CONCURRENCY} \
  --bind 0.0.0.0:8000 \
  --timeout ${GUNICORN_TIMEOUT} \
  --max-requests ${GUNICORN_MAX_REQUESTS} \
  --max-requests-jitter ${GUNICORN_MAX_REQUESTS_JITTER} \
  --access-logfile - \
  --error-logfile -"]
