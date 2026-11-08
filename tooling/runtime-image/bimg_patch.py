#!/usr/bin/env python3
"""Stage the approved bimg replacement after the caller verifies Go modules."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

spec = importlib.util.spec_from_file_location('reference_inventory', Path(__file__).resolve().parent / 'reference_inventory.py')
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)

IDENTITY = {
    'module': 'github.com/h2non/bimg', 'version': 'v1.1.4',
    'source_url': 'https://raw.githubusercontent.com/h2non/bimg/v1.1.4/resizer.go',
    'original_sha256': '4897d4105655308bb8235132f057753b718facd1368b11c867ee28d14c16802f',
    'original_size': 14111,
    'patched_sha256': '6dd0a8e838850b54500fad5fd48f4fdd1b014b566093523e2a9a02e342ae4bef',
    'patched_size': 14861,
}
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 10000
SAFE_PATH = re.compile(r'[A-Za-z0-9_./+@-]+\Z')


def read_regular(path, limit=MAX_FILE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('unsafe file')
        data = stream.read(limit + 1)
        if len(data) != info.st_size or len(data) > limit:
            raise ValueError('file changed while reading')
        return data


def tree(path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError('unsafe module directory')
    files = {}
    total = 0
    for base, dirs, names in os.walk(path, followlinks=False):
        for name in dirs:
            item = Path(base) / name
            if item.is_symlink() or not item.is_dir():
                raise ValueError('unsafe module directory')
        for name in names:
            item = Path(base) / name
            rel = item.relative_to(path).as_posix()
            if not SAFE_PATH.fullmatch(rel):
                raise ValueError('unsafe module path')
            data = read_regular(item)
            total += len(data)
            if total > MAX_TOTAL or len(files) >= MAX_FILES:
                raise ValueError('module size limit')
            files[rel] = data
    return files


def checked(data, size, digest):
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('patch hash mismatch')


def stage(source, module, output, replace_path):
    source, module, output = Path(source), Path(module), Path(output)
    if not replace_path.startswith('/') or not SAFE_PATH.fullmatch(replace_path) or '..' in Path(replace_path).parts:
        raise ValueError('invalid replacement path')
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError('output must be new')
    if any(path.absolute() != path.resolve() for path in (source, module, output.parent)):
        raise ValueError('symlink in input or output path')
    if source.resolve() in output.resolve().parents or module.resolve() in output.resolve().parents:
        raise ValueError('output inside source or module')
    if replace_path not in ('/bimg-patch/module', str(output.resolve() / 'module')):
        raise ValueError('replacement path is not bound to output')
    patchdir = source / 'patches/bimg-v1.1.4-webp'
    if patchdir.absolute() != patchdir.resolve():
        raise ValueError('symlink in patch path')
    identity = json.loads(read_regular(patchdir / 'identity.json', 4096))
    if identity != IDENTITY:
        raise ValueError('patch identity mismatch')
    replacement = read_regular(patchdir / 'resizer.go.txt')
    checked(replacement, identity['patched_size'], identity['patched_sha256'])
    files = tree(module)
    checked(files.get('resizer.go', b''), identity['original_size'], identity['original_sha256'])
    if not re.search(rb'(?m)^module\s+github\.com/h2non/bimg\s*$', files.get('go.mod', b'')):
        raise ValueError('module identity mismatch')
    mod = read_regular(source / 'go.mod')
    sums = read_regular(source / 'go.sum')
    if re.search(rb'(?m)^\s*(replace|toolchain|godebug|work)\b', mod):
        raise ValueError('unsupported module directive')
    if not re.search(rb'(?m)^\s*(?:require\s+)?github\.com/h2non/bimg\s+v1\.1\.4\s*$', mod):
        raise ValueError('unapproved bimg version')
    files['resizer.go'] = replacement
    output.mkdir()
    (output / 'module').mkdir()
    for name, data in sorted(files.items()):
        dest = output / 'module' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    (output / 'patch.mod').write_bytes(mod.rstrip() + b'\n\nreplace github.com/h2non/bimg => ' + replace_path.encode('ascii') + b'\n')
    (output / 'patch.sum').write_bytes(sums)
    (output / 'identity.json').write_text(json.dumps(identity, sort_keys=True, separators=(',', ':')) + '\n')
    lines = [hashlib.sha256(data).hexdigest() + '  module/' + name for name, data in sorted(files.items())]
    (output / 'manifest.sha256').write_text('\n'.join(lines) + '\n')
    original = tree(module)
    original['resizer.go'] = replacement
    if original != tree(output / 'module'):
        raise ValueError('module changed during staging')
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--module', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--replace-path', required=True)
    parser.add_argument('--go', required=True, type=Path)
    args = parser.parse_args()
    if not args.go.is_absolute():
        raise ValueError('Go path must be absolute')
    env = os.environ.copy()
    env.update({'GOWORK': 'off', 'GOFLAGS': '-mod=readonly', 'GO111MODULE': 'on'})
    command = ['/bin/sh', '-c', 'cd "$1" && shift && exec "$@"', 'go', str(args.source)]
    cache = reference.bounded_run(command + [str(args.go), 'env', 'GOMODCACHE'], env=env, timeout=30, limit=4096)
    expected = Path(cache.decode('utf-8').strip()) / 'github.com/h2non/bimg@v1.1.4'
    if not expected.is_absolute() or args.module.absolute() != expected.absolute():
        raise ValueError('module cache path mismatch')
    reference.bounded_run(command + [str(args.go), 'mod', 'verify'], env=env, timeout=300, limit=65536)
    stage(args.source, args.module, args.output, args.replace_path)
    reference.bounded_run(command + [str(args.go), 'mod', 'verify'], env=env, timeout=300, limit=65536)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, UnicodeError, reference.InventoryError):
        print('bimg patch staging failed', file=sys.stderr)
        sys.exit(1)
