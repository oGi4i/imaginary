#!/usr/bin/env python3
"""Authenticate a replayed closure and propose an unqualified package lock in CI."""
import argparse
import ctypes
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('runtime_build', HERE / 'build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
closure = build.closure
discovery = build.discovery
EXPECTED_CLOSURE_SHA256 = '21f5e2a210886c85fa5eae45f20475977608d335307b4819ad92e431ff65161b'
EXPECTED_CLOSURE_SIZE = 2452794
WORKFLOW = '.github/workflows/runtime-inputs.yml'


def read_input(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('invalid input')
        data = stream.read(limit + 1)
        if len(data) != info.st_size or len(data) > limit:
            raise ValueError('invalid input')
        return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate_identity(env, commit):
    build.validate_ci_identity(env, commit)
    if (env.get('GITHUB_WORKFLOW_REF') != env['GITHUB_REPOSITORY'] + '/' + WORKFLOW + '@refs/heads/mitigate-native-memory'
            or not re.fullmatch('[1-9][0-9]{0,19}', env.get('GITHUB_RUN_ID', ''))
            or not re.fullmatch('[1-9][0-9]{0,9}', env.get('GITHUB_RUN_ATTEMPT', ''))):
        raise ValueError('CI identity mismatch')


def verify_payloads(packages):
    seen = set()
    total = 0
    for rows, _ in packages.values():
        for row in rows:
            pkg = row['package']
            closure._validate_package(pkg)
            size = int(pkg['Size'])
            if size > 256 * 1024**2:
                raise ValueError('package size limit')
            identity = (size, pkg['SHA256'])
            if identity in seen:
                continue
            seen.add(identity)
            total += size
            if total > 4 * 1024**3:
                raise ValueError('aggregate package size limit')
            data = closure.official_fetch(closure.archive_root(row['repository']) + pkg['Filename'], limit=size)
            closure.verify_deb(pkg, data)


def make_candidate(lock, evidence):
    states = [lock.get(kind + '_packages') is None for kind in ('build', 'runtime')]
    if states[0] != states[1] or lock.get('features') is not None:
        raise ValueError('unexpected lock state')
    template = copy.deepcopy(lock)
    if not states[0]:
        for kind in ('build', 'runtime'):
            template[kind + '_packages'] = None
            template[kind + '_roots'] = list(closure.ROOTS[kind])
    candidate = build.populate_lock.populate(template, evidence)
    if not states[0] and candidate != lock:
        raise ValueError('populated lock mismatch')
    return candidate


def publish(output, candidate, report):
    if output.exists() or output.is_symlink():
        raise ValueError('output exists')
    staging = Path(tempfile.mkdtemp(prefix='.runtime-verified-', dir=output.parent))
    try:
        (staging / 'candidate-lock.json').write_bytes(candidate)
        (staging / 'verification.json').write_bytes(report)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.renameat2(-100, os.fsencode(staging), -100, os.fsencode(output), 1) != 0:
            raise OSError(ctypes.get_errno(), 'publication failed')
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def run(args, env):
    validate_identity(env, args.commit)
    source = args.source.resolve(strict=True)
    output = build.output_path(args.output, source)
    closure_bytes = read_input(args.closure, 8 * 1024**2)
    build.verify_blob(closure_bytes, EXPECTED_CLOSURE_SIZE, EXPECTED_CLOSURE_SHA256)
    metadata_bytes = read_input(args.discovery, 2 * 1024**2)
    lock_bytes = read_input(args.lock, 1024**2)
    key_bytes = read_input(args.keyring, 2 * 1024**2)
    evidence, metadata, lock = (json.loads(data) for data in (closure_bytes, metadata_bytes, lock_bytes))
    closure._OVERALL_END = time.monotonic() + 1110
    with closure.deadline(1200):
        with tempfile.TemporaryDirectory(prefix='runtime-verify-') as temp:
            temp = Path(temp)
            with closure.deadline(1110):
                source_copy = temp / 'source'
                build.copy_source(source, source_copy, args.commit, args.tree)
                if lock_bytes != read_input(source_copy / 'docs/runtime-image.lock.yaml', 1024**2):
                    raise ValueError('source lock mismatch')
                workflow = read_input(source_copy / WORKFLOW, 1024**2)
                if read_input(source / WORKFLOW, 1024**2) != workflow:
                    raise ValueError('workflow mismatch')
                keyring = temp / 'keyring.gpg'
                keyring.write_bytes(key_bytes)
                with closure.deadline(build.overall_remaining(120)):
                    base = discovery.registry_manifest('library/debian', discovery.BASE)
                if metadata.get('base') != base:
                    raise ValueError('base metadata mismatch')
                build.validate_snapshot_pins(lock, metadata, keyring)
                indexes = closure.authenticated_indexes(metadata, keyring)
                build.validate_base_inventory(evidence, build.measure_base(temp, metadata))
                packages = build.validate_closure_evidence(evidence, indexes, build.compare_versions)
                verify_payloads(packages)
                candidate = make_candidate(lock, evidence)
                build.validate_closure(evidence, indexes, candidate, build.compare_versions)
                candidate_bytes = (json.dumps(candidate, sort_keys=True, indent=2) + '\n').encode()
                report = {'schema_version': 1, 'scope': 'package-closure-only', 'qualified': False,
                          'features': None, 'source_commit': args.commit, 'source_tree': args.tree,
                          'workflow': WORKFLOW, 'workflow_sha256': digest(workflow),
                          'repository': env['GITHUB_REPOSITORY'], 'ref': env['GITHUB_REF'],
                          'run_id': env['GITHUB_RUN_ID'], 'run_attempt': env['GITHUB_RUN_ATTEMPT'],
                          'input_lock_sha256': digest(lock_bytes), 'discovery_sha256': digest(metadata_bytes),
                          'closure_sha256': digest(closure_bytes), 'keyring_sha256': digest(key_bytes),
                          'candidate_sha256': digest(candidate_bytes)}
                report_bytes = (json.dumps(report, sort_keys=True, indent=2) + '\n').encode()
    publish(output, candidate_bytes, report_bytes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('lock', 'closure', 'discovery', 'keyring', 'source', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tree', required=True)
    run(parser.parse_args(), os.environ)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('runtime closure verification failed', file=sys.stderr)
        sys.exit(1)
