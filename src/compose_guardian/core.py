"""Isolated, fail-closed Guardian core. No channel-specific dependencies."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def redact(value):
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if re.search(r'password|secret|token|authorization|api.?key', k, re.I) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    value = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', value)
    value = re.sub(r'(?i)((?:password|token|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', value)
    value = re.sub(r'https?://[^\s]+', '[URL REDACTED]', value)
    for key, secret in os.environ.items():
        if re.search(r'password|secret|token|api.?key', key, re.I) and len(secret) >= 4:
            value = value.replace(secret, '[REDACTED]')
    return value


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.guardian-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(redact(data), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextlib.contextmanager
def round_lock(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class CommandError(RuntimeError):
    pass


class Runner:
    def __init__(self, timeout=120):
        self.timeout = timeout
        self.events = []

    def __call__(self, argv):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, check=False)
            event = {'command': argv, 'returncode': result.returncode, 'stdout': result.stdout[-8192:], 'stderr': result.stderr[-8192:]}
        except subprocess.TimeoutExpired as exc:
            event = {'command': argv, 'timeout': self.timeout, 'stderr': str(exc)}
            self.events.append(redact(event))
            raise CommandError('command timeout') from None
        self.events.append(redact(event))
        if result.returncode:
            raise CommandError(json.dumps(redact(event)))
        return result.stdout.strip()


@dataclass(frozen=True)
class Project:
    name: str
    directory: str
    files: tuple
    services: tuple
    independent: bool = False

    def validate(self, *, require_independent=True):
        if not self.name or not self.services or not self.files:
            raise ValueError('explicit project identity/files/services required')
        if not os.path.isabs(self.directory) or any(not os.path.isabs(p) for p in self.files):
            raise ValueError('absolute original host paths required')
        if require_independent and not self.independent:
            raise ValueError('service association boundary unconfirmed; project paused')
        if any(not Path(p).is_file() for p in self.files) or not Path(self.directory).is_dir():
            raise ValueError('original host paths not accessible; do not remap implicitly')

    def command(self, *args, override=None):
        cmd = ['docker', 'compose', '-p', self.name, '--project-directory', self.directory]
        for file in self.files:
            cmd += ['-f', file]
        if override:
            cmd += ['-f', str(override)]
        return cmd + list(args)


class UnavailableAnalyzer:
    def assess(self, context):
        return {'decision': 'unavailable', 'reason': 'Pi analyzer not connected'}


class LocalAnalyzer:
    """Test-only injectable decision; not a substitute for real changelog analysis."""
    def __init__(self, decision='review'):
        self.decision = decision

    def assess(self, context):
        return {'decision': self.decision, 'reason': 'local test fixture'}


class Approvals:
    """In-memory intentionally: restart invalidates approvals. No TTL."""
    def __init__(self, actor=None, notify=None):
        self.actor, self.notify = actor, notify
        self.round = None
        self.requests = {}

    def begin(self, round_id):
        self.round, self.requests = round_id, {}

    def request(self, key, target):
        for ident, request in self.requests.items():
            if request['key'] == key and request['target'] == target:
                return ident
        ident = uuid.uuid4().hex
        self.requests[ident] = {'round': self.round, 'key': key, 'target': target, 'state': 'pending'}
        if self.notify:
            self.notify(ident, dict(self.requests[ident]))
        return ident

    def reply(self, ident, actor, target, approve):
        request = self.requests.get(ident)
        if not self.actor or actor != self.actor or not request or request['round'] != self.round or target != request['target'] or request['state'] != 'pending':
            return False
        request['state'] = 'approved' if approve else 'rejected'
        return True

    def allowed(self, ident):
        return bool(self.notify and self.actor and self.requests.get(ident, {}).get('state') == 'approved')


def next_daily(now):
    local = now.astimezone(ZoneInfo('Asia/Shanghai'))
    nxt = local.replace(hour=10, minute=0, second=0, microsecond=0)
    return nxt if nxt > local else nxt + timedelta(days=1)


class Guardian:
    def __init__(self, root, runner=None, analyzer=None, approvals=None, execute=False, health_timeout=180, stable_seconds=30, poll=3):
        self.root = Path(root)
        self.run = runner or Runner()
        self.analyzer = analyzer or UnavailableAnalyzer()
        self.approvals = approvals or Approvals()
        self.execute = execute
        self.health_timeout, self.stable_seconds, self.poll = health_timeout, stable_seconds, poll
        self.round = None

    def image(self, ref):
        return self.run(['docker', 'image', 'inspect', '-f', '{{.Id}}', ref])

    def containers(self, project, service):
        ids = self.run(project.command('ps', '--all', '-q', service)).split()
        return [json.loads(self.run(['docker', 'inspect', cid]))[0] for cid in ids]

    def identity(self, project, service, containers):
        for container in containers:
            labels = container.get('Config', {}).get('Labels', {})
            recorded_files = tuple(labels.get('com.docker.compose.project.config_files', '').split(','))
            extras = recorded_files[len(project.files):]
            managed_extras = all(Path(p).is_file() and Path(p).resolve().is_relative_to(self.root.resolve()) and Path(p).name.endswith(('-target.json', '-rollback.json')) for p in extras)
            if (labels.get('com.docker.compose.project') != project.name or
                labels.get('com.docker.compose.service') != service or
                labels.get('com.docker.compose.project.working_dir') != project.directory or
                recorded_files[:len(project.files)] != project.files or not managed_extras):
                raise ValueError('container project identity/original config combination mismatch')

    def verify(self, project, service, expected=None):
        deadline = time.monotonic() + self.health_timeout
        baseline = None
        since = time.monotonic()
        while True:
            containers = self.containers(project, service)
            signature = tuple(sorted((c['Id'], c.get('RestartCount', 0)) for c in containers))
            if signature != baseline:
                baseline, since = signature, time.monotonic()
            image_ok = bool(containers) and (expected is None or all(c['Image'] == expected for c in containers))
            running = bool(containers) and all(c['State'].get('Status') == 'running' for c in containers)
            if not running:
                since = time.monotonic()
            health_ok = running and all(c['State'].get('Health', {}).get('Status', 'healthy') == 'healthy' for c in containers)
            has_health = bool(containers) and all('Health' in c['State'] for c in containers)
            stable = time.monotonic() - since >= self.stable_seconds
            result = {'image_ok': image_ok, 'health_ok': health_ok and (has_health or stable), 'business_health_verified': has_health, 'containers': [{'id': c['Id'], 'image': c['Image'], 'state': c['State'].get('Status'), 'health': c['State'].get('Health', {}).get('Status'), 'restarts': c.get('RestartCount', 0)} for c in containers]}
            if image_ok and result['health_ok'] or time.monotonic() >= deadline:
                return result
            time.sleep(self.poll)

    def override(self, project, service, image, phase):
        path = self.root / self.round / (hashlib.sha256(f'{project.name}/{service}'.encode()).hexdigest()[:16] + f'-{phase}.json')
        atomic_json(path, {'services': {service: {'image': image, 'pull_policy': 'never'}}})
        return path

    def state_path(self, key):
        return self.root / 'state' / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    def cleanup(self, state):
        # Protect references from ALL containers, including stopped ones; never image-rm IDs.
        ids = self.run(['docker', 'ps', '-aq']).split()
        protected = {json.loads(self.run(['docker', 'inspect', cid]))[0]['Image'] for cid in ids}
        for state_file in (self.root / 'state').glob('*.json'):
            other = json.loads(state_file.read_text())
            if other.get('pending'):
                protected.add(other['pending']['old'])
                protected.add(other['pending']['target'])
        if state.get('pending'):
            protected.add(state['pending']['old'])
            protected.add(state['pending']['target'])
        current = state.get('current')
        versions = []
        for entry in reversed(state.get('history', [])):
            if entry['id'] != current and entry['id'] not in versions:
                versions.append(entry['id'])
        keep = set(versions[:2]) | protected
        retained = []
        for entry in state.get('history', []):
            if entry['id'] in keep:
                retained.append(entry)
            else:
                if not entry['tag'].startswith('compose-guardian-backup:') or self.image(entry['tag']) != entry['id']:
                    raise ValueError('managed backup ownership mismatch')
                self.run(['docker', 'image', 'rm', entry['tag']])
        state['history'] = retained

    def recover(self, project, service, state, report):
        pending = state['pending']
        report['stages'].append({'phase': 'recovery', 'old': pending['old'], 'target': pending['target']})
        override = self.override(project, service, pending['tag'], 'rollback')
        pending['override'] = str(override)
        atomic_json(self.state_path(f'{project.name}/{service}'), state)
        if self.image(pending['tag']) != pending['old']:
            raise ValueError('backup image no longer matches recovery baseline')
        command_ok = True
        try:
            self.run(project.command('up', '-d', '--force-recreate', '--no-deps', '--pull', 'never', '--no-build', service, override=override))
        except Exception as exc:
            command_ok = False
            report['stages'].append({'phase': 'rollback_command_failed', 'error': redact(str(exc))})
        verification = self.verify(project, service, pending['old'])
        report['rollback'] = dict(verification, command_ok=command_ok)
        report['status'] = 'RECOVERED' if verification['image_ok'] and verification['health_ok'] else 'RECOVERY_FAILED'
        if report['status'] == 'RECOVERED':
            state['current'] = pending['old']
            state['pending'] = None
        atomic_json(self.state_path(f'{project.name}/{service}'), state)

    def service(self, project, service, config):
        report = {'round': self.round, 'project': project.name, 'service': service, 'status': 'FAILED', 'stages': []}
        key = f'{project.name}/{service}'
        path = self.state_path(key)
        state = {'history': [], 'pending': None}
        mutated = False
        start_events = len(getattr(self.run, 'events', []))
        try:
            if path.exists():
                state = json.loads(path.read_text())
            containers = self.containers(project, service)
            self.identity(project, service, containers)
            if state.get('pending'):
                if not self.execute:
                    report['status'] = 'RECOVERY_PENDING'
                else:
                    self.recover(project, service, state, report)
                return report
            if not containers or any(c['State'].get('Status') != 'running' for c in containers):
                report['status'] = 'SKIPPED_STOPPED'
                return report
            old_ids = {c['Image'] for c in containers}
            if len(old_ids) != 1:
                raise ValueError('mixed replica images: explicit recovery required')
            old = old_ids.pop()
            pre = self.verify(project, service, old)
            report['stages'].append({'phase': 'precheck', **pre})
            if not pre['health_ok']:
                report['status'] = 'SKIPPED_UNHEALTHY'
                return report
            image = config.get('image')
            if not image or config.get('build'):
                raise ValueError('build-only/mixed build service unsupported')
            report['baseline'] = old
            if self.execute:
                state['current'] = old
                self.cleanup(state)
                atomic_json(path, state)
            report['stages'].append({'phase': 'pull'})
            self.run(['docker', 'pull', image])
            target = self.image(image)
            if not target.startswith('sha256:'):
                raise ValueError('invalid target image ID')
            report['target'] = target
            if old == target:
                report['status'] = 'NO_UPDATE'
                return report
            assessment = self.analyzer.assess({'project': project.name, 'service': service, 'old': old, 'target': target, 'image': image})
            report['assessment'] = assessment
            if assessment.get('decision') == 'unavailable':
                report['status'] = 'PAUSED_ANALYZER'
                return report
            if assessment.get('decision') != 'safe':
                approval = self.approvals.request(key, target)
                report['approval'] = approval
                if not self.approvals.allowed(approval):
                    report['status'] = 'PAUSED_APPROVAL'
                    return report
            if not self.execute:
                report['status'] = 'CHECK_ONLY'
                return report
            # Revalidate baseline after analysis/approval before touching containers.
            latest = self.containers(project, service)
            self.identity(project, service, latest)
            if not latest or any(c['Image'] != old for c in latest) or not self.verify(project, service, old)['health_ok']:
                raise ValueError('baseline changed during analysis')
            tag = 'compose-guardian-backup:' + hashlib.sha256(key.encode()).hexdigest()[:16] + '-' + uuid.uuid4().hex
            self.run(['docker', 'image', 'tag', old, tag])
            if self.image(tag) != old:
                raise ValueError('backup verification failed')
            report['stages'].append({'phase': 'backup', 'tag': tag, 'image': old})
            state['history'].append({'id': old, 'tag': tag})
            state['pending'] = {'old': old, 'target': target, 'tag': tag}
            atomic_json(path, state)
            override = self.override(project, service, target, 'target')
            report['stages'].append({'phase': 'update', 'target': target, 'override': str(override)})
            mutated = True
            self.run(project.command('up', '-d', '--force-recreate', '--no-deps', '--pull', 'never', '--no-build', service, override=override))
            verification = self.verify(project, service, target)
            report['stages'].append({'phase': 'verify', **verification})
            if not verification['image_ok'] or not verification['health_ok']:
                raise ValueError('target image/health verification failed')
            state['pending'], state['current'] = None, target
            atomic_json(path, state)
            mutated = False
            report['status'] = 'SUCCESS'
            self.cleanup(state)
            atomic_json(path, state)
        except Exception as exc:
            report['error'] = redact(str(exc))
            if mutated and state.get('pending'):
                try:
                    self.recover(project, service, state, report)
                except Exception as recovery_exc:
                    report['status'] = 'RECOVERY_FAILED'
                    report['recovery_error'] = redact(str(recovery_exc))
        finally:
            report['commands'] = getattr(self.run, 'events', [])[start_events:]
            atomic_json(self.root / self.round / (hashlib.sha256(key.encode()).hexdigest()[:16] + '-report.json'), report)
        return report

    def run_round(self, projects):
        with round_lock(self.root / 'guardian.lock'):
            self.round = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex
            self.approvals.begin(self.round)
            reports = []
            if not projects:
                reports.append({'status': 'PAUSED_PROJECT', 'error': 'explicit project manifest is empty'})
            for project in projects:
                try:
                    # Inspection/pull-only rounds do not mutate services and need
                    # no assertion that services can be independently updated.
                    project.validate(require_independent=self.execute)
                    config = json.loads(self.run(project.command('config', '--format', 'json')))
                    for service in project.services:
                        if service not in config.get('services', {}):
                            raise ValueError('service absent from original config')
                        reports.append(self.service(project, service, config['services'][service]))
                except Exception as exc:
                    reports.append({'project': project.name, 'status': 'PAUSED_PROJECT', 'error': redact(str(exc))})
            bad = {'FAILED', 'RECOVERY_FAILED', 'RECOVERED'}
            paused = any(r['status'].startswith('PAUSED') or r['status'] == 'RECOVERY_PENDING' for r in reports)
            code = 1 if any(r['status'] in bad for r in reports) else (2 if paused else 0)
            atomic_json(self.root / self.round / 'summary.json', {'round': self.round, 'reports': reports, 'exit_code': code})
            return code, reports
