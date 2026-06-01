FROM python:3.12-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    TZ=Europe/Berlin

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        iw iproute2 mtr-tiny traceroute ca-certificates tzdata zip tini curl \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 pingwatch \
 && useradd  --system --uid 10001 --gid 10001 --home /app --shell /usr/sbin/nologin pingwatch

WORKDIR /app

COPY --chown=pingwatch:pingwatch requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY --chown=pingwatch:pingwatch src /app/src
COPY --chown=pingwatch:pingwatch docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN mkdir -p /data /data/logs /data/backups \
 && chown -R pingwatch:pingwatch /data

USER pingwatch
EXPOSE 5000

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "pingwatch.api.app:app", "--host", "127.0.0.1", "--port", "5000", "--app-dir", "/app/src", "--workers", "1", "--loop", "uvloop"]
