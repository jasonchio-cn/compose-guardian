import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Report:
    timestamp: str
    compose_file: str

    status: str = ""
    message: str = ""

    ignored_services: List[str] = field(default_factory=list)

    services: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    before_image_ids: Dict[str, str] = field(default_factory=dict)
    after_image_ids: Dict[str, str] = field(default_factory=dict)
    changed_services: List[str] = field(default_factory=list)

    backup_tags: Dict[str, str] = field(default_factory=dict)

    verify_ok: Optional[bool] = None
    verify_message: str = ""

    rollback_verify_ok: Optional[bool] = None
    rollback_verify_message: str = ""


def write_report(report: Report) -> str:
    report_dir = "/reports"
    os.makedirs(report_dir, exist_ok=True)

    name = f"{report.timestamp}_{report.status.lower() or 'unknown'}.json"
    path = os.path.join(report_dir, name)

    data = {
        "timestamp": report.timestamp,
        "compose_file": report.compose_file,
        "status": report.status,
        "message": report.message,
        "ignored_services": report.ignored_services,
        "services": report.services,
        "before_image_ids": report.before_image_ids,
        "after_image_ids": report.after_image_ids,
        "changed_services": report.changed_services,
        "backup_tags": report.backup_tags,
        "verify_ok": report.verify_ok,
        "verify_message": report.verify_message,
        "rollback_verify_ok": report.rollback_verify_ok,
        "rollback_verify_message": report.rollback_verify_message,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)

    # Also update latest.json for easy access
    latest = os.path.join(report_dir, "latest.json")
    with open(latest, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)

    return path
