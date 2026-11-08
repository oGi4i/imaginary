#!/usr/bin/env python3
"""Collect the fixed public reference inventory on GitHub Actions only.

Usage: python3 tooling/runtime-image/reference_inventory.py --output PATH
The image, platform and probes are fixed; no container command is user supplied.
Docker uses an empty temporary client configuration and the local Unix socket.
Each command has a deadline and a 1 MiB combined-output limit; containers have
no network, credentials or host mounts. Missing optional tools produce an
incomplete inventory, not evidence of loader parity. This does not establish
publisher provenance or authorize running the Imaginary service.
"""
import argparse
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

IMAGE = 'docker.io/h2non/imaginary@sha256:7facb4221047a5e79b9e902f380247f4e5bf4376400d0badbeb738d3e1c2f654'
CONFIG = 'sha256:ccc2df4fa965458678f796ef7d2af7ac2752a5b9d564bbab9d7abadac5eb7804'
LIMIT = 1024 * 1024
NATIVE_MAX_ROWS = 10000
NATIVE_ROOT = '/usr/local/lib'
PROBES = {
    'packages': "command -v dpkg-query >/dev/null || exit 127; dpkg-query -W -f='${db:Status-Abbrev}\\t${binary:Package}\\t${Version}\\t${Architecture}\\n'",
    'libc': 'command -v getconf >/dev/null || exit 127; getconf GNU_LIBC_VERSION',
    'vips_version': 'command -v vips >/dev/null || exit 127; vips --version',
    'vips_config': 'command -v vips >/dev/null || exit 127; vips --vips-config',
    'loaders': 'command -v vips >/dev/null || exit 127; vips -l foreign',
    'elf': 'command -v readelf >/dev/null || exit 127; readelf -d /usr/local/bin/imaginary',
    'vips_elf': 'command -v readelf >/dev/null || exit 127; test -f /usr/local/lib/libvips.so.42 || exit 127; readelf -d /usr/local/lib/libvips.so.42',
    'dependencies': 'command -v ldd >/dev/null || exit 127; ldd /usr/local/bin/imaginary',
    'vips_dependencies': 'command -v ldd >/dev/null || exit 127; test -f /usr/local/lib/libvips.so.42 || exit 127; ldd /usr/local/lib/libvips.so.42',
    'native_files': 'command -v find >/dev/null || exit 127; command -v sha256sum >/dev/null || exit 127; find /usr/local/lib -type f -exec sha256sum {} +',
    'native_symlinks': "command -v find >/dev/null || exit 127; find /usr/local/lib -type l -printf '%p\\t%l\\n'",
}


class InventoryError(Exception):
    pass


class CommandError(InventoryError):
    pass


class ProbeExit(CommandError):
    def __init__(self, code):
        super().__init__('command exited nonzero')
        self.code = code


def bounded_run(argv, *, timeout=60, limit=LIMIT, env):
    """Capture bounded combined output and always reap the client process."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, start_new_session=True)
    except OSError as exc:
        raise CommandError('command unavailable') from exc
    selector = None
    finished = False
    try:
        selector = selectors.DefaultSelector()
        data = bytearray()
        deadline = time.monotonic() + timeout
        selector.register(proc.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommandError('command timed out')
            events = selector.select(min(remaining, 0.25))
            if events:
                chunk = os.read(proc.stdout.fileno(), min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise CommandError('command output exceeded limit')
            elif proc.poll() is not None:
                # Read the pipe through EOF, including output after exit.
                continue
        try:
            code = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise CommandError('command timed out') from exc
        finished = True
        if code != 0:
            raise ProbeExit(code)
        return bytes(data)
    except OSError as exc:
        raise CommandError('command supervision failed') from exc
    finally:
        close_error = None
        if selector is not None:
            try:
                selector.close()
            except OSError as exc:
                close_error = exc
        kill_error = None
        try:
            if not finished:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    kill_error = exc
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise CommandError('command reap failed') from exc
            if kill_error:
                raise CommandError('command group cleanup failed') from kill_error
            if close_error:
                raise CommandError('selector cleanup failed') from close_error
        finally:
            proc.stdout.close()


def require_ci():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise InventoryError('GitHub Actions required')


def identity(data):
    try:
        items = json.loads(data)
        if not isinstance(items, list) or len(items) != 1:
            raise ValueError()
        item = items[0]
        # Docker may normalize docker.io to its canonical repository name.
        digests = item['RepoDigests']
        valid_digest = isinstance(digests, list) and (
            IMAGE in digests or IMAGE.removeprefix('docker.io/') in digests)
        if (item['Id'] != CONFIG or item['Os'] != 'linux' or item['Architecture'] != 'amd64'
                or not valid_digest or item['Config'].get('Volumes')):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InventoryError('reference image identity mismatch') from exc
    return {'manifest': IMAGE, 'config': CONFIG, 'os': 'linux', 'architecture': 'amd64'}


def parse_packages(data):
    try:
        lines = data.decode('ascii').splitlines()
        packages = []
        for line in lines:
            status, name, version, arch = line.split('\t')
            if not re.fullmatch(r'[a-z0-9][a-z0-9+.:_-]{0,199}', name):
                raise ValueError()
            if not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,200}', version):
                raise ValueError()
            if not re.fullmatch(r'[a-z0-9-]{1,40}', arch) or not re.fullmatch(r'[a-zA-Z? ]{3}', status):
                raise ValueError()
            if status.startswith('ii'):
                packages.append({'name': name, 'version': version, 'architecture': arch})
        if not packages or len(packages) > 10000 or len({(p['name'], p['architecture']) for p in packages}) != len(packages):
            raise ValueError()
        return sorted(packages, key=lambda p: (p['name'], p['architecture']))
    except (UnicodeError, ValueError) as exc:
        raise InventoryError('invalid package inventory') from exc


def native_path(path):
    prefix = NATIVE_ROOT + '/'
    if not path.startswith(prefix):
        return False
    parts = path[len(prefix):].split('/')
    return all(re.fullmatch(r'[A-Za-z0-9_+.-]{1,200}', part)
               and part not in ('.', '..') for part in parts)


def parse_native(name, data):
    try:
        if len(data) > LIMIT or not data.endswith(b'\n'):
            raise ValueError()
        lines = data.decode('ascii').split('\n')[:-1]
        if not lines or len(lines) > NATIVE_MAX_ROWS:
            raise ValueError()
        rows = []
        paths = set()
        for line in lines:
            if name == 'native_files':
                match = re.fullmatch(r'([0-9a-fA-F]{64})  (.+)', line)
                if not match:
                    raise ValueError()
                digest, path = match.groups()
                row = {'path': path, 'sha256': digest.lower()}
            else:
                path, target = line.split('\t')
                if not (native_path(target) if target.startswith('/') else
                        re.fullmatch(r'[A-Za-z0-9_+.-]{1,200}', target)
                        and target not in ('.', '..')):
                    raise ValueError()
                row = {'path': path, 'target': target}
            if not native_path(path) or path in paths:
                raise ValueError()
            paths.add(path)
            rows.append(row)
        if name == 'native_files':
            if not any(re.fullmatch(r'/usr/local/lib/libvips\.so\.42\.[0-9]+(?:\.[0-9]+)*', path)
                       for path in paths):
                raise ValueError()
        elif '/usr/local/lib/libvips.so.42' not in paths:
            raise ValueError()
        return sorted(rows, key=lambda row: row['path'])
    except (UnicodeError, ValueError) as exc:
        raise InventoryError('invalid probe output') from exc


def parse_probe(name, data):
    if name in ('native_files', 'native_symlinks'):
        return parse_native(name, data)
    try:
        text = data.decode('ascii').strip()
        if not text or len(text) > LIMIT:
            raise ValueError()
        if name == 'libc':
            match = re.fullmatch(r'(?:glibc )?([0-9]+(?:\.[0-9]+)+)', text)
            if not match:
                raise ValueError()
            return match.group(1)
        if name == 'vips_version':
            if not re.fullmatch(r'vips-[0-9]+(?:\.[0-9]+)+', text):
                raise ValueError()
            return text
        if name == 'vips_config':
            # Config output is not a stable public schema. Keep known boolean features only.
            allowed = {'jpeg', 'png', 'tiff', 'webp', 'svg', 'gif', 'heif', 'magick',
                       'openexr', 'pdf', 'openslide', 'orc', 'fftw', 'lcms', 'xml'}
            features = {}
            for line in text.splitlines():
                match = re.fullmatch(r'\s*([A-Za-z0-9_-]+)\s*[:=]\s*(yes|no|true|false)\s*', line, re.I)
                if match and match.group(1).lower() in allowed:
                    features[match.group(1).lower()] = match.group(2).lower() in ('yes', 'true')
            if not features:
                raise ValueError()
            return features
        if name == 'loaders':
            lines = text.splitlines()
            loaders = sorted(set(re.findall(r'\bVipsForeignLoad[A-Za-z0-9_]+\b', text)))
            if not loaders or len(lines) > 10000:
                raise ValueError()
            return loaders
        if name in ('elf', 'vips_elf'):
            needed = [line for line in text.splitlines() if '(NEEDED)' in line]
            lines = [re.fullmatch(r'\s*0x[0-9a-fA-F]+\s+\(NEEDED\)\s+Shared library: \[([A-Za-z0-9_.+\-]{1,200})\]\s*', line) for line in needed]
            if not lines or 'Dynamic section' not in text or any(match is None for match in lines):
                raise ValueError()
            return sorted(set(match.group(1) for match in lines))
        if name in ('dependencies', 'vips_dependencies'):
            result = []
            for line in text.splitlines():
                line = line.strip()
                if 'not found' in line:
                    raise ValueError()
                match = re.fullmatch(r'([A-Za-z0-9_.+\-]+) => (/[A-Za-z0-9_./+\-]+)(?: \(0x[0-9a-f]+\))?', line)
                direct = re.fullmatch(r'(/[A-Za-z0-9_./+\-]+)(?: \(0x[0-9a-f]+\))?', line)
                virtual = re.fullmatch(r'linux-vdso\.so\.1 \(0x[0-9a-f]+\)', line)
                if match:
                    result.append({'name': match.group(1), 'path': match.group(2)})
                elif direct:
                    result.append({'name': direct.group(1).rsplit('/', 1)[-1], 'path': direct.group(1)})
                elif not virtual:
                    raise ValueError()
            if not result:
                raise ValueError()
            return sorted(result, key=lambda p: (p['name'], p['path']))
    except (UnicodeError, ValueError) as exc:
        raise InventoryError('invalid probe output') from exc
    raise InventoryError('unknown probe')


def collect(run):
    run(['docker', 'pull', '--platform=linux/amd64', IMAGE], timeout=300)
    image = identity(run(['docker', 'image', 'inspect', IMAGE], timeout=30))
    result = {'schema_version': 1, 'identity': image, 'status': 'complete',
              'packages': None, 'probes': {}, 'limitations': [
                  'Inventory does not establish publisher provenance or feature parity.']}
    for name, command in PROBES.items():
        container = 'reference-inventory-' + uuid.uuid4().hex
        argv = ['docker', 'run', '--name', container, '--platform=linux/amd64',
                '--network=none', '--read-only', '--user=65534:65534', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--pids-limit=128', '--memory=512m',
                '--cpus=1', '--env=LC_ALL=C', '--pull=never', '--entrypoint=/bin/sh', CONFIG,
                '-c', command]
        try:
            try:
                output = run(argv, timeout=45)
                value = parse_packages(output) if name == 'packages' else parse_probe(name, output)
                if name == 'packages':
                    result['packages'] = value
                else:
                    result['probes'][name] = {'status': 'available', 'value': value}
            except ProbeExit as exc:
                # Docker reserves 125 and 126 for runtime/launch failures.
                if name == 'packages' or exc.code in (125, 126) or exc.code < 0:
                    raise
                result['status'] = 'incomplete'
                result['probes'][name] = {'status': 'unavailable'}
            except CommandError:
                raise
            except InventoryError:
                if name == 'packages':
                    raise
                result['status'] = 'incomplete'
                result['probes'][name] = {'status': 'unavailable'}
        finally:
            # rm -f is needed even after a killed Docker client: the daemon owns the container.
            run(['docker', 'rm', '-f', container], timeout=30)
    files = result['probes']['native_files']
    links = result['probes']['native_symlinks']
    if files['status'] == links['status'] == 'available':
        link = next(row for row in links['value']
                    if row['path'] == NATIVE_ROOT + '/libvips.so.42')
        target = link['target'] if link['target'].startswith('/') else NATIVE_ROOT + '/' + link['target']
        if (not re.fullmatch(r'/usr/local/lib/libvips\.so\.42\.[0-9]+(?:\.[0-9]+)*', target)
                or target not in {row['path'] for row in files['value']}):
            result['status'] = 'incomplete'
            result['probes']['native_symlinks'] = {'status': 'unavailable'}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        require_ci()
        with tempfile.TemporaryDirectory(prefix='reference-inventory-') as temp:
            env = {'PATH': os.defpath, 'HOME': temp, 'DOCKER_CONFIG': temp,
                   'DOCKER_HOST': 'unix:///var/run/docker.sock'}
            result = collect(lambda argv, **kw: bounded_run(argv, env=env, **kw))
        encoded = (json.dumps(result, sort_keys=True, indent=2) + '\n').encode('ascii')
        if len(encoded) > LIMIT:
            raise InventoryError('inventory exceeds output limit')
        args.output.write_bytes(encoded)
        return 0 if result['status'] == 'complete' else 2
    except (InventoryError, OSError):
        print('reference inventory failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
