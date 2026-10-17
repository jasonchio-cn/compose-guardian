# 保守镜像清理

新增独立入口 `compose_guardian.image_cleanup`。默认只生成预览，不挂接更新调度、不拉取或构建镜像，不启动/停止/删除容器，也不清理数据卷、网络或构建缓存。

## 保留规则

- 所有容器实际使用的镜像 ID，包括停止中的容器；同时保护容器原始镜像名称当前指向的缓存镜像。
- 选定 Compose 根目录和显式 manifest 中引用的本地镜像。通过 Compose 自身解析变量、所有 profiles、include/extends 及文件组合，不自行解析 YAML。
- 默认保留所有带 `local/*` 标签的镜像；一个镜像有多个标签时，只要任一标签属于保留范围，就保留整个镜像。
- Guardian 状态目录中 current、history、pending 引用的镜像及其标签。其保留版本数仍由更新核心管理，本命令不越过回滚记录。
- 保留上述镜像的父镜像，避免清理本地构建的依赖链。

其他镜像列为候选。预览包含完整不可变 ID、标签、摘要、保护原因、缺失的缓存引用以及当前环境指纹。不存在于本地的 Compose 镜像只列为缺失，不尝试下载。

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

不会隐式猜测路径。自建镜像使用其他前缀时，可额外传入 `--keep-prefix example.internal/team/`；默认 `local/` 规则仍保留。

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

新增 28 项用例覆盖停止中容器、Compose profiles/override/变量解析边界、容器标签中的相对配置路径、镜像摘要别名、本地多标签、回滚状态、父镜像保护、预览失效、并发引用变化、部分执行记录和 Docker 错误。连同原更新核心用例，53 项测试已在 Python 3.12/Linux 环境通过。

另外在独立数据目录的真实 Docker daemon 中验证了预览、默认跳过多标签镜像和显式清理多标签旧镜像；停止中的容器、Compose 镜像、`local/` 镜像及数据卷校验值均保持不变，过程中没有运行任何容器。
