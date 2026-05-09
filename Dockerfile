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
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# browser_agent + visual_regression + accessibility_auditor + lighthouse_auditor
# all need a real Chromium. `playwright install-deps` pulls in the (long) list
# of shared-libs Chromium needs on slim Debian, then `playwright install
# chromium` downloads the browser binary. We do this as root before dropping
# privileges. ~300MB image growth, but otherwise these agents return 0% success
# in prod.
RUN python -m playwright install-deps chromium \
    && python -m playwright install chromium \
    && rm -rf /var/lib/apt/lists/*

# lighthouse_auditor shells out to the Node-native lighthouse CLI. Installed
# globally so it's on PATH for the appuser. ~80MB.
RUN npm install -g lighthouse@11 \
    && npm cache clean --force

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
