import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .reporting import Report, write_report


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


def _compose_base() -> List[str]:
    compose_file = os.getenv("COMPOSE_FILE", "/compose/docker-compose.yml")
    project = os.getenv("COMPOSE_PROJECT_NAME", "").strip()
    base = ["docker", "compose"]
    if project:
        base += ["-p", project]
    base += ["-f", compose_file]
    return base


def _compose(cmd: List[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return _run(_compose_base() + cmd, check=check)


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


def _get_services_images() -> Dict[str, str]:
    cfg = _compose(["config", "--format", "json"]).stdout
    data = json.loads(cfg)
    services = data.get("services", {})
    out: Dict[str, str] = {}
    for name, svc in services.items():
        img = (svc or {}).get("image")
        if img:
            out[name] = img
    return out


def _service_container_ids(services: List[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for svc in services:
        p = _compose(["ps", "-q", svc], check=False)
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


def _verify_services(services: List[str]) -> Tuple[bool, str]:
    timeout = int(os.getenv("HEALTH_TIMEOUT_SECONDS", "180"))
    stable_seconds = int(os.getenv("STABLE_SECONDS", "30"))
    poll = int(os.getenv("VERIFY_POLL_SECONDS", "3"))

    start = time.time()

    # For no-healthcheck containers we require RestartCount to remain stable for stable_seconds.
    stable_since: Dict[str, float] = {}
    restart_baseline: Dict[str, int] = {}

    while time.time() - start <= timeout:
        svc_cids = _service_container_ids(services)
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


def run_once() -> None:
    compose_file = os.getenv("COMPOSE_FILE", "/compose/docker-compose.yml")
    ignore = _ignore_set()
    ts_compact = datetime.now().strftime("%Y%m%dT%H%M%S")

    report = Report(
        timestamp=ts_compact,
        compose_file=compose_file,
        ignored_services=sorted(ignore),
    )

    try:
        services_images = _get_services_images()
        for svc in list(services_images.keys()):
            if svc in ignore:
                services_images.pop(svc, None)

        report.services = {svc: {"image": img} for svc, img in services_images.items()}

        if not services_images:
            report.status = "SKIPPED"
            report.message = "no services with image after applying ignore list"
            write_report(report)
            return

        before_ids: Dict[str, str] = {svc: _image_id(img) for svc, img in services_images.items()}
        report.before_image_ids = before_ids

        _compose(["pull"], check=False)

        after_ids: Dict[str, str] = {svc: _image_id(img) for svc, img in services_images.items()}
        report.after_image_ids = after_ids

        changed = [svc for svc in services_images.keys() if before_ids.get(svc, "") != after_ids.get(svc, "")]
        report.changed_services = changed

        if not changed:
            report.status = "SKIPPED"
            report.message = "no image updates detected"
            write_report(report)
            return

        # Backup old images for changed services.
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
        _compose(["up", "-d", "--force-recreate", "--no-deps"] + changed, check=False)

        ok, why = _verify_services(changed)
        report.verify_ok = ok
        report.verify_message = why

        if not ok:
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

            _compose(["up", "-d", "--force-recreate", "--no-deps"] + changed, check=False)
            rok, rwhy = _verify_services(changed)
            report.rollback_verify_ok = rok
            report.rollback_verify_message = rwhy
            report.status = "ROLLBACK" if rok else "FAILED"

            write_report(report)

            title = f"Compose Update {report.status}"
            text = _format_dingtalk(report)
            _dingtalk_send(title, text)
            return

        # Success: cleanup backup tags and old images.
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

        title = "Compose Update SUCCESS"
        text = _format_dingtalk(report)
        _dingtalk_send(title, text)

    except Exception as e:
        report.status = "FAILED"
        report.message = f"exception: {type(e).__name__}: {e}"
        write_report(report)

        title = "Compose Update FAILED"
        text = _format_dingtalk(report)
        _dingtalk_send(title, text)


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
