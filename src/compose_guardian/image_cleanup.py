"""Explicit, preview-first image cleanup. Never starts or removes containers."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

FILENAMES = ('compose.yaml', 'compose.yml', 'docker-compose.yaml', 'docker-compose.yml')
OVERRIDES = ('compose.override.yaml', 'compose.override.yml',
             'docker-compose.override.yaml', 'docker-compose.override.yml')
IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')
LOCAL_UNUSED_KEEP = 2


class CleanupError(RuntimeError):
    pass


class Docker:
    def __init__(self, timeout=120):
        self.timeout = timeout

    def __call__(self, arguments):
        # Do not log inspect/config stdout, environment values, or daemon errors.
        try:
            result = subprocess.run(['docker', *arguments], capture_output=True,
                                    text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise CleanupError('Docker command unavailable or timed out; refresh inventory before retrying') from None
        if result.returncode:
            raise CleanupError(f'Docker {arguments[0]} command failed (exit {result.returncode})')
        return result.stdout.strip()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.image-cleanup-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def normalize_reference(reference):
    if reference.startswith('sha256:'):
        return reference
    for registry in ('docker.io/', 'index.docker.io/', 'registry-1.docker.io/'):
        if reference.startswith(registry):
            reference = reference[len(registry):]
            break
    reference = reference.removeprefix('library/')
    if '@' in reference:
        repository, digest = reference.split('@', 1)
        if ':' in repository.rsplit('/', 1)[-1]:
            repository = repository.rsplit(':', 1)[0]
        return repository + '@' + digest
    if ':' not in reference.rsplit('/', 1)[-1]:
        reference += ':latest'
    return reference


def creation_order(image):
    # Docker uses RFC3339Nano. Keep nanoseconds and compare absolute UTC times.
    try:
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})', image['created'])
        if not match:
            raise ValueError()
        second = datetime.fromisoformat(match[1] + match[3]).astimezone(UTC)
        return second, int((match[2] or '').ljust(9, '0')), image['id']
    except (ValueError, TypeError, OverflowError):
        raise CleanupError('Invalid local image creation timestamp; cleanup blocked') from None


class Cleaner:
    def __init__(self, roots, state_dir, *, manifest=None, path_maps=(),
                 keep_prefixes=(), runner=None):
        self.roots = tuple(str(Path(root).resolve()) for root in roots)
        self.state_dir = Path(state_dir).resolve()
        self.manifest = str(Path(manifest).resolve()) if manifest else None
        self.path_maps = tuple(path_maps)
        self.keep_prefixes = tuple(keep_prefixes)
        self.run = runner or Docker()

    @property
    def scope(self):
        return {'roots': self.roots, 'state_dir': str(self.state_dir),
                'manifest': self.manifest, 'path_maps': self.path_maps,
                'keep_prefixes': self.keep_prefixes, 'local_unused_keep': LOCAL_UNUSED_KEEP}

    def _json(self, arguments):
        try:
            return json.loads(self.run(arguments))
        except (json.JSONDecodeError, TypeError):
            raise CleanupError('Invalid Docker/Compose JSON; cleanup blocked') from None

    def _inspect(self, arguments, identifiers):
        result = []
        for start in range(0, len(identifiers), 64):
            batch = self._json([*arguments, *identifiers[start:start + 64]])
            if not isinstance(batch, list) or len(batch) != len(identifiers[start:start + 64]):
                raise CleanupError('Incomplete Docker inventory; cleanup blocked')
            result.extend(batch)
        return result

    def _mapped(self, value):
        for old, new in sorted(self.path_maps, key=lambda item: len(item[0]), reverse=True):
            if value == old or value.startswith(old.rstrip('/') + '/'):
                return new.rstrip('/') + value[len(old):]
        return value

    def _projects(self, containers):
        projects = {}
        explicit_directories = set()
        if self.manifest:
            try:
                entries = json.loads(Path(self.manifest).read_text(encoding='utf-8'))['projects']
                for entry in entries:
                    if not entry['name'] or not Path(entry['directory']).is_absolute() or any(not Path(file).is_absolute() for file in entry['files']):
                        raise ValueError('absolute project identity required')
                    directory = str(Path(entry['directory']).resolve())
                    files = tuple(str(Path(file).resolve()) for file in entry['files'])
                    projects[(entry['name'], directory, files)] = None
                    explicit_directories.add(directory)
            except (OSError, ValueError, KeyError, TypeError):
                raise CleanupError('Invalid cleanup project manifest') from None
        for root in self.roots:
            path = Path(root)
            if not path.is_dir():
                raise CleanupError(f'Compose root unavailable: {root}')
            for directory in (path, *sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith('.'))):
                if str(directory.resolve()) in explicit_directories:
                    continue
                files = [directory / name for name in FILENAMES if (directory / name).is_file()]
                if len(files) > 1:
                    raise CleanupError(f'Ambiguous Compose files: {directory}; specify a manifest')
                if not files:
                    continue
                overrides = [directory / name for name in OVERRIDES if (directory / name).is_file()]
                if len(overrides) > 1:
                    raise CleanupError(f'Ambiguous Compose overrides: {directory}')
                projects[('', str(directory.resolve()), tuple(str(p.resolve()) for p in files + overrides))] = None
        # Preserve additional recorded file combinations when they are accessible.
        # Explicit roots/manifest define scope; never silently remap host paths.
        for container in containers:
            labels = container.get('Config', {}).get('Labels') or {}
            files = labels.get('com.docker.compose.project.config_files', '')
            directory = labels.get('com.docker.compose.project.working_dir', '')
            if not files or not directory:
                continue
            directory = str(Path(self._mapped(directory)).resolve())
            files = tuple(str((Path(directory) / self._mapped(file)).resolve())
                          for file in files.split(','))
            if all(Path(file).is_file() for file in files) and Path(directory).is_dir():
                projects[(labels.get('com.docker.compose.project', ''), directory, files)] = None
            elif any(Path(file).resolve().is_relative_to(Path(root)) for root in self.roots for file in files):
                raise CleanupError('Recorded Compose combination inside selected roots is unavailable')
        if not projects:
            raise CleanupError('No Compose projects found; refusing incomplete protection scope')
        for _, directory, files in projects:
            if not Path(directory).is_dir() or not files or any(not Path(file).is_file() for file in files):
                raise CleanupError('Selected Compose project files are unavailable')
        return sorted(projects)

    def _compose(self, project):
        name, directory, files = project
        command = ['compose', '--profile', '*', '--project-directory', directory]
        if name:
            command += ['-p', name]
        for file in files:
            command += ['-f', file]
        config = self._json([*command, 'config', '--format', 'json'])
        if not isinstance(config, dict) or not isinstance(config.get('services'), dict) or not config['services']:
            raise CleanupError('Compose service inventory missing')
        references = set()
        for service, settings in config['services'].items():
            reference = settings.get('image')
            if reference:
                references.add(reference)
            elif settings.get('build') and config.get('name'):
                # Protect both Compose naming conventions for build-only services.
                references.update((f"{config['name']}-{service}:latest",
                                   f"{config['name']}_{service}:latest"))
            else:
                raise CleanupError('Cannot establish image identity for a Compose service')
        return {'project': [name, directory, list(files)], 'hash': fingerprint(config),
                'references': sorted(references)}

    def collect(self):
        daemon = self.run(['info', '--format', '{{.ID}}'])
        if not daemon:
            raise CleanupError('Docker daemon identity unavailable')
        identifiers = sorted(set(self.run(['image', 'ls', '--all', '--quiet', '--no-trunc']).split()))
        if any(not IMAGE_ID.fullmatch(identifier) for identifier in identifiers):
            raise CleanupError('Full immutable image IDs required')
        images = {}
        for image in self._inspect(['image', 'inspect'], identifiers):
            images[image['Id']] = {
                'id': image['Id'], 'tags': sorted(tag for tag in (image.get('RepoTags') or []) if tag != '<none>:<none>'),
                'digests': sorted(image.get('RepoDigests') or []), 'parent': image.get('Parent', ''),
                'created': image.get('Created', ''), 'size': image.get('Size', 0),
            }
        if set(images) != set(identifiers):
            raise CleanupError('Image inventory changed while inspecting')
        container_ids = sorted(set(self.run(['container', 'ls', '--all', '--quiet', '--no-trunc']).split()))
        raw_containers = self._inspect(['container', 'inspect'], container_ids)
        containers = []
        for container in raw_containers:
            if container['Image'] not in images:
                raise CleanupError('A container references an unavailable image')
            containers.append({'id': container['Id'], 'name': container.get('Name', ''),
                               'image': container['Image'], 'reference': container.get('Config', {}).get('Image', '')})
        volume_names = sorted(set(self.run(['volume', 'ls', '--quiet']).split()))
        volume_rows = self._inspect(['volume', 'inspect'], volume_names)
        volumes = sorted((row['Name'], row.get('Driver', ''), row.get('Mountpoint', '')) for row in volume_rows)
        projects = self._projects(raw_containers)
        with ThreadPoolExecutor(max_workers=4) as executor:
            compose = list(executor.map(self._compose, projects))
        states = []
        for path in sorted((self.state_dir / 'state').glob('*.json')):
            try:
                state = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(state, dict):
                    raise TypeError()
                references = []
                for entry in state.get('history', []):
                    references.extend((entry['id'], entry['tag']))
                if state.get('current'):
                    references.append(state['current'])
                if state.get('pending'):
                    references.extend((state['pending']['old'], state['pending']['target']))
                    if state['pending'].get('tag'):
                        references.append(state['pending']['tag'])
                if any(not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/@:-]*', reference)
                       or (reference.startswith('sha256:') and not IMAGE_ID.fullmatch(reference))
                       for reference in references):
                    raise ValueError()
                states.append({'path': str(path), 'hash': fingerprint(state), 'references': sorted(references)})
            except (OSError, ValueError, KeyError, TypeError):
                raise CleanupError('Unreadable or invalid Guardian recovery state') from None
        return {'daemon': daemon, 'images': images, 'containers': sorted(containers, key=lambda row: row['id']),
                'volumes': volumes, 'compose': compose, 'states': states}

    def plan(self, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.collect()
        images = snapshot['images']
        aliases = {normalize_reference(reference): identifier
                   for identifier, image in images.items()
                   for reference in [identifier, *image['tags'], *image['digests']]}
        protected = {}
        missing = set()

        def protect(reference, reason):
            identifier = aliases.get(normalize_reference(reference))
            if identifier:
                protected.setdefault(identifier, set()).add(reason)
            else:
                missing.add(reference)

        for container in snapshot['containers']:
            protect(container['image'], 'container:' + container['name'])
            if container['reference']:
                protect(container['reference'], 'container-reference:' + container['name'])
        for project in snapshot['compose']:
            for reference in project['references']:
                protect(reference, 'compose')
        for state in snapshot['states']:
            for reference in state['references']:
                protect(reference, 'guardian-state')
        for identifier, image in images.items():
            for prefix in self.keep_prefixes:
                if any(normalize_reference(tag).startswith(prefix) for tag in image['tags']):
                    protect(identifier, 'keep-prefix:' + prefix)

        def protect_ancestors():
            pending = list(protected)
            while pending:
                parent = images[pending.pop()].get('parent')
                if parent in images and parent not in protected:
                    protected[parent] = {'protected-image-ancestor'}
                    pending.append(parent)

        # Required references and their ancestors never consume the extra history quota.
        protect_ancestors()
        repositories = {}
        for identifier, image in images.items():
            if identifier in protected:
                continue
            for tag in image['tags']:
                reference = normalize_reference(tag)
                if reference.startswith('local/'):
                    repository = reference.rsplit(':', 1)[0]
                    repositories.setdefault(repository, set()).add(identifier)
        local_history = {}
        # Select independently per repository, then union the protected IDs.
        for repository, identifiers in sorted(repositories.items()):
            ordered = sorted(identifiers, key=lambda key: creation_order(images[key]), reverse=True)
            retained = ordered[:LOCAL_UNUSED_KEEP]
            local_history[repository] = {'retained_unused_ids': retained,
                                         'older_unused_ids': ordered[LOCAL_UNUSED_KEEP:]}
            for identifier in retained:
                protect(identifier, 'local-history:' + repository)
        protect_ancestors()
        candidates = [image for identifier, image in images.items() if identifier not in protected]
        return {'schema': 1, 'created': datetime.now(UTC).isoformat(), 'scope': self.scope,
                'daemon': snapshot['daemon'], 'fingerprint': fingerprint(snapshot),
                'candidates': sorted(candidates, key=lambda row: row['id']),
                'protected': {identifier: sorted(reasons) for identifier, reasons in sorted(protected.items())},
                'local_history': local_history,
                'missing_cached_references': sorted(missing),
                'container_count': len(snapshot['containers']), 'volume_count': len(snapshot['volumes'])}

    def apply(self, plan, *, allow_multi_tag=False, journal=None):
        result = {'status': 'running', 'attempted': [], 'removed': [], 'skipped': [], 'error': None}
        save = journal or (lambda result: None)
        save(result)
        try:
            current = self.collect()
            fresh = self.plan(current)
            if (plan.get('schema') != 1 or fingerprint(plan.get('scope')) != fingerprint(self.scope)
                    or plan.get('daemon') != current['daemon'] or plan.get('fingerprint') != fingerprint(current)):
                raise CleanupError('Preview is stale or belongs to a different cleanup scope/daemon')
            candidates = {row['id'] for row in fresh['candidates']}
            approved = {row['id'] for row in plan['candidates']}
            if not approved <= candidates or len(approved) != len(plan['candidates']):
                raise CleanupError('Preview contains a protected, duplicate or unknown image ID')
            expected = current
            remaining = set(approved)
            while remaining:
                parents = {image['parent'] for image in expected['images'].values()}
                leaves = remaining - parents
                if not leaves:
                    result['skipped'].extend({'id': key, 'reason': 'retained descendant image'} for key in sorted(remaining))
                    break
                identifier = max(leaves, key=lambda key: (expected['images'][key]['created'], key))
                remaining.remove(identifier)
                live = self.collect()
                if fingerprint(live) != fingerprint(expected) or identifier in self.plan(live)['protected']:
                    raise CleanupError('Docker/Compose/recovery references changed; refresh preview')
                image = live['images'][identifier]
                multiple = len(image['tags']) + len(image['digests']) > 1
                if multiple and not allow_multi_tag:
                    result['skipped'].append({'id': identifier, 'reason': 'multiple aliases require explicit opt-in'})
                    save(result)
                    continue
                command = ['image', 'rm', '--no-prune']
                if multiple:
                    command.append('--force')
                result['attempted'].append(identifier)
                save(result)
                self.run([*command, identifier])
                expected = dict(live, images={key: value for key, value in live['images'].items() if key != identifier})
                after = self.collect()
                if identifier not in after['images']:
                    result['removed'].append(identifier)
                if fingerprint(after) != fingerprint(expected):
                    raise CleanupError('Unexpected Docker mutation after image deletion; cleanup stopped')
                save(result)
            result['status'] = 'complete' if not result['skipped'] else 'complete_with_skips'
        except Exception as error:  # noqa: BLE001 - persist partial progress for unexpected failures
            result['status'] = 'failed'
            # Do not include daemon stderr/config output or image environment values.
            result['error'] = str(error) if isinstance(error, CleanupError) else type(error).__name__
        save(result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose-root', action='append', default=[])
    parser.add_argument('--manifest', help='explicit project names/directories/file combinations (core manifest format)')
    parser.add_argument('--state-dir', default=os.getenv('GUARDIAN_STATE_DIR', os.getenv('REPORT_DIR', '/reports')))
    parser.add_argument('--path-map', action='append', default=[], metavar='HOST_PREFIX=VISIBLE_PREFIX')
    parser.add_argument('--keep-prefix', action='append', default=[], help='always retain every image matching this prefix, overriding local history retention')
    parser.add_argument('--plan', required=True, help='preview JSON path; required input for --apply')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--allow-multi-tag', action='store_true', help='permit immutable-ID force removal of approved multi-alias images; quiesce other Docker writers first')
    args = parser.parse_args()
    roots = args.compose_root or ([os.environ['COMPOSE_ROOT']] if os.getenv('COMPOSE_ROOT') else [])
    if not roots and not args.manifest:
        parser.error('--compose-root (or COMPOSE_ROOT) or --manifest is required')
    mappings = []
    for mapping in args.path_map:
        if '=' not in mapping:
            parser.error('--path-map requires HOST_PREFIX=VISIBLE_PREFIX')
        old, new = mapping.split('=', 1)
        if not old or not new:
            parser.error('--path-map prefixes must not be empty')
        mappings.append((old.rstrip('/'), new.rstrip('/')))
    cleaner = Cleaner(roots, args.state_dir, manifest=args.manifest, path_maps=mappings,
                      keep_prefixes=args.keep_prefix)
    # Same lock used by the opt-in updater core and the legacy update entrypoint.
    from .core import round_lock
    try:
        with round_lock(Path(args.state_dir) / 'guardian.lock'):
            if args.apply:
                plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
                report = Path(args.plan).with_suffix('.result.json')
                result = cleaner.apply(plan, allow_multi_tag=args.allow_multi_tag,
                                       journal=lambda value: write_json(report, value))
                print(json.dumps({'status': result['status'], 'removed': len(result['removed']),
                                  'skipped': len(result['skipped']), 'report': str(report)}, ensure_ascii=False))
                return 1 if result['status'] == 'failed' else 0
            plan = cleaner.plan()
            write_json(args.plan, plan)
            print(json.dumps({'status': 'preview', 'candidates': len(plan['candidates']),
                              'protected': len(plan['protected']), 'plan': args.plan}, ensure_ascii=False))
            return 0
    except (CleanupError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'status': 'blocked', 'error': str(error) if isinstance(error, CleanupError) else type(error).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
