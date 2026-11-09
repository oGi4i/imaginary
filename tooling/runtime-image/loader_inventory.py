#!/usr/bin/env python3
"""Collect bounded registered libvips types from two identity-checked images in CI."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid


def sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reference = sibling('reference_inventory')
image_load = sibling('image_load')
LIMIT = reference.LIMIT
PROBE = '/usr/local/share/imaginary-build/loader-probe'
PROBE_EXIT_REASONS = {41: 'probe-setup', 42: 'probe-init', 43: 'probe-version',
                      44: 'probe-classes', 45: 'probe-library', 46: 'probe-stderr',
                      47: 'probe-protocol', 48: 'probe-output'}


class ProbeStageExit(reference.ProbeExit):
    pass


class DiagnosticError(ValueError):
    pass


@contextmanager
def stage(name):
    try:
        yield
    except DiagnosticError:
        raise
    except ProbeStageExit as exc:
        raise DiagnosticError(name + ': ' + PROBE_EXIT_REASONS[exc.code]) from None
    except reference.ProbeExit:
        raise DiagnosticError(name + ': command-exit') from None
    except reference.InventoryError:
        raise DiagnosticError(name + ': command-failed') from None
    except OSError:
        raise DiagnosticError(name + ': io-failed') from None
    except (KeyError, TypeError):
        raise DiagnosticError(name + ': invalid-structure') from None
    except (ValueError, UnicodeError):
        raise DiagnosticError(name + ': invalid-data') from None


def parse_protocol(data):
    try:
        if len(data) > LIMIT or not data.endswith(b'\n'):
            raise ValueError()
        lines = data.decode('ascii').split('\n')[:-1]
        if len(lines) < 3 or lines[0] != 'VIPS_PROBE 1' or lines[-1] != 'END':
            raise ValueError()
        version = re.fullmatch(r'VERSION ([0-9]+\.[0-9]+\.[0-9]+)', lines[1])
        if not version or any(int(part) > 9999 for part in version.group(1).split('.')):
            raise ValueError()
        result = {'version': version.group(1), 'loaders': [], 'savers': []}
        saving = False
        for line in lines[2:-1]:
            match = re.fullmatch(r'(LOAD|SAVE) (VipsForeign(?:Load|Save)[A-Za-z0-9_]+)', line)
            if not match:
                raise ValueError()
            kind, name = match.groups()
            if len(name) > 128 or not name.startswith('VipsForeign' + ('Load' if kind == 'LOAD' else 'Save')):
                raise ValueError()
            if kind == 'SAVE':
                saving = True
            elif saving:
                raise ValueError()
            values = result['loaders' if kind == 'LOAD' else 'savers']
            if len(values) >= 1024 or (values and name <= values[-1]):
                raise ValueError()
            values.append(name)
        if not result['loaders'] or not result['savers']:
            raise ValueError()
        return result
    except (UnicodeError, ValueError) as exc:
        raise ValueError('invalid loader protocol') from exc


def digest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 16 * LIMIT:
            raise ValueError('invalid probe artifact')
        hasher = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(fd, min(LIMIT, 16 * LIMIT + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            if count > 16 * LIMIT:
                raise ValueError('invalid probe artifact')
            hasher.update(chunk)
        if count != info.st_size:
            raise ValueError('invalid probe artifact')
        return hasher.hexdigest()
    finally:
        os.close(fd)


def cleanup(run, name, label):
    raw = run(['docker', 'ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}'], timeout=30)
    names = raw.decode('ascii').splitlines()
    if names:
        if names != [name]:
            raise ValueError('container identity mismatch')
        data = json.loads(run(['docker', 'inspect', name], timeout=30))
        if len(data) != 1 or data[0]['Config']['Labels'].get('imaginary.loader-probe') != label:
            raise ValueError('container identity mismatch')
        run(['docker', 'rm', '-f', name], timeout=30)
    if run(['docker', 'ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}'], timeout=30).strip():
        raise ValueError('container cleanup failed')


def probe(run, image, path):
    label = uuid.uuid4().hex
    name = 'loader-inventory-' + label
    argv = ['docker', 'run', '--name', name, '--label', 'imaginary.loader-probe=' + label,
            '--platform=linux/amd64', '--network=none', '--read-only', '--user=65534:65534',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=128',
            '--memory=512m', '--cpus=1', '--pull=never',
            '--mount', 'type=bind,src=' + str(path) + ',dst=' + PROBE + ',readonly',
            '--entrypoint=/usr/bin/env', image, '-i', 'HOME=/nonexistent',
            'XDG_CACHE_HOME=/nonexistent', 'XDG_CONFIG_HOME=/nonexistent', 'TMPDIR=/nonexistent',
            'LC_ALL=C', PROBE]
    if run(['docker', 'ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}'], timeout=30).strip():
        raise ValueError('container name already exists')
    try:
        try:
            data = run(argv, timeout=45, limit=LIMIT)
        except reference.ProbeExit as exc:
            if exc.code in PROBE_EXIT_REASONS:
                raise ProbeStageExit(exc.code) from None
            raise
        return parse_protocol(data)
    finally:
        with stage('cleanup'):
            cleanup(run, name, label)


def safe_environment(inspect):
    config = inspect[0]['Config']
    if config.get('Volumes'):
        raise ValueError('image volumes are not allowed')
    items = config.get('Env') or []
    if not isinstance(items, list) or any(not isinstance(item, str) or
            re.match(r'^(?:LD_|VIPS_)', item) for item in items):
        raise ValueError('unsafe image environment')


def collect(run, expected, inspect, binary, source):
    with stage('candidate-identity'):
        image_load.validate_loaded(expected, inspect)
        safe_environment(inspect)
        candidate_id = expected['config']
    with stage('probe-identity'):
        binary_hash = digest(binary)
        source_hash = digest(source)
    with stage('reference-identity'):
        run(['docker', 'pull', '--platform=linux/amd64', reference.IMAGE], timeout=300)
        ref_data = run(['docker', 'image', 'inspect', reference.IMAGE], timeout=30)
        ref = reference.identity(ref_data)
        ref_inspect = image_load.verify._json(b'{"value":' + ref_data + b'}')['value']
        safe_environment(ref_inspect)
    with stage('candidate-probe'):
        candidate = probe(run, candidate_id, binary)
    with stage('reference-probe'):
        published = probe(run, reference.CONFIG, binary)
    with stage('probe-identity'):
        if digest(binary) != binary_hash or digest(source) != source_hash:
            raise ValueError('probe identity changed')
    differences = {}
    for key in ('loaders', 'savers'):
        differences[key] = {'candidate_only': sorted(set(candidate[key]) - set(published[key])),
                            'reference_only': sorted(set(published[key]) - set(candidate[key]))}
    equal = candidate == published
    return {'schema_version': 1, 'probe_sha256': binary_hash, 'source_sha256': source_hash,
            'candidate_identity': {'config': candidate_id, 'diff_ids': expected['diff_ids'],
                                   'os': 'linux', 'architecture': 'amd64'},
            'reference_identity': ref, 'candidate': candidate, 'reference': published,
            'differences': differences, 'registered_types_equal': equal,
            'features_complete': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected', required=True, type=Path)
    parser.add_argument('--inspect', required=True, type=Path)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        with stage('setup'):
            reference.require_ci()
            expected = image_load.read_json(args.expected)
            inspect = image_load.read_json(args.inspect)
            with tempfile.TemporaryDirectory(prefix='loader-inventory-') as temp:
                env = {'PATH': os.defpath, 'HOME': temp, 'DOCKER_CONFIG': temp,
                       'DOCKER_HOST': 'unix:///var/run/docker.sock'}
                run = lambda argv, **kw: reference.bounded_run(argv, env=env, **kw)
                binary = args.binary.absolute()
                if binary.parent.resolve(strict=True) != binary.parent:
                    raise ValueError('unsafe probe path')
                result = collect(run, expected, inspect, binary, args.source)
        with stage('output'):
            encoded = (json.dumps(result, sort_keys=True, indent=2) + '\n').encode('ascii')
            if len(encoded) > LIMIT:
                raise ValueError('inventory exceeds limit')
            args.output.write_bytes(encoded)
            return 0 if result['registered_types_equal'] else 2
    except DiagnosticError as exc:
        print('loader inventory failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
