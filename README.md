# compose-guardian

一个容器化的 Docker Compose "守护进程"：扫描指定根目录下的多个 Compose 栈，仅对已经处于 up 状态的栈执行镜像更新（pull -> 对比 image ID -> 仅重建变更服务），并在更新后做健康验证；失败时仅回滚变更服务。

核心目标：不会因为检测到镜像变化而把从未启动过的栈拉起来。

## 它如何发现/过滤栈

通过 `COMPOSE_ROOT` 指定一个"容器内路径"作为栈根目录，启动时扫描 compose 文件：

- `COMPOSE_ROOT/(docker-)compose.yml|yaml`
- `COMPOSE_ROOT/*/(docker-)compose.yml|yaml`

对每个 compose 文件会先判断该栈是否"已 up"（默认定义为：至少存在 1 个 `running` 容器）：

```bash
docker compose -f <file> --project-directory <dir> ps -q --status running
```

若无输出则直接跳过，不执行 `pull/up`，因此不会把没启动过的栈拉起来。

## 快速开始（推荐用 docker compose）

compose-guardian 需要在容器内调用宿主机 Docker，所以必须挂载 Docker socket：`/var/run/docker.sock`。

1) 复制示例文件并按需修改：

```bash
cp docker-compose.example.yml docker-compose.yml
```

2) 重点确认：

- `volumes` 里的 `/opt/compose/projects:/compose/projects:ro`：把所有 Compose 栈根目录挂载进来（每个栈一个子目录）。
- `COMPOSE_ROOT`：必须是"容器内"根目录路径（例如 `/compose/projects`）。
- `REPORT_DIR` 与报告目录挂载：用于把每次运行的 JSON 报告持久化到宿主机。

3) 启动：

```bash
docker compose up -d
```

调度说明：若设置了 `SCHEDULE_CRON` 则按 cron 执行；否则若设置了 `SCHEDULE_EVERY` 则按固定间隔执行；两者都未设置则运行一次后退出。

## 推荐的目录结构

宿主机目录（示例）：

```text
/opt/compose/projects/
  stack-a/
    docker-compose.yml
    .env
  stack-b/
    compose.yml
```

容器内挂载对应为：`/compose/projects/...`。

提示：compose-guardian 会用 `--project-directory <compose文件所在目录>` 运行 compose 命令，因此像 `.env`、`env_file:`、相对路径 volume 等都会按该目录解析。请确保该目录在容器内可读。

## 行为说明

- 更新检测：对 up 的栈执行 `docker compose pull` 后，在 pull 前/后通过 `docker image inspect <repo:tag> .Id` 对比 image ID；image ID 发生变化的服务视为"需要更新"。
- 安全保护：若 pull 前/后任一侧 image ID 为空（例如本地没有该镜像），该服务不会被判定为 changed。
- 应用更新：仅当至少一个服务发生变化时才执行：
  - 将旧 image ID 打一个唯一的备份 tag（用于回滚）。
  - 执行 `docker compose up -d --force-recreate --no-deps <changed services...>`，只重建变更的服务。
- 健康验证：
  - 若容器定义了 `HEALTHCHECK`：等待容器达到 `healthy` 且 `running`。
  - 否则：要求容器保持 `running` 且在一段时间内 `RestartCount` 稳定（默认 30 秒，可配置）。
- 回滚：仅回滚变更的服务；将原 tag 恢复指向备份镜像，再次对变更服务执行 `up -d --force-recreate --no-deps`。
- 清理：更新成功后删除备份 tag；并在无容器引用时清理旧 image ID。
- 通知：在 SUCCESS/ROLLBACK/FAILED 时发送钉钉通知；SKIPPED（无更新/未 up）不发送。
- 报告：每次运行会将 JSON 报告写入 `REPORT_DIR`。

## 配置（环境变量）

- `COMPOSE_ROOT`（默认：`/compose/projects`）
  - Compose 栈根目录在"容器内"的路径。
- `SCHEDULE_CRON`（可选，例如：`0 3 * * *`）
  - cron 格式为 5 段（分 时 日 月 周）。
- `SCHEDULE_EVERY`（可选，例如：`12h`、`30m`）
  - 固定间隔执行；仅当未设置 `SCHEDULE_CRON` 时生效。
- `IGNORE_SERVICES`（可选，逗号分隔）
  - 这些服务即便检测到更新也会被跳过。
- `HEALTH_TIMEOUT_SECONDS`（默认：`180`）
  - 更新后健康验证的最大等待时间（秒）。
- `STABLE_SECONDS`（默认：`30`）
  - 当服务没有 `HEALTHCHECK` 时，用于判断 "RestartCount 稳定" 的持续时间。
- `VERIFY_POLL_SECONDS`（默认：`3`）
  - 健康验证轮询间隔（秒）。
- `REPORT_DIR`（默认：`/reports`）
  - 报告写入目录（容器内路径）。
- `DINGTALK_WEBHOOK`（可选）
  - 钉钉机器人 webhook；留空则不通知。

## 运行要求

- 挂载 Docker socket：`/var/run/docker.sock`
- 挂载 Compose 栈根目录，并保证路径与 `COMPOSE_ROOT` 一致
- 容器内可用 `docker compose` 插件（Dockerfile 已安装）
