# Base image pinned by manifest-list digest so cross-arch builds resolve to a
# known multi-platform set. Refresh with:
#   curl -fsSL -I -H "Authorization: Bearer $(curl -fsSL 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull' | jq -r .token)" \
#     -H 'Accept: application/vnd.oci.image.index.v1+json' \
#     https://registry-1.docker.io/v2/library/python/manifests/3.11-slim | grep -i docker-content-digest
FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/registry.db \
    WEB_CONCURRENCY=1 \
    GUNICORN_TIMEOUT=120 \
    GUNICORN_MAX_REQUESTS=1000 \
    GUNICORN_MAX_REQUESTS_JITTER=100

WORKDIR /app

# Node 20 from NodeSource via signed apt repository (not `curl … | bash`). The
# keyring step pins trust to a specific GPG fingerprint; subsequent apt installs
# verify every package against it. Replaces the legacy setup_20.x shell pipe.
ARG NODESOURCE_KEY_FPR=9FD3B784BC1C6FC31A8A0A1C1655A0AB68576280
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && gpg --no-default-keyring --keyring /etc/apt/keyrings/nodesource.gpg --list-keys --with-colons \
        | awk -F: '/^fpr:/ {print $10; exit}' \
        | grep -Fxq "${NODESOURCE_KEY_FPR}" \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install the prod set from the pinned lockfile (requirements.txt is regenerated
# from requirements.in via `make lockfile`). `--no-deps` plus a complete
# transitive-pinned lockfile blocks pip from silently resolving an extra
# package version at build time. This kills the "compromised PyPI release
# lands on next build" supply-chain class. Adding `--require-hashes` is the
# next hardening step (see docs/runbooks/deploy.md).
COPY requirements.txt .
RUN pip install --no-cache-dir --no-deps -r requirements.txt

# pytest / pytest-asyncio / coverage / checkov are now pinned in
# requirements.in (they are runtime tools that agents shell out to, not
# dev-only deps). The duplicate `pip install` block that lived here was
# dropped when the lockfile rolled out. If you reintroduce a runtime tool,
# add it to requirements.in and run `make lockfile`.

# hadolint: dockerfile_analyzer shells out to `hadolint --format json`. When
# absent, the agent falls back to regex heuristics and flags `degraded_mode`.
# Pin to a recent stable release; static binary, no apt dependency. The SHA256
# is the upstream-published checksum for v2.12.0/hadolint-Linux-x86_64 — refresh
# with `curl -fsSL https://github.com/hadolint/hadolint/releases/download/<ver>/hadolint-Linux-x86_64.sha256`.
ARG HADOLINT_VERSION=v2.12.0
ARG HADOLINT_SHA256=56de6d5e5ec427e17b74fa48d51271c7fc0d61244bf5c90e828aab8362d55010
RUN curl -fsSL -o /tmp/hadolint \
        "https://github.com/hadolint/hadolint/releases/download/${HADOLINT_VERSION}/hadolint-Linux-x86_64" \
    && echo "${HADOLINT_SHA256}  /tmp/hadolint" | sha256sum -c - \
    && install -m 0755 /tmp/hadolint /usr/local/bin/hadolint \
    && rm /tmp/hadolint

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
# jest (and the npm CLI) is installed globally so ci_failure_reproducer can
# reproduce JS test failures the same way pytest covers Python ones.
# Exact versions pinned so the image is reproducible; bump deliberately, not on
# every build.
RUN npm install -g lighthouse@11.7.1 jest@29.7.0 \
    && npm cache clean --force

# golang: ci_failure_reproducer also picks up `go test ...` commands from
# CI logs. Without a Go toolchain those re-runs reported "go: command not
# found" instead of the real failure. ~150MB but worth it for the agent to
# diagnose real Go failures rather than a missing-toolchain artifact.
RUN apt-get update \
    && apt-get install -y --no-install-recommends golang-go git \
    && rm -rf /var/lib/apt/lists/*

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
