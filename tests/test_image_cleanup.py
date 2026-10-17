import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compose_guardian.image_cleanup import (
    Cleaner,
    CleanupError,
    Docker,
    normalize_reference,
    write_json,
)


def ident(number):
    return 'sha256:' + f'{number:064x}'


def image(number, tags=(), digests=(), parent=''):
    return {'Id': ident(number), 'RepoTags': list(tags), 'RepoDigests': list(digests),
            'Parent': parent, 'Created': f'2026-10-{number:02d}T00:00:00Z',
            'Size': 1000 * number, 'Config': {'Env': ['PASSWORD=private-value']}}


class FakeDocker:
    def __init__(self):
        self.images = {row['Id']: row for row in [
            image(1, ['app:latest']), image(2), image(3, ['local/tool:old']),
            image(4, ['app:next']), image(5, ['remote/app:old']),
        ]}
        self.containers = [{'Id': 'container-one', 'Name': '/app', 'Image': ident(1),
                            'Config': {'Image': 'app:latest'}, 'State': {'Status': 'exited'}}]
        self.volumes = [{'Name': 'database', 'Driver': 'local', 'Mountpoint': '/volumes/database'}]
        self.config = {'name': 'app', 'services': {'app': {'image': 'app:next'}}}
        self.calls = []
        self.daemon = 'daemon-one'
        self.failure = None
        self.after_delete = None

    def __call__(self, command):
        self.calls.append(command)
        if self.failure and self.failure(command):
            raise CleanupError('injected command failure')
        if command[:1] == ['info']:
            return self.daemon
        if command[:2] == ['image', 'ls']:
            return '\n'.join(self.images)
        if command[:2] == ['container', 'ls']:
            return '\n'.join(row['Id'] for row in self.containers)
        if command[:2] == ['volume', 'ls']:
            return '\n'.join(row['Name'] for row in self.volumes)
        if command[:2] == ['image', 'inspect']:
            return json.dumps([self.images[key] for key in command[2:]])
        if command[:2] == ['container', 'inspect']:
            return json.dumps([row for key in command[2:] for row in self.containers if row['Id'] == key])
        if command[:2] == ['volume', 'inspect']:
            return json.dumps([row for key in command[2:] for row in self.volumes if row['Name'] == key])
        if command[0] == 'compose':
            return json.dumps(self.config)
        if command[:2] == ['image', 'rm']:
            self.images.pop(command[-1])
            if self.after_delete:
                self.after_delete()
            return ''
        raise AssertionError(command)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = self.root / 'projects'
        self.project = self.projects / 'app'
        self.project.mkdir(parents=True)
        (self.project / 'compose.yaml').write_text('services: {}')
        self.state = self.root / 'reports'
        self.fake = FakeDocker()
        self.cleaner = Cleaner([self.projects], self.state, runner=self.fake)

    def plan_ids(self):
        return {row['id'] for row in self.cleaner.plan()['candidates']}

    def state_file(self, state):
        directory = self.state / 'state'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'service.json').write_text(json.dumps(state))

    def test_preview_has_no_mutations_and_protects_stopped_container_compose_local(self):
        plan = self.cleaner.plan()
        self.assertEqual({row['id'] for row in plan['candidates']}, {ident(2), ident(5)})
        self.assertTrue(all(command[:2] != ['image', 'rm'] for command in self.fake.calls))
        self.assertNotIn('private-value', json.dumps(plan))

    def test_running_container_is_also_protected(self):
        self.fake.containers[0]['State']['Status'] = 'running'
        self.assertNotIn(ident(1), self.plan_ids())

    def test_recorded_relative_compose_file_resolves_against_project_directory(self):
        self.fake.containers[0]['Config']['Labels'] = {
            'com.docker.compose.project': 'app',
            'com.docker.compose.project.working_dir': str(self.project),
            'com.docker.compose.project.config_files': 'compose.yaml',
        }
        self.cleaner.plan()
        commands = [command for command in self.fake.calls if command[0] == 'compose']
        self.assertTrue(any('-p' in command and str(self.project / 'compose.yaml') in command for command in commands))

    def test_local_alias_protects_whole_image_even_with_remote_alias(self):
        self.fake.images[ident(5)]['RepoTags'] += ['docker.io/local/custom:backup']
        self.assertNotIn(ident(5), self.plan_ids())

    def test_container_mutable_reference_protects_new_cached_target(self):
        self.fake.containers[0]['Config']['Image'] = 'remote/app:old'
        self.assertNotIn(ident(5), self.plan_ids())

    def test_digest_with_tag_and_default_registry_aliases(self):
        self.fake.images[ident(5)]['RepoDigests'] = ['redis@sha256:' + 'f' * 64]
        self.fake.config['services']['other'] = {'image': 'docker.io/library/redis:6.2@sha256:' + 'f' * 64}
        self.assertNotIn(ident(5), self.plan_ids())
        self.assertEqual(normalize_reference('docker.io/library/redis'), 'redis:latest')

    def test_missing_cached_compose_image_does_not_pull(self):
        self.fake.config['services']['future'] = {'image': 'future:next'}
        plan = self.cleaner.plan()
        self.assertIn('future:next', plan['missing_cached_references'])
        self.assertFalse(any('pull' in command for command in self.fake.calls))

    def test_guardian_history_pending_and_current_are_protected(self):
        self.state_file({'current': ident(2), 'history': [{'id': ident(5), 'tag': 'remote/app:old'}],
                         'pending': {'old': ident(2), 'target': ident(5)}})
        self.assertEqual(self.plan_ids(), set())

    def test_broken_recovery_state_blocks_everything(self):
        self.state_file({'history': [{'tag': 'broken'}]})
        with self.assertRaises(CleanupError):
            self.cleaner.plan()

    def test_protected_image_ancestors_are_kept(self):
        self.fake.images[ident(3)]['Parent'] = ident(2)
        self.assertNotIn(ident(2), self.plan_ids())

    def test_invalid_compose_blocks_cleanup_without_raw_diagnostics(self):
        self.fake.failure = lambda command: command[0] == 'compose'
        with self.assertRaises(CleanupError):
            self.cleaner.plan()
        self.assertFalse(any(command[:2] == ['image', 'rm'] for command in self.fake.calls))

    def test_empty_compose_or_missing_roots_block_cleanup(self):
        self.fake.config['services'] = {}
        with self.assertRaises(CleanupError):
            self.cleaner.plan()
        with self.assertRaises(CleanupError):
            Cleaner([self.root / 'missing'], self.state, runner=self.fake).plan()

    def test_ambiguous_base_files_require_explicit_manifest(self):
        (self.project / 'docker-compose.yml').write_text('services: {}')
        with self.assertRaises(CleanupError):
            self.cleaner.plan()
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'projects': [{'name': 'explicit', 'directory': str(self.project),
                                                     'files': [str(self.project / 'compose.yaml')]}]}))
        self.assertTrue(Cleaner([self.projects], self.state, manifest=manifest, runner=self.fake).plan())

    def test_profiles_and_override_files_are_rendered(self):
        override = self.project / 'compose.override.yaml'
        override.write_text('services: {}')
        self.cleaner.plan()
        command = next(command for command in self.fake.calls if command[0] == 'compose')
        self.assertIn(str(override.resolve()), command)
        self.assertEqual(command[command.index('--profile') + 1], '*')

    def test_build_only_default_image_names_are_protected(self):
        self.fake.images[ident(5)]['RepoTags'] = ['app-worker:latest']
        self.fake.config['services']['worker'] = {'build': '.'}
        self.assertNotIn(ident(5), self.plan_ids())

    def test_container_recorded_override_combination_and_explicit_path_mapping(self):
        override = self.project / 'target.json'
        override.write_text('{}')
        self.fake.containers[0]['Config']['Labels'] = {
            'com.docker.compose.project': 'recorded',
            'com.docker.compose.project.working_dir': '/host/projects/app',
            'com.docker.compose.project.config_files': '/host/projects/app/compose.yaml,/host/projects/app/target.json'}
        cleaner = Cleaner([self.projects], self.state, path_maps=(('/host/projects', str(self.projects)),), runner=self.fake)
        cleaner.plan()
        self.assertTrue(any(str(override) in command for command in self.fake.calls))

    def test_unreadable_recorded_combination_inside_scope_blocks_cleanup(self):
        self.fake.containers[0]['Config']['Labels'] = {
            'com.docker.compose.project.working_dir': str(self.project),
            'com.docker.compose.project.config_files': str(self.project / 'missing.yml')}
        with self.assertRaises(CleanupError):
            self.cleaner.plan()

    def test_apply_deletes_only_immutable_candidates_and_preserves_volumes(self):
        plan = self.cleaner.plan()
        containers, volumes = copy.deepcopy(self.fake.containers), copy.deepcopy(self.fake.volumes)
        result = self.cleaner.apply(plan)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(set(result['removed']), {ident(2), ident(5)})
        self.assertEqual(self.fake.containers, containers)
        self.assertEqual(self.fake.volumes, volumes)
        self.assertTrue(all('--no-prune' in command and '--force' not in command and IMAGE(command[-1])
                            for command in self.fake.calls if command[:2] == ['image', 'rm']))

    def test_multi_alias_requires_explicit_opt_in(self):
        self.fake.images[ident(5)]['RepoTags'] += ['remote/app:backup']
        plan = self.cleaner.plan()
        self.assertEqual(self.cleaner.apply(plan)['status'], 'complete_with_skips')
        self.assertIn(ident(5), self.fake.images)
        result = self.cleaner.apply(self.cleaner.plan(), allow_multi_tag=True)
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(any('--force' in command and command[-1] == ident(5) for command in self.fake.calls))

    def test_parent_is_deleted_after_candidate_child(self):
        self.fake.images[ident(2)]['Parent'] = ident(5)
        result = self.cleaner.apply(self.cleaner.plan())
        self.assertEqual(result['removed'], [ident(2), ident(5)])

    def test_skipped_child_keeps_its_parent(self):
        self.fake.images[ident(2)]['Parent'] = ident(5)
        self.fake.images[ident(2)]['RepoTags'] = ['old:a', 'old:b']
        result = self.cleaner.apply(self.cleaner.plan())
        self.assertEqual(result['status'], 'complete_with_skips')
        self.assertEqual(result['removed'], [])

    def test_new_container_or_compose_change_invalidates_preview(self):
        plan = self.cleaner.plan()
        self.fake.containers.append({'Id': 'new', 'Name': '/new', 'Image': ident(5), 'Config': {}})
        self.assertEqual(self.cleaner.apply(plan)['status'], 'failed')
        self.assertFalse(any(command[:2] == ['image', 'rm'] for command in self.fake.calls))
        self.fake.containers.pop()
        self.fake.config['services']['app']['image'] = 'remote/app:old'
        self.assertEqual(self.cleaner.apply(plan)['status'], 'failed')

    def test_daemon_change_or_tampered_candidate_is_rejected(self):
        plan = self.cleaner.plan()
        self.fake.daemon = 'different-daemon'
        self.assertEqual(self.cleaner.apply(plan)['status'], 'failed')
        self.fake.daemon = 'daemon-one'
        plan['candidates'].append({'id': ident(1)})
        self.assertEqual(self.cleaner.apply(plan)['status'], 'failed')

    def test_mid_run_reference_change_aborts_remaining_deletions_and_journals_partial(self):
        plan = self.cleaner.plan()
        self.fake.after_delete = lambda: self.fake.containers.append(
            {'Id': 'new', 'Name': '/new', 'Image': ident(2), 'Config': {}})
        journals = []
        result = self.cleaner.apply(plan, journal=lambda value: journals.append(copy.deepcopy(value)))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['removed'], [ident(5)])
        self.assertIn(ident(2), self.fake.images)
        self.assertEqual(journals[-1], result)

    def test_native_delete_conflict_stops_without_pruning_containers_or_volumes(self):
        self.fake.failure = lambda command: command[:2] == ['image', 'rm']
        result = self.cleaner.apply(self.cleaner.plan())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.fake.images), 5)
        self.assertEqual(result['attempted'], [ident(5)])
        self.assertFalse(any(command[0] in ('system', 'prune') or 'prune' in command for command in self.fake.calls))

    def test_atomic_private_json_report(self):
        path = self.root / 'report.json'
        write_json(path, {'status': 'preview'})
        self.assertEqual(json.loads(path.read_text())['status'], 'preview')
        self.assertFalse(list(self.root.glob('.image-cleanup-*')))

    def test_docker_command_errors_do_not_leak_inspect_or_config_output(self):
        with (
            patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, 'PASSWORD=private-value', 'token=secret')),
            self.assertRaises(CleanupError) as caught,
        ):
            Docker()(['image', 'inspect', 'app'])
        self.assertNotIn('private-value', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    @unittest.skipIf(os.name == 'nt', 'Updater flock integration runs in Linux validation')
    def test_legacy_updater_uses_the_same_cleanup_lock(self):
        from compose_guardian import updater
        from compose_guardian.core import round_lock
        self.state.mkdir()
        with (
            patch.dict(os.environ, {'GUARDIAN_STATE_DIR': str(self.state)}),
            patch.object(updater, '_run_once_unlocked') as update,
        ):
            with round_lock(self.state / 'guardian.lock'):
                updater.run_once()
            update.assert_not_called()
            updater.run_once()
            update.assert_called_once()


def IMAGE(value):
    return value.startswith('sha256:') and len(value) == 71


if __name__ == '__main__':
    unittest.main()
