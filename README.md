# compose-guardian

一个容器化的 Docker Compose "守护进程"，用于监控 Compose 栈的镜像更新（同 tag、不同 image ID），自动拉取并仅重建发生变化的服务；在更新后进行健康验证，失败时只回滚变更过的服务。

## 镜像使用说明

你可以直接使用预构建镜像（推荐），或者在本地自行构建。

### 方式一：使用预构建镜像（GHCR）

1) 拉取镜像（将 `<owner>` 替换为你的 GitHub 用户名/组织名）：

```bash
docker pull ghcr.io/<owner>/compose-guardian:latest
```

2) 如果你的镜像仓库是私有的，需要先登录：

```bash
docker login ghcr.io
```

提示：公开仓库一般无需登录即可拉取。

### 方式二：本地构建镜像

在仓库根目录执行：

```bash
docker build -t compose-guardian:local .
```

然后在 compose 里把 `image` 改为 `compose-guardian:local`。

## 快速开始（推荐用 docker compose）

compose-guardian 需要在容器里调用宿主机 Docker，因此必须挂载 Docker socket：`/var/run/docker.sock`。

1) 复制示例文件并按需修改：

```bash
cp docker-compose.example.yml docker-compose.yml
```

2) 重点需要你确认的三件事：

- `volumes` 里的 `/opt/mystack:/compose`：把你的 Compose 栈目录挂载进来。
- `COMPOSE_FILE`：必须是 "容器内" 的 compose 文件路径（例如 `/compose/docker-compose.yml`）。
- 报告目录挂载：用于把每次运行的 JSON 报告持久化到宿主机（示例是 `/opt/compose-guardian/reports:/reports`）。

3) 启动：

```bash
docker compose up -d
```

调度说明：如果设置了 `SCHEDULE_CRON` 则按 cron 执行；否则如果设置了 `SCHEDULE_EVERY` 则按固定间隔执行；两者都未设置则运行一次后退出。

## 行为说明

- 更新检测：执行 `docker compose pull` 后，分别在 pull 前/后通过 `docker image inspect <repo:tag> .Id` 对比 image ID；image ID 发生变化的服务视为 "需要更新"。
- 应用更新：仅当至少一个服务发生变化时才执行。
  - 将旧 image ID 打一个唯一的备份 tag（用于回滚）。
  - 执行 `docker compose up -d --force-recreate --no-deps <changed services...>`，只重建变更的服务。
- 健康验证：
  - 若容器定义了 `HEALTHCHECK`：等待容器达到 `healthy` 且 `running`。
  - 否则：要求容器保持 `running` 且在一段时间内 `RestartCount` 稳定（默认 30 秒，可配置）。
- 回滚：仅回滚变更的服务；将原 tag 恢复指向备份镜像，再次对变更服务执行 `up -d --force-recreate --no-deps`。
- 清理：更新成功后删除备份 tag；并在无容器引用时清理旧 image ID。
- 通知：在 SUCCESS/ROLLBACK/FAILED 时发送钉钉通知；SKIPPED（无更新）不发送。
- 报告：每次运行会将 JSON 报告写入容器内 `/reports`（建议挂载到宿主机持久化）。

## 配置（环境变量）

- `COMPOSE_FILE`（默认：`/compose/docker-compose.yml`）
  - Compose 文件在"容器内"的路径。
  - 你必须把包含该文件的目录挂载到容器中，并保证路径能对应上。
- `COMPOSE_PROJECT_NAME`（可选）
  - 等同于 `docker compose -p <name>`。
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
- `DINGTALK_WEBHOOK`（可选）
  - 钉钉机器人 webhook；留空则不通知。

## 运行要求

运行该容器通常需要：

- 挂载 Docker socket：`/var/run/docker.sock`
- 挂载 Compose 文件所在目录，并保证路径与 `COMPOSE_FILE` 一致
- 容器内可用 `docker compose` 插件（Dockerfile 已安装）
