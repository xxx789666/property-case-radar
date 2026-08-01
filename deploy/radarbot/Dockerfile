# Deterministic base for the production RadarBot process. This image runs
# only apps.discord_bot.container_main: no scheduler or crawler entrypoint
# exists in the container configuration.
FROM python@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 1002 radarbot \
    && useradd --uid 1002 --gid 1002 --create-home --shell /usr/sbin/nologin radarbot

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
    && install -d -o radarbot -g radarbot -m 0700 /run/radarbot

USER radarbot

HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=6 \
    CMD ["python", "-c", "from pathlib import Path; raise SystemExit(0 if Path('/run/radarbot/ready.json').is_file() else 1)"]

ENTRYPOINT ["python", "-m", "apps.discord_bot.container_main"]
