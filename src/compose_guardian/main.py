import logging
import os
import time
from datetime import datetime, timezone

from croniter import croniter

from .updater import run_once

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _parse_every(value: str) -> int:
    v = value.strip().lower()
    if v.endswith("s"):
        return int(v[:-1])
    if v.endswith("m"):
        return int(v[:-1]) * 60
    if v.endswith("h"):
        return int(v[:-1]) * 3600
    raise ValueError(f"Unsupported SCHEDULE_EVERY: {value!r} (use s/m/h)")


def _sleep_until(ts: float) -> None:
    while True:
        now = time.time()
        if now >= ts:
            return
        time.sleep(min(1.0, ts - now))


def main() -> None:
    schedule_cron = os.getenv("SCHEDULE_CRON", "").strip()
    schedule_every = os.getenv("SCHEDULE_EVERY", "").strip()

    logger.info("Compose Guardian 启动")
    
    if not schedule_cron and not schedule_every:
        # No schedule configured: run once and exit.
        logger.info("未配置调度参数，执行一次后退出")
        run_once()
        logger.info("执行完成，退出程序")
        return

    if schedule_cron:
        base = datetime.now(timezone.utc)
        itr = croniter(schedule_cron, base)
        while True:
            nxt = itr.get_next(datetime)
            _sleep_until(nxt.timestamp())
            run_once()
        
    interval = _parse_every(schedule_every)
    # interval schedule runs immediately then repeats
    while True:
        run_once()
        time.sleep(interval)


if __name__ == "__main__":
    main()
