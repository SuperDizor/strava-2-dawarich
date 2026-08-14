FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    STATE_DIR=/data/state \
    GPX_DIR=/data/gpx \
    LOG_DIR=/data/logs

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY strava_gpx.py docker-entrypoint.sh ./
RUN chmod +x /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["sleep", "infinity"]
