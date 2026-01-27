# compose-guardian

一个容器化的 Docker Compose "守护者"：扫描根目录下的多个 Compose 服务栈，**仅更新已经启动的服务栈**（定义为：至少有一个正在运行的容器）。

## 更新流程

对于已启动的服务栈，更新流程如下：
- 拉取镜像
- 比较镜像 ID
- 仅重新创建发生变化的服务
- 验证服务健康状态
- 失败时仅回滚发生变化的服务

**核心保证**：绝不会启动从未启动过的服务栈。

## 服务栈发现与过滤

设置 `COMPOSE_ROOT`（容器内路径）作为服务栈的根目录。每次运行时会扫描：

- `COMPOSE_ROOT/(docker-)compose.yml|yaml`
- `COMPOSE_ROOT/*/(docker-)compose.yml|yaml`

在执行任何 `pull/up` 操作之前，会检查服务栈是否已经启动：

```bash
docker compose -f <file> --project-directory <dir> ps -q --status running
```

如果无输出，则跳过该服务栈。

## 快速开始（Docker Compose）

compose-guardian 需要访问宿主机的 Docker 引擎，因此必须挂载 Docker socket：`/var/run/docker.sock`。

### 1. 复制示例文件并编辑：

```bash
cp docker-compose.example.yml docker-compose.yml
```

### 2. 检查以下配置值：

- `volumes` 包含 `/opt/compose/projects:/compose/projects:ro`（你的服务栈根目录，以只读方式挂载）
- `COMPOSE_ROOT` 与容器内路径匹配（例如 `/compose/projects`）
- `REPORT_DIR` 与报告卷挂载匹配（用于持久化 JSON 报告）

### 3. 启动服务：

```bash
docker compose up -d
```

### 调度配置：

- 如果设置了 `SCHEDULE_CRON`，则按 cron 表达式运行（5个字段：分钟 小时 日 月 星期）
- 如果设置了 `SCHEDULE_EVERY`（且 `SCHEDULE_CRON` 为空），则按固定间隔运行
- **如果两者都未设置，则运行一次后立即退出**（适合手动控制更新）

## 推荐的目录结构

**宿主机示例**：

```text
/opt/compose/projects/
  stack-a/
    docker-compose.yml
    .env
  stack-b/
    compose.yml
```

在容器内挂载为：`/compose/projects/...`。

**注意**：compose-guardian 使用 `--project-directory <compose file dir>` 运行 docker compose，因此 `.env`、`env_file:` 和相对路径都会从该目录解析。

## 行为细节

- **更新检测**：运行 `docker compose pull`，然后通过 `docker image inspect <image> .Id` 比较镜像 ID
- **安全性**：如果拉取前后镜像 ID 缺失（例如镜像不存在），该服务不被视为已更改
- **应用更新**：如果有服务发生更改，运行 `docker compose up -d --force-recreate --no-deps <changed...>`
- **验证机制**：
  - 如果容器有 `HEALTHCHECK`，等待状态变为 `healthy` 和 `running`
  - 否则要求 `running` 状态且 `RestartCount` 在 `STABLE_SECONDS` 时间内保持稳定
- **回滚机制**：使用临时备份标签恢复已更改的服务，然后重新创建这些服务
- **清理机制**：成功时删除备份标签；仅在没有容器引用时删除旧的镜像 ID
- **通知机制**：发送钉钉通知（SUCCESS/ROLLBACK/FAILED）；SKIPPED 状态不通知
- **报告机制**：每次运行后向 `REPORT_DIR` 写入 JSON 格式的报告

## 手动控制更新（运行一次后退出）

当你需要手动控制更新时机时，可以不设置任何调度参数：

```yaml
services:
  compose-guardian:
    image: ghcr.io/jasonchio-cn/compose-guardian:latest
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /opt/compose/projects:/compose/projects:ro
      - /opt/compose-guardian/reports:/reports
    environment:
      COMPOSE_ROOT: /compose/projects
      # 不设置 SCHEDULE_CRON 和 SCHEDULE_EVERY
    # 容器运行一次更新后自动退出
```

然后你可以通过以下方式手动触发更新：

```bash
# 手动运行一次更新
docker compose run --rm compose-guardian

# 或者在现有容器中执行
docker compose exec compose-guardian python -m compose_guardian.main
```

这种方式非常适合：
- 集成到 CI/CD 流水线中
- 需要人工确认后再更新的场景
- 调试和测试更新流程

## 配置参数（环境变量）

- `COMPOSE_ROOT`（默认：`/compose/projects`）
- `SCHEDULE_CRON`（可选，例如 `0 3 * * *`）
- `SCHEDULE_EVERY`（可选，例如 `12h`、`30m`；仅在 `SCHEDULE_CRON` 为空时使用）
- `IGNORE_SERVICES`（可选，逗号分隔的服务名列表）
- `HEALTH_TIMEOUT_SECONDS`（默认：`180`）
- `STABLE_SECONDS`（默认：`30`）
- `VERIFY_POLL_SECONDS`（默认：`3`）
- `REPORT_DIR`（默认：`/reports`）
- `DINGTALK_WEBHOOK`（可选，钉钉机器人 webhook URL）