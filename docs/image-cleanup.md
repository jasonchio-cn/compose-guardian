# 保守镜像清理

新增独立入口 `compose_guardian.image_cleanup`。默认只生成预览，不挂接更新调度、不拉取或构建镜像，不启动/停止/删除容器，也不清理数据卷、网络或构建缓存。

## 保留规则

- 所有容器实际使用的镜像 ID，包括停止中的容器；同时保护容器原始镜像名称当前指向的缓存镜像。
- 选定 Compose 根目录和显式 manifest 中引用的本地镜像。通过 Compose 自身解析变量、所有 profiles、include/extends 及文件组合，不自行解析 YAML。
- `local/*` 镜像先遵循容器、Compose、恢复记录及父镜像保护，再按完整仓库名额外保留最近 **2 个未引用的镜像 ID**，更旧的未引用版本进入候选；例如 `local/app:v1` 与 `local/app:v2` 属于同一仓库 `local/app`。
- Guardian 状态目录中 current、history、pending 引用的镜像及其标签。其保留版本数仍由更新核心管理，本命令不越过回滚记录。
- 保留上述镜像的父镜像，避免清理本地构建的依赖链。

其他镜像列为候选。预览包含完整不可变 ID、标签、摘要、保护原因、缺失的缓存引用以及当前环境指纹。不存在于本地的 Compose 镜像只列为缺失，不尝试下载。

### 本地历史版本如何计算

- 按 Docker 的镜像创建时间 `Created` 从新到旧排序，统一时区并保留纳秒精度；时间完全相同时按完整 ID 确定稳定顺序。标签名称、拉取时间和打标签时间不参与排序。无法解析未引用本地镜像的创建时间时中止预览。
- 一个 ID 的多个标签只算一个版本。`docker.io/local/app:v1` 等默认仓库别名统一归到 `local/app`；每个完整仓库名各有 2 个名额。
- 容器（含停止中）、Compose、恢复记录引用的版本及其父镜像不占额外名额。这些必要版本可有多个，全部保留。
- 一个 ID 同时属于多个本地仓库时，各仓库独立选取历史版本，任一仓库保留就保留整个 ID。额外保留的历史镜像的父镜像也保留，因此实际保留数量可能超过 2。
- 完全失去本地标签的旧镜像无法可靠归属某个仓库，按普通无标签镜像处理，仍遵循引用和父镜像保护。

预览的 `local_history` 按仓库列出 `retained_unused_ids`（额外保留）和 `older_unused_ids`（较旧版本）。较旧版本若仍被其他仓库或父镜像依赖规则保护，会出现在全局 `protected` 中；**只有最终 `candidates` 才是待清理清单**。本策略改变了保护范围，旧策略生成的预览无法执行，必须重新生成。

## 生成预览

在 Guardian 容器或具备 Docker CLI/Compose 插件的 Linux 环境运行：

```sh
python -m compose_guardian.image_cleanup \
  --compose-root /compose/projects \
  --state-dir /reports \
  --plan /reports/image-cleanup-plan.json
```

`--compose-root` 可重复传入，或使用 `COMPOSE_ROOT`。扫描根目录自身和一级子目录，支持四种标准 Compose 文件名。发现多份基础文件或多份默认 override 时中止，需用 `--manifest` 明确文件组合。默认 override 文件会一起解析。

也可以传入与新更新核心相同结构的 manifest：

```json
{
  "projects": [
    {
      "name": "example",
      "directory": "/srv/example",
      "files": ["/srv/example/compose.yaml", "/srv/example/production.yaml"]
    }
  ]
}
```

清理不更新服务，因此无需 `services` 或 `independent` 字段。请明确列出所有应保护的当前项目和文件组合；仅容器标签中能够访问的额外文件组合会自动纳入。根目录之外、不可访问的历史标签路径不能代表当前完整 Compose 清单，但其容器的实际镜像及名称引用仍被保护。选定范围内的文件缺失、解析失败、恢复状态损坏或范围为空时中止。

若容器标签记录的是宿主机路径，而维护环境看到不同路径，必须显式声明映射，例如：

```sh
... --path-map /srv/compose=/compose/projects
```

不会隐式猜测路径。若某个前缀的镜像需要全部保留，可显式传入 `--keep-prefix example.internal/team/`；该选项是永久保留规则，不实行历史数量限制。传入 `--keep-prefix local/` 可恢复全部保留本地版本的行为；不传时使用上述每仓库额外保留 2 个的策略。

## 明确执行

检查预览后，以相同范围和 Docker endpoint 再次运行：

```sh
python -m compose_guardian.image_cleanup \
  --compose-root /compose/projects \
  --state-dir /reports \
  --plan /reports/image-cleanup-plan.json \
  --apply
```

默认不强制删除，多标签/多摘要镜像会跳过。若已确认这些别名均不需要，并且停止了其他镜像更新、拉取、构建和打标签任务，可以额外使用 `--allow-multi-tag`。此选项仅对预览中获准的不可变镜像 ID 使用 `--force`，解决旧镜像挂有多个备份别名的冲突；仍不会放宽任何保留规则。**不要在其他 Docker 写入者并发运行时使用该选项。**

执行前和每次删除前重新采集引用关系、Docker 身份、Compose 配置哈希和恢复状态。预览过期、范围变更或任何意外变动均停止后续删除。按子镜像先于父镜像的顺序删除，并始终使用 `--no-prune`，防止 Docker 隐式删除不在清单中的父镜像。不会调用 `system prune`、`container prune` 或 `volume prune`。

结果逐项写入 `image-cleanup-plan.result.json`，包括已尝试、已删除、已跳过的 ID 和错误状态。退出码：`0` 完成（可能有保守跳过）、`1` 执行失败/部分完成、`2` 无法生成或读取有效预览。超时后 Docker 可能仍在处理已经接受的删除；应重新检查实际状态，不能直接重试旧计划。

## 并发与部署边界

清理和旧更新入口使用 `GUARDIAN_STATE_DIR`（默认 `REPORT_DIR`，再默认 `/reports`）中的 `guardian.lock`。新更新核心应把 `--state-dir` 指向相同目录。只有使用相同锁目录的 Guardian 进程才能互斥；这不是 Docker 全局事务锁，不能阻止外部工具在最后一次检查与 Docker 删除之间发起写入。执行清理时应暂停其他更新器，尤其是多别名强制删除模式。

命令使用已有 Docker daemon，遵循 `DOCKER_HOST` 等 Docker 环境配置。它不会启动 Unraid 阵列或 Docker daemon，也不会直接编辑离线 `docker.img` 的 layerdb/subvolume。离线存储维护必须另行准备能够保证业务容器不会自动运行的维护环境；不能直接为清理而启动普通生产 daemon。

镜像的 `size` 含共享层，不能累加为实际可回收空间。清理后 Docker 内部空闲空间与宿主机稀疏镜像文件占用也可能不同，取决于底层 discard/TRIM 支持。

## 验证

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

单元测试覆盖停止中容器、Compose profiles/override/变量解析边界、容器标签中的相对配置路径、镜像摘要别名、回滚状态、父镜像保护、预览失效、并发引用变化、部分执行记录和 Docker 错误。本地历史策略另覆盖每仓库名额、引用不占名额、多标签去重、跨仓库共享 ID、父镜像例外、创建时间时区/纳秒精度及过期预览拒绝。`test_image_cleanup.py` 可在 Windows 单独运行，其中依赖 Linux `flock` 的更新器互斥用例会跳过；上面的完整套件依赖 `fcntl`，需在 Linux 中运行。

基础清理流程另曾在独立数据目录的真实 Docker daemon 中验证预览、默认跳过多标签镜像和显式清理多标签旧镜像；停止中的容器、Compose 镜像、保留的本地镜像及数据卷校验值均保持不变，过程中没有运行任何容器。本地历史策略的单元测试使用模拟 Docker 清单，不连接 NAS 或执行真实删除。
