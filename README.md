# Compose Guardian - Docker Compose 自动更新守护者

![GitHub License](https://img.shields.io/github/license/jasonchio-cn/compose-guardian)
![Docker Image Size](https://img.shields.io/docker/image-size/jasonchio/compose-guardian)

**Compose Guardian** 是一个智能的 Docker Compose 服务自动更新工具。它会监控您的 compose 项目，自动拉取最新的镜像并安全地更新服务，同时提供完整的回滚机制和健康检查。

## 🎯 核心特性

- **智能更新检测**：只更新有新镜像的服务，避免不必要的重启
- **安全回滚机制**：更新失败时自动回滚到之前的版本
- **健康状态验证**：确保更新后的服务正常运行
- **灵活调度**：支持定时任务（cron）或间隔执行
- **手动控制模式**：运行一次后立即退出，适合集成到 CI/CD
- **详细日志输出**：实时显示操作进度和状态
- **钉钉通知**：支持钉钉 webhook 通知更新结果
- **报告生成**：每次运行都会生成详细的 JSON 报告

## 🚀 快速开始

### 使用 Docker Compose 部署

1. **创建配置文件**

创建 `docker-compose.yml`：

```yaml
version: '3.8'
services:
  compose-guardian:
    image: jasonchio/compose-guardian:latest
    container_name: compose-guardian
    volumes:
      # 挂载 Docker socket 以管理容器
      - /var/run/docker.sock:/var/run/docker.sock
      # 挂载您的 compose 项目目录（只读）
      - /opt/compose/projects:/compose/projects:ro
      # 挂载报告目录（可选）
      - /opt/compose-guardian/reports:/reports
    environment:
      # 指定 compose 项目根目录
      - COMPOSE_ROOT=/compose/projects
      # 可选：设置更新调度（每小时检查一次）
      - SCHEDULE_EVERY=1h
      # 可选：忽略特定服务
      - IGNORE_SERVICES=database,cache
    restart: unless-stopped
```

2. **启动服务**

```bash
# 创建必要的目录
sudo mkdir -p /opt/compose/projects /opt/compose-guardian/reports

# 启动 Compose Guardian
docker compose up -d
```

### 使用 Docker 命令部署

```bash
docker run -d \
  --name compose-guardian \
  --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/compose/projects:/compose/projects:ro \
  -v /opt/compose-guardian/reports:/reports \
  -e COMPOSE_ROOT=/compose/projects \
  -e SCHEDULE_EVERY=1h \
  jasonchio/compose-guardian:latest
```

### 手动控制更新（运行一次后退出）

如果您希望完全控制更新时机（比如在 CI/CD 流水线中），可以不设置调度参数：

```yaml
# docker-compose.yml - 手动模式
version: '3.8'
services:
  compose-guardian:
    image: jasonchio/compose-guardian:latest
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /opt/compose/projects:/compose/projects:ro
      - /opt/compose-guardian/reports:/reports
    environment:
      - COMPOSE_ROOT=/compose/projects
      # 注意：不设置 SCHEDULE_CRON 和 SCHEDULE_EVERY
    # 不设置 restart，因为只需要运行一次
```

然后手动触发更新：

```bash
# 方式1：使用 docker compose run
docker compose run --rm compose-guardian

# 方式2：直接执行容器内的命令
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/compose/projects:/compose/projects:ro \
  -v /opt/compose-guardian/reports:/reports \
  -e COMPOSE_ROOT=/compose/projects \
  jasonchio/compose-guardian:latest
```

## ⚙️ 环境变量配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `COMPOSE_ROOT` | `/compose/projects` | Compose 项目根目录 |
| `SCHEDULE_CRON` | (空) | Cron 表达式，如 `"0 */6 * * *"` |
| `SCHEDULE_EVERY` | (空) | 间隔时间，如 `"30m"`, `"2h"`, `"15s"` |
| `IGNORE_SERVICES` | (空) | 要忽略的服务列表，用逗号分隔 |
| `HEALTH_TIMEOUT_SECONDS` | `180` | 健康检查超时时间（秒） |
| `STABLE_SECONDS` | `30` | 无健康检查服务的稳定时间（秒） |
| `VERIFY_POLL_SECONDS` | `3` | 健康检查轮询间隔（秒） |
| `DINGTALK_WEBHOOK` | (空) | 钉钉 webhook URL |

### 调度配置说明

- **不设置任何调度参数**：运行一次后立即退出（手动控制模式）
- **设置 `SCHEDULE_EVERY`**：立即执行一次，然后按指定间隔重复
- **设置 `SCHEDULE_CRON`**：按 cron 表达式定时执行

> 💡 **注意**：`SCHEDULE_CRON` 和 `SCHEDULE_EVERY` 不能同时设置，优先使用 `SCHEDULE_CRON`。

### 时间格式说明

`SCHEDULE_EVERY` 支持以下格式：
- `15s` - 15秒
- `5m` - 5分钟  
- `2h` - 2小时

## 📁 目录结构要求

Compose Guardian 会扫描 `COMPOSE_ROOT` 目录下的以下文件：

```
/compose/projects/
├── docker-compose.yml          # 单个项目的 compose 文件
├── project1/
│   └── docker-compose.yml      # 多个项目，每个项目一个目录
├── project2/
│   └── compose.yaml
└── my-app/
    └── docker-compose.yaml
```

支持的文件名：
- `docker-compose.yml`
- `docker-compose.yaml`  
- `compose.yml`
- `compose.yaml`

## 📊 运行报告

每次运行都会在 `/reports` 目录下生成 JSON 格式的报告文件，包含：

- 更新的服务列表
- 镜像 ID 变化对比
- 备份标签信息
- 健康检查结果
- 回滚状态（如果发生）

## 🔔 钉钉通知

配置 `DINGTALK_WEBHOOK` 环境变量即可启用钉钉通知。通知内容包括：

- 更新状态（成功/失败/回滚）
- 涉及的服务
- 镜像变化详情

## 🛡️ 安全机制

1. **只更新有变化的服务**：避免不必要的服务重启
2. **自动备份**：更新前为旧镜像打标签备份
3. **健康验证**：确保新版本服务正常运行
4. **自动回滚**：验证失败时自动恢复到之前版本
5. **清理机制**：成功更新后自动清理备份镜像

## 📝 使用示例

### 场景1：自动定时更新

```yaml
environment:
  - COMPOSE_ROOT=/compose/projects
  - SCHEDULE_EVERY=6h  # 每6小时检查一次
  - IGNORE_SERVICES=database,redis  # 忽略数据库和缓存服务
```

### 场景2：CI/CD 集成

```bash
# 在 GitHub Actions 或其他 CI 中
- name: Update services
  run: |
    docker run --rm \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v ${{ github.workspace }}:/compose/projects:ro \
      -e COMPOSE_ROOT=/compose/projects \
      jasonchio/compose-guardian:latest
```

### 场景3：手动触发更新

```bash
# 临时更新所有服务
docker compose run --rm compose-guardian

# 查看更新报告
ls -la /opt/compose-guardian/reports/
cat /opt/compose-guardian/reports/latest.json
```

## 🐳 开发与构建

```bash
# 克隆项目
git clone https://github.com/jasonchio-cn/compose-guardian.git
cd compose-guardian

# 构建镜像
docker build -t compose-guardian .

# 本地测试
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v ./test-projects:/compose/projects:ro \
  -e COMPOSE_ROOT=/compose/projects \
  compose-guardian
```

## 📄 许可证

MIT License - 详情请查看 [LICENSE](LICENSE) 文件。

---

> **提示**：首次使用建议先在测试环境中验证，确保您的服务能够正确处理更新和回滚操作。
