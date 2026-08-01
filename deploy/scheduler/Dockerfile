# Dedicated deterministic image for the official-data scheduler.  It has no
# OpenAB/Codex state, Docker socket, host bind mounts, or generic command
# entrypoint.
FROM python@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 1003 radarscheduler \
    && useradd --uid 1003 --gid 1003 --create-home --shell /usr/sbin/nologin radarscheduler

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY apps /app/apps
COPY crawlers /app/crawlers
COPY database /app/database
COPY notifications /app/notifications
COPY scoring /app/scoring

RUN python -m pip install --no-cache-dir . \
    && python -m compileall -q \
        /app/apps /app/crawlers /app/database /app/notifications /app/scoring \
    && install -d -o radarscheduler -g radarscheduler -m 0700 \
        /run/radar-scheduler /var/cache/radar-scheduler

USER radarscheduler

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=6 \
    CMD ["python", "-c", "from pathlib import Path; raise SystemExit(0 if Path('/run/radar-scheduler/ready.json').is_file() else 1)"]

ENTRYPOINT ["python", "-m", "apps.scheduler.container_main"]
