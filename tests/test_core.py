import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from compose_guardian.core import (Guardian, Project, LocalAnalyzer, Approvals, Runner,
                                  CommandError, atomic_json, redact, round_lock, next_daily)

OLD = 'sha256:old'
NEW = 'sha256:new'


class DockerFake:
    def __init__(self, project):
        self.project = project
        self.actual = OLD
        self.images = {'app:latest': NEW}
        self.commands = []
        self.fail = None
        self.wrong = False
        self.running = True
        self.health = 'healthy'

    def __call__(self, cmd):
        self.commands.append(cmd)
        if self.fail and self.fail(cmd):
            raise CommandError('injected command failure token=secret123')
        if cmd[1] == 'pull':
            return ''
        if cmd[1:3] == ['image', 'inspect']:
            return self.images.get(cmd[-1], cmd[-1] if cmd[-1].startswith('sha256:') else '')
        if cmd[1:3] == ['image', 'tag']:
            self.images[cmd[-1]] = cmd[-2]
            return ''
        if cmd[1:3] == ['image', 'rm']:
            self.images.pop(cmd[-1], None)
            return ''
        if cmd[1] == 'inspect':
            p = self.project
            return json.dumps([{'Id': 'cid', 'Image': self.actual, 'RestartCount': 0,
                'Config': {'Labels': {'com.docker.compose.project': p.name,
                'com.docker.compose.service': 'app', 'com.docker.compose.project.working_dir': p.directory,
                'com.docker.compose.project.config_files': ','.join(p.files)}},
                'State': {'Status': 'running' if self.running else 'exited', 'Health': {'Status': self.health}}}])
        if cmd[1] == 'ps' or 'ps' in cmd:
            return 'cid'
        if 'config' in cmd:
            return json.dumps({'services': {'app': {'image': 'app:latest'}}})
        if 'up' in cmd:
            override = cmd[cmd.index('up')-1]
            ref = json.loads(Path(override).read_text())['services']['app']['image']
            if not (self.wrong and ref == NEW):
                self.actual = self.images.get(ref, ref)
            return ''
        raise AssertionError(cmd)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        file = self.root / 'compose.json'
        file.write_text('{}')
        self.project = Project('explicit', str(self.root), (str(file),), ('app',), True)
        self.fake = DockerFake(self.project)

    def guardian(self, **kwargs):
        return Guardian(self.root / 'reports', self.fake, health_timeout=0, stable_seconds=0, poll=0, **kwargs)

    def run_case(self, **kwargs):
        return self.guardian(**kwargs).run_round([self.project])

    def test_actual_baseline_and_success(self):
        code, reports = self.run_case(analyzer=LocalAnalyzer('safe'), execute=True)
        self.assertEqual(code, 0)
        self.assertEqual(reports[0]['baseline'], OLD)
        self.assertEqual(self.fake.actual, NEW)
        up = next(c for c in self.fake.commands if 'up' in c)
        self.assertIn('--no-build', up)
        self.assertEqual(up[up.index('--pull')+1], 'never')

    def test_wrong_target_recovers(self):
        self.fake.wrong = True
        code, reports = self.run_case(analyzer=LocalAnalyzer('safe'), execute=True)
        self.assertEqual(code, 1)
        self.assertEqual(reports[0]['status'], 'RECOVERED')
        self.assertTrue(reports[0]['rollback']['image_ok'])
        self.assertTrue(reports[0]['rollback']['health_ok'])
        self.assertEqual(self.fake.actual, OLD)

    def test_up_failure_recovers(self):
        self.fake.fail = lambda c: 'up' in c and '-target.json' in ' '.join(c)
        self.assertEqual(self.run_case(analyzer=LocalAnalyzer('safe'), execute=True)[1][0]['status'], 'RECOVERED')

    def test_recovery_failure_keeps_pending(self):
        self.fake.wrong = True
        self.fake.fail = lambda c: 'up' in c and '-rollback.json' in ' '.join(c)
        # Old unchanged is still verified, despite failed rollback command.
        report = self.run_case(analyzer=LocalAnalyzer('safe'), execute=True)[1][0]
        self.assertFalse(report['rollback']['command_ok'])
        self.assertEqual(report['status'], 'RECOVERED')

    def test_pull_failure_never_updates(self):
        self.fake.fail = lambda c: c[1] == 'pull'
        self.assertEqual(self.run_case(analyzer=LocalAnalyzer('safe'), execute=True)[0], 1)
        self.assertFalse(any('up' in c for c in self.fake.commands))

    def test_backup_failure_never_updates(self):
        self.fake.fail = lambda c: c[1:3] == ['image', 'tag']
        self.assertEqual(self.run_case(analyzer=LocalAnalyzer('safe'), execute=True)[0], 1)
        self.assertFalse(any('up' in c for c in self.fake.commands))

    def test_unavailable_pauses(self):
        self.assertEqual(self.run_case(execute=True)[1][0]['status'], 'PAUSED_ANALYZER')
        self.assertFalse(any('up' in c for c in self.fake.commands))

    def test_missing_channel_pauses(self):
        self.assertEqual(self.run_case(analyzer=LocalAnalyzer(), execute=True)[1][0]['status'], 'PAUSED_APPROVAL')

    def test_check_only(self):
        self.assertEqual(self.run_case(analyzer=LocalAnalyzer('safe'))[1][0]['status'], 'CHECK_ONLY')
        self.assertFalse(any('up' in c for c in self.fake.commands))

    def test_stopped_skipped(self):
        self.fake.running = False
        self.assertEqual(self.run_case()[1][0]['status'], 'SKIPPED_STOPPED')
        self.assertFalse(any(c[1] == 'pull' for c in self.fake.commands))

    def test_unhealthy_skipped(self):
        self.fake.health = 'unhealthy'
        self.assertEqual(self.run_case()[1][0]['status'], 'SKIPPED_UNHEALTHY')

    def test_no_update(self):
        self.fake.images['app:latest'] = OLD
        self.assertEqual(self.run_case()[1][0]['status'], 'NO_UPDATE')

    def test_identity_rejects(self):
        other = Project('wrong', self.project.directory, self.project.files, ('app',), True)
        code, reports = self.guardian().run_round([other])
        self.assertEqual(code, 1)
        self.assertIn('mismatch', reports[0]['error'])

    def test_association_unknown_pauses(self):
        p = Project('explicit', self.project.directory, self.project.files, ('app',))
        self.assertEqual(self.guardian().run_round([p])[0], 2)

    def test_approvals_bound_and_invalidated(self):
        approvals = Approvals('owner', lambda *args: None)
        approvals.begin('one')
        ident = approvals.request('p/s', NEW)
        self.assertEqual(ident, approvals.request('p/s', NEW))
        self.assertFalse(approvals.reply(ident, 'intruder', NEW, True))
        self.assertFalse(approvals.reply(ident, 'owner', OLD, True))
        self.assertTrue(approvals.reply(ident, 'owner', NEW, True))
        self.assertTrue(approvals.allowed(ident))
        approvals.begin('two')
        self.assertFalse(approvals.reply(ident, 'owner', NEW, True))
        self.assertFalse(approvals.allowed(ident))

    def test_retention_protects_pending_and_two_history(self):
        guardian = self.guardian()
        entries = [{'id': 'sha256:'+str(i), 'tag': 'compose-guardian-backup:'+str(i)} for i in range(5)]
        self.fake.images.update({e['tag']: e['id'] for e in entries})
        state = {'history': entries, 'current': NEW, 'pending': {'old': 'sha256:0', 'target': NEW}}
        guardian.cleanup(state)
        self.assertEqual([e['id'] for e in state['history']], ['sha256:0', 'sha256:3', 'sha256:4'])
        self.assertFalse(any(c[-1].startswith('sha256:') for c in self.fake.commands if c[1:3] == ['image', 'rm']))

    def test_unique_atomic_reports(self):
        self.run_case()
        self.run_case()
        self.assertEqual(len(list((self.root/'reports').glob('*/summary.json'))), 2)
        self.assertFalse(list((self.root/'reports').rglob('.guardian-*')))

    def test_mutual_exclusion(self):
        with round_lock(self.root/'lock'):
            with self.assertRaises(BlockingIOError):
                with round_lock(self.root/'lock'):
                    pass

    def test_timezone(self):
        now = datetime(2026, 10, 8, 1, 59, tzinfo=timezone.utc)
        self.assertEqual(next_daily(now).astimezone(timezone.utc).hour, 2)
        now = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(next_daily(now).day, 9)

    def test_redaction(self):
        text = redact('token=secret123 Bearer private https://example.com/?key=abc')
        self.assertNotIn('secret123', text)
        self.assertNotIn('private', text)
        self.assertNotIn('abc', text)

    def test_runner_returncode(self):
        runner = Runner()
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'token=secret123')):
            with self.assertRaises(CommandError):
                runner(['fake'])
        self.assertNotIn('secret123', json.dumps(runner.events))

    def test_real_recovery_failure_and_restart(self):
        base = self.fake.__call__
        def partial_failure(cmd):
            if 'up' in cmd and '-target.json' in ' '.join(cmd):
                self.fake.actual = NEW
                self.fake.health = 'unhealthy'
                raise CommandError('partial update')
            if 'up' in cmd and '-rollback.json' in ' '.join(cmd):
                raise CommandError('rollback blocked')
            return base(cmd)
        guardian = self.guardian(analyzer=LocalAnalyzer('safe'), execute=True)
        guardian.run = partial_failure
        code, reports = guardian.run_round([self.project])
        self.assertEqual(code, 1)
        self.assertEqual(reports[0]['status'], 'RECOVERY_FAILED')
        state_path = guardian.state_path('explicit/app')
        self.assertIsNotNone(json.loads(state_path.read_text())['pending'])
        self.fake.health = 'healthy'
        guardian.run = self.fake
        code, reports = guardian.run_round([self.project])
        self.assertEqual(reports[0]['status'], 'RECOVERED')
        self.assertEqual(self.fake.actual, OLD)
        self.assertIsNone(json.loads(state_path.read_text())['pending'])

    def test_empty_round_is_not_success(self):
        self.assertEqual(self.guardian().run_round([])[0], 2)

    def test_local_approval_executes(self):
        approvals = Approvals('owner')
        def notify(ident, request):
            approvals.reply(ident, 'owner', request['target'], True)
        approvals.notify = notify
        code, reports = self.run_case(analyzer=LocalAnalyzer(), approvals=approvals, execute=True)
        self.assertEqual(code, 0)
        self.assertEqual(reports[0]['status'], 'SUCCESS')

    def test_runner_timeout(self):
        runner = Runner(timeout=1)
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('fake', 1)):
            with self.assertRaises(CommandError):
                runner(['fake'])
        self.assertEqual(runner.events[0]['timeout'], 1)


if __name__ == '__main__':
    unittest.main()
