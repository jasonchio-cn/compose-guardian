FROM python:3.12-alpine

RUN apk add --no-cache \
    bash curl coreutils \
    docker-cli docker-cli-compose

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/src/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

# Defaults
ENV COMPOSE_FILE=/compose/docker-compose.yml
ENV COMPOSE_PROJECT_NAME=
ENV REPORT_DIR=/reports
ENV HEALTH_TIMEOUT_SECONDS=180
ENV STABLE_SECONDS=30
ENV VERIFY_POLL_SECONDS=3
ENV IGNORE_SERVICES=
ENV SCHEDULE_CRON=
ENV SCHEDULE_EVERY=
ENV DINGTALK_WEBHOOK=

CMD ["python", "-m", "compose_updater.main"]
