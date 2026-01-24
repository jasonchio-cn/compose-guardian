# compose-watchdog

A containerized watchdog that monitors a Docker Compose stack for image updates (same tag, different image ID), applies updates with `docker compose up -d --no-deps`, verifies health, and rolls back only the changed services on failure.

## GitHub Actions build
This repo includes a workflow that builds and pushes a multi-arch image (amd64/arm64) to GitHub Container Registry (GHCR):
- Workflow: `.github/workflows/docker-build.yml`
- Image: `ghcr.io/<owner>/compose-watchdog` (by default)

To use it:
1. Push this repo to GitHub.
2. Ensure Actions is enabled for the repo.
3. Push to `main` (publishes a `main` tag) and/or create a tag like `v0.1.0` (publishes `v0.1.0`).
4. Pull from your server: `docker pull ghcr.io/<owner>/compose-watchdog:<tag>`.

If you want Docker Hub instead, I can swap the login/push part to use `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` secrets.

## Behavior
- Update detection: after `docker compose pull`, compare `docker image inspect <repo:tag> .Id` before/after. Any service whose image ID changed is considered updated.
- Apply: only if at least one service changed.
  - Backup old image ID by tagging it with a unique backup tag.
  - Run `docker compose up -d --force-recreate --no-deps <changed services...>`.
- Verify:
  - If container has HEALTHCHECK: wait for `healthy` and `running`.
  - Else: require `running` and stable `RestartCount` for 30s (configurable).
- Rollback: only changed services. Restore the original tag to the backed up image and `up -d --force-recreate --no-deps` again.
- Cleanup: on success, delete backup tags and delete old image IDs if no container references them.
- Notifications: DingTalk is sent for SUCCESS/ROLLBACK/FAILED. SKIPPED (no updates) does not send.
- Reporting: each run writes a JSON report to `REPORT_DIR`.

## Configuration (env)
- `COMPOSE_FILE` (default: `/compose/docker-compose.yml`)
- `COMPOSE_PROJECT_NAME` (optional)
- `SCHEDULE_CRON` (optional, e.g. `0 3 * * *`)
- `SCHEDULE_EVERY` (optional, e.g. `12h`, `30m`)
- `IGNORE_SERVICES` (optional, comma-separated)
- `HEALTH_TIMEOUT_SECONDS` (default: `180`)
- `STABLE_SECONDS` (default: `30`)
- `VERIFY_POLL_SECONDS` (default: `3`)
- `REPORT_DIR` (default: `/reports`)
- `DINGTALK_WEBHOOK` (optional)

Precedence: if `SCHEDULE_CRON` is set, it is used; otherwise `SCHEDULE_EVERY` is used; otherwise the updater runs once and exits.

## Runtime
The updater expects:
- Docker socket mounted: `/var/run/docker.sock`
- Compose file directory mounted to match `COMPOSE_FILE`
- `docker compose` plugin available in the container image (Dockerfile installs it).
