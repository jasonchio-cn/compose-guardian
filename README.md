# compose-guardian

A containerized Docker Compose "guardian": scans multiple Compose stacks under a root directory and only updates stacks that are already up (defined as: at least one running container).

Update flow for an "up" stack:
- pull images
- compare image IDs
- recreate only changed services
- verify health
- rollback only changed services on failure

Key guarantee: it will not bring up stacks that were never started.

## Stack discovery and filtering

Set `COMPOSE_ROOT` (container path) as the root of your stacks. On each run it scans:

- `COMPOSE_ROOT/(docker-)compose.yml|yaml`
- `COMPOSE_ROOT/*/(docker-)compose.yml|yaml`

Before doing any `pull/up`, it checks whether the stack is already up:

```bash
docker compose -f <file> --project-directory <dir> ps -q --status running
```

If there is no output, the stack is skipped.

## Quick start (docker compose)

compose-guardian needs access to the host Docker Engine, so you must mount the Docker socket: `/var/run/docker.sock`.

1) Copy the example file and edit it:

```bash
cp docker-compose.example.yml docker-compose.yml
```

2) Check these values:

- `volumes` includes `/opt/compose/projects:/compose/projects:ro` (your stacks root, mounted read-only)
- `COMPOSE_ROOT` matches the container path (for example `/compose/projects`)
- `REPORT_DIR` matches the reports volume mount (to persist JSON reports)

3) Start:

```bash
docker compose up -d
```

Scheduling:
- If `SCHEDULE_CRON` is set, it runs on cron (5 fields: min hour day month weekday)
- Else if `SCHEDULE_EVERY` is set, it runs on a fixed interval
- Else it runs once and exits

## Recommended directory layout

Host (example):

```text
/opt/compose/projects/
  stack-a/
    docker-compose.yml
    .env
  stack-b/
    compose.yml
```

Mounted inside the container as: `/compose/projects/...`.

Note: compose-guardian runs docker compose with `--project-directory <compose file dir>` so `.env`, `env_file:`, and relative paths are resolved from that directory.

## Behavior details

- Update detection: runs `docker compose pull` then compares image IDs via `docker image inspect <image> .Id`.
- Safety: if the image ID is missing before or after pull (e.g. image not present), that service is not considered changed.
- Apply: if any services changed, runs `docker compose up -d --force-recreate --no-deps <changed...>`.
- Verify:
  - If the container has `HEALTHCHECK`, waits for `healthy` and `running`
  - Otherwise requires `running` and stable `RestartCount` for `STABLE_SECONDS`
- Rollback: restores only changed services using a temporary backup tag, then recreates those services again.
- Cleanup: deletes backup tags on success; deletes old image IDs only if no containers reference them.
- Notify: sends DingTalk notifications for SUCCESS/ROLLBACK/FAILED; SKIPPED does not notify.
- Reports: writes a JSON report per run to `REPORT_DIR`.

## Configuration (environment variables)

- `COMPOSE_ROOT` (default: `/compose/projects`)
- `SCHEDULE_CRON` (optional, e.g. `0 3 * * *`)
- `SCHEDULE_EVERY` (optional, e.g. `12h`, `30m`; only used when `SCHEDULE_CRON` is empty)
- `IGNORE_SERVICES` (optional, comma-separated)
- `HEALTH_TIMEOUT_SECONDS` (default: `180`)
- `STABLE_SECONDS` (default: `30`)
- `VERIFY_POLL_SECONDS` (default: `3`)
- `REPORT_DIR` (default: `/reports`)
- `DINGTALK_WEBHOOK` (optional)
