"""Opt-in isolated entrypoint; legacy main and notifications remain untouched."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .core import Guardian, Project, next_daily


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--execute', action='store_true', help='explicit container mutation opt-in')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    projects = [Project(p['name'], p['directory'], tuple(p['files']), tuple(p['services']), p.get('independent', False)) for p in manifest['projects']]
    guardian = Guardian(args.state_dir, execute=args.execute)
    if args.once:
        return guardian.run_round(projects)[0]
    while True:
        delay = next_daily(datetime.now(timezone.utc)).timestamp() - time.time()
        time.sleep(max(0, delay))
        guardian.run_round(projects)


if __name__ == '__main__':
    raise SystemExit(main())
