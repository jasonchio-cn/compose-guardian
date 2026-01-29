import json
import logging
import os
import subprocess
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .reporting import Report, write_report

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


COMPOSE_FILENAMES = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
]


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run(cmd: List[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _stack_name(compose_file: str) -> str:
    # Default stack label: use the parent directory name
    return os.path.basename(os.path.dirname(compose_file.rstrip("/\\"))) or compose_file


def _compose_base(compose_file: str) -> List[str]:
    base = ["docker", "compose", "--project-directory", os.path.dirname(compose_file), "-f", compose_file]
    return base


def _compose(compose_file: str, cmd: List[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return _run(_compose_base(compose_file) + cmd, check=check)


def _docker(cmd: List[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return _run(["docker"] + cmd, check=check)


def _image_id(image: str) -> str:
    p = _docker(["image", "inspect", "-f", "{{.Id}}", image], check=False)
    out = (p.stdout or "").strip()
    return out


def _backup_tag(image: str, ts_compact: str) -> str:
    # Produce an ASCII-only tag. Keep it deterministic and unique enough for one run.
    # Example: repo:tag__backup__20260124T030000
    return f"{image}__backup__{ts_compact}"


def _ignore_set() -> set:
    raw = os.getenv("IGNORE_SERVICES", "").strip()
    if not raw:
        return set()
    parts = [p.strip() for p in raw.split(",")]
    return {p for p in parts if p}


def _discover_compose_files(root: str) -> List[str]:
    root = (root or "").strip()
    if not root or not os.path.isdir(root):
        return []

    out: List[str] = []

    # Support root itself containing a compose file.
    for name in COMPOSE_FILENAMES:
        p = os.path.join(root, name)
        if os.path.isfile(p):
            out.append(p)
            break

    # Scan one level down: /root/*/docker-compose.yml (or compose.yml)
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except FileNotFoundError:
        return []

    for ent in entries:
        if not ent.is_dir():
            continue
        for name in COMPOSE_FILENAMES:
            p = os.path.join(ent.path, name)
            if os.path.isfile(p):
                out.append(p)
                break

    return out


def _stack_is_up(compose_file: str) -> bool:
    # Define "up" as: at least one running container in this stack.
    # If nothing is running, skip to avoid bringing up stacks that were never started.
    p = _compose(compose_file, ["ps", "-q", "--status", "running"], check=False)
    return bool((p.stdout or "").strip())


def _get_services_images(compose_file: str) -> Dict[str, str]:
    cfg = _compose(compose_file, ["config", "--format", "json"], check=True).stdout
    data = json.loads(cfg)
    services = data.get("services", {})
    out: Dict[str, str] = {}
    for name, svc in services.items():
        img = (svc or {}).get("image")
        if img:
            out[name] = img
    return out


def _service_container_ids(compose_file: str, services: List[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for svc in services:
        p = _compose(compose_file, ["ps", "-q", svc], check=False)
        ids = [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
        out[svc] = ids
    return out


def _inspect_container(cid: str) -> dict:
    p = _docker(["inspect", cid])
    arr = json.loads(p.stdout)
    return arr[0]


def _container_health_info(ins: dict) -> Tuple[str, Optional[str], int]:
    state = (ins.get("State") or {})
    status = state.get("Status") or ""
    health = None
    if state.get("Health") and isinstance(state.get("Health"), dict):
        health = state["Health"].get("Status")
    restart_count = state.get("RestartCount")
    if restart_count is None:
        restart_count = 0
    return status, health, int(restart_count)


def _verify_services(compose_file: str, services: List[str]) -> Tuple[bool, str]:
    timeout = int(os.getenv("HEALTH_TIMEOUT_SECONDS", "180"))
    stable_seconds = int(os.getenv("STABLE_SECONDS", "30"))
    poll = int(os.getenv("VERIFY_POLL_SECONDS", "3"))

    start = time.time()

    # For no-healthcheck containers we require RestartCount to remain stable for stable_seconds.
    stable_since: Dict[str, float] = {}
    restart_baseline: Dict[str, int] = {}

    while time.time() - start <= timeout:
        svc_cids = _service_container_ids(compose_file, services)
        all_ok = True
        reason = ""

        for svc, cids in svc_cids.items():
            if not cids:
                all_ok = False
                reason = f"service {svc} has no containers"
                continue

            for cid in cids:
                ins = _inspect_container(cid)
                status, health, restarts = _container_health_info(ins)
                key = f"{svc}:{cid}"

                if status != "running":
                    all_ok = False
                    reason = f"container not running: {key} status={status}"
                    continue

                if health is not None:
                    if health != "healthy":
                        all_ok = False
                        reason = f"container not healthy: {key} health={health}"
                        continue
                else:
                    if key not in restart_baseline:
                        restart_baseline[key] = restarts
                        stable_since[key] = time.time()
                    else:
                        if restarts != restart_baseline[key]:
                            restart_baseline[key] = restarts
                            stable_since[key] = time.time()

                    if time.time() - stable_since[key] < stable_seconds:
                        all_ok = False
                        reason = f"container not yet stable: {key} restarts={restarts}"
                        continue

        if all_ok:
            return True, "ok"

        time.sleep(poll)

    return False, f"verify timeout after {timeout}s"


def _dingtalk_send(title: str, text: str) -> None:
    webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    if not webhook:
        return

    # Minimal webhook send (no secret signing).
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
    }

    try:
        import requests

        requests.post(webhook, json=payload, timeout=10)
    except Exception:
        # Notification failures should not crash the updater.
        return


def _run_once_for_compose(compose_file: str) -> Report:
    ignore = _ignore_set()
    ts_compact = datetime.now().strftime("%Y%m%dT%H%M%S")
    stack = _stack_name(compose_file)

    logger.info(f"开始处理 compose 文件: {compose_file}")
    logger.info(f"堆栈名称: {stack}")
    if ignore:
        logger.info(f"忽略的服务: {', '.join(sorted(ignore))}")

    report = Report(
        timestamp=ts_compact,
        compose_file=compose_file,
        ignored_services=sorted(ignore),
    )

    try:
        if not _stack_is_up(compose_file):
            report.status = "SKIPPED"
            report.message = "stack not up (no running containers)"
            logger.info(f"跳过 {stack}: 堆栈未启动（没有运行中的容器）")
            write_report(report)
            return report

        services_images = _get_services_images(compose_file)
        for svc in list(services_images.keys()):
            if svc in ignore:
                services_images.pop(svc, None)

        report.services = {svc: {"image": img} for svc, img in services_images.items()}
        logger.info(f"发现服务: {', '.join(services_images.keys())}")

        if not services_images:
            report.status = "SKIPPED"
            report.message = "no services with image after applying ignore list"
            logger.info(f"跳过 {stack}: 应用忽略列表后没有带镜像的服务")
            write_report(report)
            return report

        before_ids: Dict[str, str] = {svc: _image_id(img) for svc, img in services_images.items()}
        report.before_image_ids = before_ids

        logger.info(f"正在拉取最新镜像...")
        _compose(compose_file, ["pull"], check=False)

        after_ids: Dict[str, str] = {svc: _image_id(img) for svc, img in services_images.items()}
        report.after_image_ids = after_ids

        changed: List[str] = []
        skipped_no_id: List[str] = []
        for svc in services_images.keys():
            b = (before_ids.get(svc, "") or "").strip()
            a = (after_ids.get(svc, "") or "").strip()
            if not b or not a:
                skipped_no_id.append(svc)
                continue
            if b != a:
                changed.append(svc)

        report.changed_services = changed

        if not changed:
            report.status = "SKIPPED"
            if skipped_no_id:
                report.message = "no image updates detected (some services missing image id: %s)" % ",".join(
                    skipped_no_id
                )
                logger.info(f"跳过 {stack}: 未检测到镜像更新（某些服务缺少镜像ID: {', '.join(skipped_no_id)}）")
            else:
                report.message = "no image updates detected"
                logger.info(f"跳过 {stack}: 未检测到镜像更新")
            write_report(report)
            return report

        # Backup old images for changed services.
        logger.info(f"检测到 {len(changed)} 个服务需要更新: {', '.join(changed)}")
        backups: Dict[str, str] = {}
        for svc in changed:
            img = services_images[svc]
            old_id = before_ids.get(svc, "")
            if not old_id:
                continue
            btag = _backup_tag(img, ts_compact)
            _docker(["image", "tag", old_id, btag], check=False)
            backups[svc] = btag

        report.backup_tags = backups

        # Apply update for changed services only.
        logger.info(f"正在更新服务: {', '.join(changed)}")
        _compose(compose_file, ["up", "-d", "--force-recreate", "--no-deps"] + changed, check=False)

        logger.info(f"正在验证服务健康状态...")
        ok, why = _verify_services(compose_file, changed)
        report.verify_ok = ok
        report.verify_message = why

        if not ok:
            logger.warning(f"服务验证失败，开始回滚: {why}")
            report.status = "ROLLING_BACK"
            # Roll back only changed services.
            for svc in changed:
                img = services_images[svc]
                btag = backups.get(svc)
                if not btag:
                    continue
                bid = _image_id(btag)
                if bid:
                    _docker(["image", "tag", bid, img], check=False)

            logger.info(f"正在回滚服务: {', '.join(changed)}")
            _compose(compose_file, ["up", "-d", "--force-recreate", "--no-deps"] + changed, check=False)
            rok, rwhy = _verify_services(compose_file, changed)
            report.rollback_verify_ok = rok
            report.rollback_verify_message = rwhy
            report.status = "ROLLBACK" if rok else "FAILED"

            write_report(report)
            logger.info(f"更新流程完成: {report.status}")
            return report

        # Success: cleanup backup tags and old images.
        logger.info(f"更新成功，正在清理备份镜像...")
        for svc in changed:
            btag = backups.get(svc)
            if btag:
                _docker(["image", "rm", btag], check=False)

            old_id = before_ids.get(svc, "")
            if old_id:
                # Only delete old image if no containers reference it.
                ps = _docker(["ps", "-a", "--filter", f"ancestor={old_id}", "-q"], check=False)
                if not (ps.stdout or "").strip():
                    _docker(["image", "rm", old_id], check=False)

        report.status = "SUCCESS"
        write_report(report)
        logger.info(f"更新流程完成: SUCCESS")
        return report

    except Exception as e:
        report.status = "FAILED"
        report.message = f"exception: {type(e).__name__}: {e}"
        write_report(report)
        return report


def run_once() -> None:
    root = os.getenv("COMPOSE_ROOT", "/compose/projects").strip() or "/compose/projects"
    logger.info(f"开始扫描 compose 文件，根目录: {root}")
    compose_files = _discover_compose_files(root)

    reports: List[Report] = []

    if not compose_files:
        logger.warning(f"未找到任何 compose 文件，COMPOSE_ROOT={root}")
        ts_compact = datetime.now().strftime("%Y%m%dT%H%M%S")
        report = Report(
            timestamp=ts_compact,
            compose_file=root,
            ignored_services=sorted(_ignore_set()),
            status="SKIPPED",
            message=f"no compose files found under COMPOSE_ROOT={root}",
        )
        write_report(report)
        reports.append(report)
    else:
        logger.info(f"发现 {len(compose_files)} 个 compose 文件: {[os.path.basename(f) for f in compose_files]}")
        for compose_file in compose_files:
            reports.append(_run_once_for_compose(compose_file))

    logger.info("所有 compose 文件处理完成")

    # Send a single summary notification per run.
    _dingtalk_send(_summary_title(reports), _format_dingtalk_summary(reports))


def _format_dingtalk(report: Report) -> str:
    # Markdown
    lines = []
    lines.append(f"### {report.status}")
    lines.append("")
    lines.append(f"- compose: `{report.compose_file}`")
    if report.changed_services:
        lines.append(f"- changed: {', '.join(report.changed_services)}")
    if report.message:
        lines.append(f"- message: {report.message}")
    if report.verify_message:
        lines.append(f"- verify: {report.verify_ok} ({report.verify_message})")
    if report.rollback_verify_message:
        lines.append(f"- rollback_verify: {report.rollback_verify_ok} ({report.rollback_verify_message})")
    lines.append("")
    # Include a compact service diff
    if report.changed_services:
        lines.append("#### Image IDs")
        for svc in report.changed_services:
            b = (report.before_image_ids or {}).get(svc, "")
            a = (report.after_image_ids or {}).get(svc, "")
            if b and a and b != a:
                lines.append(f"- {svc}: {b} -> {a}")
    return "\n".join(lines)


def _summary_title(reports: List[Report]) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ok = sum(1 for r in reports if r.status == "SUCCESS")
    rollback = sum(1 for r in reports if r.status == "ROLLBACK")
    failed = sum(1 for r in reports if r.status == "FAILED")
    skipped = sum(1 for r in reports if r.status == "SKIPPED")

    total = len(reports)
    if failed:
        overall = "FAILED"
    elif rollback:
        overall = "ROLLBACK"
    elif ok:
        overall = "SUCCESS"
    else:
        overall = "SKIPPED"

    return f"Compose Guardian Run {overall} ({ts}) total={total} ok={ok} rollback={rollback} failed={failed} skipped={skipped}"


def _format_dingtalk_summary(reports: List[Report]) -> str:
    # One markdown message for the whole run.
    lines: List[str] = []

    ok = [r for r in reports if r.status == "SUCCESS"]
    rb = [r for r in reports if r.status == "ROLLBACK"]
    failed = [r for r in reports if r.status == "FAILED"]
    skipped = [r for r in reports if r.status == "SKIPPED"]

    if failed:
        overall = "FAILED"
    elif rb:
        overall = "ROLLBACK"
    elif ok:
        overall = "SUCCESS"
    else:
        overall = "SKIPPED"

    lines.append(f"### Run Summary: {overall}")
    lines.append("")
    lines.append(
        "- totals: ok=%d, rollback=%d, failed=%d, skipped=%d" % (len(ok), len(rb), len(failed), len(skipped))
    )
    lines.append("")

    # Per-stack compact section.
    for r in reports:
        stack = _stack_name(r.compose_file)
        changed = ", ".join(r.changed_services) if r.changed_services else "-"
        lines.append(f"#### {stack}: {r.status}")
        lines.append(f"- compose: `{r.compose_file}`")
        lines.append(f"- changed: {changed}")
        if r.message:
            lines.append(f"- message: {r.message}")
        if r.verify_message:
            lines.append(f"- verify: {r.verify_ok} ({r.verify_message})")
        if r.rollback_verify_message:
            lines.append(f"- rollback_verify: {r.rollback_verify_ok} ({r.rollback_verify_message})")
        lines.append("")

    return "\n".join(lines)
