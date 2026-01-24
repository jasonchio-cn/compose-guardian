# compose-guardian

一个容器化的 Docker Compose“守护进程”，用于监控 Compose 栈的镜像更新（同 tag、不同 image ID），自动拉取并仅重建发生变化的服务；在更新后进行健康验证，失败时只回滚变更过的服务。

## GitHub Actions 构建
本仓库包含一个工作流，用于构建并推送多架构镜像（amd64/arm64）到 GitHub Container Registry（GHCR）：
- 工作流：`.github/workflows/docker-build.yml`
- 镜像：`ghcr.io/<owner>/compose-guardian`（默认）

使用方式：
1. 将仓库推送到 GitHub。
2. 确保该仓库已启用 GitHub Actions。
3. 推送到 `main`（发布 `main` tag）和/或创建 tag（例如 `v0.1.0`，发布 `v0.1.0`）。
4. 在服务器上拉取：`docker pull ghcr.io/<owner>/compose-guardian:<tag>`。

如果你更希望推送到 Docker Hub，也可以把 workflow 的登录/推送步骤替换为使用 `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` secrets。

## 行为说明
- 更新检测：执行 `docker compose pull` 后，分别在 pull 前/后通过 `docker image inspect <repo:tag> .Id` 对比 image ID；image ID 发生变化的服务视为“需要更新”。
- 应用更新：仅当至少一个服务发生变化时才执行。
  - 将旧 image ID 打一个唯一的备份 tag（用于回滚）。
  - 执行 `docker compose up -d --force-recreate --no-deps <changed services...>`，只重建变更的服务。
- 健康验证：
  - 若容器定义了 `HEALTHCHECK`：等待容器达到 `healthy` 且 `running`。
  - 否则：要求容器保持 `running` 且在一段时间内 `RestartCount` 稳定（默认 30 秒，可配置）。
- 回滚：仅回滚变更的服务；将原 tag 恢复指向备份镜像，再次对变更服务执行 `up -d --force-recreate --no-deps`。
- 清理：更新成功后删除备份 tag；并在无容器引用时清理旧 image ID。
- 通知：在 SUCCESS/ROLLBACK/FAILED 时发送钉钉通知；SKIPPED（无更新）不发送。
- 报告：每次运行会将 JSON 报告写入 `REPORT_DIR`。

## 配置（环境变量）
- `COMPOSE_FILE`（默认：`/compose/docker-compose.yml`）
- `COMPOSE_PROJECT_NAME`（可选）
- `SCHEDULE_CRON`（可选，例如：`0 3 * * *`）
- `SCHEDULE_EVERY`（可选，例如：`12h`、`30m`）
- `IGNORE_SERVICES`（可选，逗号分隔）
- `HEALTH_TIMEOUT_SECONDS`（默认：`180`）
- `STABLE_SECONDS`（默认：`30`）
- `VERIFY_POLL_SECONDS`（默认：`3`）
- `REPORT_DIR`（默认：`/reports`）
- `DINGTALK_WEBHOOK`（可选）

调度优先级：如果设置了 `SCHEDULE_CRON` 则优先使用；否则使用 `SCHEDULE_EVERY`；两者都未设置则仅执行一次后退出。

## 运行要求
运行该容器通常需要：
- 挂载 Docker socket：`/var/run/docker.sock`
- 挂载 Compose 文件所在目录，并保证路径与 `COMPOSE_FILE` 一致
- 容器内可用 `docker compose` 插件（Dockerfile 已安装）
