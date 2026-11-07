#!/usr/bin/env python3
"""Prepare verified inputs for the offline runtime image in GitHub Actions."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import time
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
spec = importlib.util.spec_from_file_location('runtime_closure', HERE / 'closure.py')
closure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(closure)
discovery = closure.discovery
BASE = 'docker.io/library/debian@sha256:' + discovery.BASE.removeprefix('sha256:')
BUILDKIT = 'docker.io/moby/buildkit@sha256:8c8514715aab54e12f65b6a38a219084ab926d49c52d519ac17a8e79befb9c75'
FRONTEND = 'docker.io/docker/dockerfile@sha256:dc9e236567481e0aca4c1f52351af213b9a176622f10e3f4a86e5cc48919fa01'
BUILDX_SHA256 = 'b13bee81c3db12a4be7d0b9d042b64d0dd9ed116f7674dfac0ffdf2a71acfe3d'
GO_SHA256 = 'ff445e48af27f93f66bd949ae060d97991c83e11289009d311f25426258f9c44'
VIPS_SHA256 = '1299d3a98213ae477f0180817ec79afd36803a5b8434f48068e75a721d23b6f9'
RECIPE_SHA256 = '60301c99211a8774a4933c50c49d91f0b017cc06429b07f57088f1a0e0b26338'
VIPS_URL = 'https://github.com/libvips/libvips/releases/download/v8.10.0/vips-8.10.0.tar.gz'
CONFIGURE = ['--disable-debug', '--disable-dependency-tracking', '--disable-introspection',
             '--disable-static', '--enable-gtk-doc-html=no', '--enable-gtk-doc=no', '--enable-pyvips8=no']


def verify_blob(data, size, sha256):
    if type(size) is not int or size < 1 or len(data) != size:
        raise ValueError('payload size mismatch')
    if not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
        raise ValueError('invalid payload digest')
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError('payload digest mismatch')
    return data


def validate_tools(tools):
    if not isinstance(tools, dict) or tools.get('buildkit_image') != BUILDKIT or tools.get('frontend_image') != FRONTEND:
        raise ValueError('missing or unexpected tool image pin')
    buildx = tools.get('buildx')
    if (not isinstance(buildx, dict) or buildx.get('sha256') != BUILDX_SHA256
            or type(buildx.get('size')) is not int or not 0 < buildx['size'] <= 128 * 1024 * 1024
            or buildx.get('url') != 'https://github.com/docker/buildx/releases/download/v0.21.2/buildx-v0.21.2.linux-amd64'):
        raise ValueError('missing or unexpected buildx tool pin')


def validate_lock_result(status, data):
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('invalid lock validator output') from exc
    if (status, value) not in ((1, {'valid': True, 'unresolved': ['features']}),
                               (0, {'valid': True, 'unresolved': []})):
        raise ValueError('lock incomplete or feature status not explicit')


def validate_lock_cli(validator, path):
    data = run_bounded(['/bin/sh', '-c', '"$1" --json "$2"; code=$?; printf "\\nexit:%s\\n" "$code"',
                        'validator', str(validator), str(path)], env=git_environment(), timeout=30, limit=65536)
    try:
        body, marker = data.rsplit(b'\nexit:', 1)
        status = int(marker.strip())
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid lock validator response') from exc
    validate_lock_result(status, body)


def validate_edges(rows, edges, compare):
    if closure.resolve_edges(rows, compare) != edges:
        raise ValueError('dependency edges differ from authenticated packages')


def validate_closure(document, indexes, lock=None, compare=None):
    if not isinstance(document, dict) or document.get('schema_version') != 1 or document.get('platform') != 'linux/amd64' or document.get('base_manifest') != discovery.BASE or lock is None:
        raise ValueError('invalid closure')
    result = {}
    for kind in ('build', 'runtime'):
        try:
            part = document['closures'][kind]
            post = [tuple(row) for row in part['post']]
            base = [tuple(row) for row in part['base']]
            if part['pre'] != part['base'] or part['roots'] != lock[kind + '_roots']:
                raise ValueError('closure roots or pre-inventory mismatch')
            rows = closure.match_installed(post, indexes)
            closure.match_installed(base, indexes)
            given = part['packages']
            if len(rows) != len(given) or not rows:
                raise ValueError('closure package count mismatch')
            by_name = {row['package']['Package']: row for row in given}
            locks = {row['name']: row for row in lock[kind + '_packages']}
            if len(by_name) != len(rows) or len(locks) != len(rows):
                raise ValueError('closure duplicate package')
            for row in rows:
                pkg = row['package']
                name = pkg['Package']
                actual = by_name[name]
                expected_url = closure.archive_root(row['repository']) + pkg['Filename']
                if (actual['repository'] != row['repository'] or actual['package'] != pkg
                        or actual['artifact_url'] != expected_url or actual['relations'] != closure.relations(pkg)):
                    raise ValueError('closure package differs from signed index')
                item = locks[name]
                identity = (name, pkg['Version'], pkg['Architecture'])
                expected = (name, item['version'], item['architecture'])
                if (identity != expected or item['repository'] != row['repository']
                        or item['url'] != expected_url or item['size'] != int(pkg['Size'])
                        or item['sha256'] != pkg['SHA256']
                        or item['provenance'] != ('base' if identity in base else 'snapshot')):
                    raise ValueError('closure differs from lock')
            edges = part['dependency_edges']
            if compare is None:
                raise ValueError('missing Debian version comparator')
            validate_edges(rows, edges, compare)
            for row in rows:
                name = row['package']['Package']
                dependencies = sorted({edge['provider']['name'] for edge in edges if edge['source']['name'] == name})
                if dependencies != sorted(locks[name]['dependencies']):
                    raise ValueError('closure dependencies differ from lock')
            result[kind] = (rows, post)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('invalid closure: ' + str(exc)) from exc
    return result


def install_script(roots, post):
    for root in roots:
        if not re.fullmatch('[a-z0-9][a-z0-9+.-]*', root):
            raise ValueError('invalid root')
    lines = []
    for name, version, arch in sorted(post):
        if not re.fullmatch('[a-z0-9][a-z0-9+.-]*', name) or not re.fullmatch('[A-Za-z0-9.+:~_-]+', version) or arch not in ('all', 'amd64'):
            raise ValueError('invalid inventory')
        lines.append(f'{name}\t{version}\t{arch}')
    return ('#!/bin/sh\nset -eu\n( ' + closure.CONFIGURE + ' ) > /tmp/apt-result\n'
            + "test \"$(wc -l < /tmp/apt-result)\" -eq 1\n"
            + "grep -qx 'APT_RESULT=ok' /tmp/apt-result\n"
            + 'rm /tmp/apt-result\nmkdir -p /var/cache/apt/archives\nchmod 755 /var/cache/apt/archives\n'
            + 'cp /mirror/debs/*.deb /var/cache/apt/archives/\n'
            + 'apt-get ' + closure.APT_OPTIONS + ' --no-download --no-install-recommends -y install ' + ' '.join(roots) + '\n'
            + 'apt-get check\n'
            + "dpkg-query -W -f='${db:Status-Abbrev}\\t${binary:Package}\\t${Version}\\t${Architecture}\\n' "
            + "| awk -F '\\t' '$1 == \"ii \" {sub(/:.*/,\"\",$2); print $2 \"\\t\" $3 \"\\t\" $4}' | LC_ALL=C sort > /tmp/actual\n"
            + "cat > /tmp/expected <<'INVENTORY'\n" + '\n'.join(lines) + '\nINVENTORY\n'
            + 'diff -u /tmp/expected /tmp/actual\n')


def normalize_times(path, epoch):
    if type(epoch) is not int or epoch < 0:
        raise ValueError('invalid source timestamp')
    root = Path(path)
    for base, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in dirs + files:
            os.utime(Path(base) / name, (epoch, epoch), follow_symlinks=False)
        os.utime(base, (epoch, epoch), follow_symlinks=False)


def git_environment():
    return {'PATH': os.defpath, 'HOME': '/nonexistent', 'LC_ALL': 'C',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}


def run_bounded(args, *, env, timeout=60, limit=2 * 1024 * 1024):
    end = getattr(closure, '_OVERALL_END', time.monotonic() + timeout)
    remaining = min(timeout, end - time.monotonic())
    if remaining <= 0:
        raise TimeoutError('overall preparation deadline exceeded')
    return closure.reference.bounded_run(args, timeout=remaining, limit=limit, env=env)


def go_environment(goroot, temp):
    return {'PATH': os.defpath, 'HOME': str(temp), 'GOROOT': str(goroot),
            'GOCACHE': str(temp / 'gocache'), 'GOMODCACHE': str(temp / 'modules'),
            'GOPROXY': 'https://proxy.golang.org', 'GOSUMDB': 'sum.golang.org',
            'GOFLAGS': '-mod=readonly', 'GO111MODULE': 'on', 'GOWORK': 'off',
            'CGO_ENABLED': '0', 'LC_ALL': 'C'}


def validate_ci_identity(env, commit):
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_SHA') != commit
            or env.get('GITHUB_REPOSITORY', '').lower() != 'ogi4i/imaginary'
            or env.get('GITHUB_REF') != 'refs/heads/mitigate-native-memory'):
        raise ValueError('CI identity mismatch')


def validate_attributes(source):
    for root, dirs, files in os.walk(source, followlinks=False):
        if '.gitattributes' in files:
            text = (Path(root) / '.gitattributes').read_text(encoding='utf-8')
            if re.search(r'\bexport-(?:ignore|subst)\b', text):
                raise ValueError('unsafe git archive attribute')


def hash_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_lock_source(consumed, source):
    archived = source / 'docs/runtime-image.lock.yaml'
    if consumed.stat().st_size > 1024 * 1024 or consumed.read_bytes() != archived.read_bytes():
        raise ValueError('consumed lock differs from archived source lock')
    return hash_file(archived)


def context_manifest(root):
    rows = []
    total = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(base) / name).is_symlink():
                raise ValueError('context directory symlink')
        for name in files:
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('unsafe context file')
            size = path.stat().st_size
            total += size
            if total > 8 * 1024**3 or len(rows) >= 200000:
                raise ValueError('context manifest limit')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            rows.append({'path': path.relative_to(root).as_posix(), 'size': size,
                         'mode': path.stat().st_mode & 0o777, 'sha256': digest.hexdigest()})
    rows.sort(key=lambda item: item['path'])
    return rows


def validate_modules(source, config):
    for name in ('go.mod', 'go.sum'):
        if hash_file(source / name) != config[name.replace('.', '_') + '_sha256']:
            raise ValueError('module file hash mismatch')
    text = (source / 'go.mod').read_text(encoding='utf-8')
    if re.search(r'(?m)^\s*(replace|toolchain|godebug|work)\b', text):
        raise ValueError('module replace or unsupported directive')


def require_clean_source(source):
    if run_bounded(['git', '-C', str(source), 'status', '--porcelain=v1', '-z', '--untracked-files=no'], env=git_environment(), timeout=30):
        raise ValueError('source must be clean')


def validate_recipe(text):
    if hashlib.sha256(text.encode('utf-8')).hexdigest() != RECIPE_SHA256:
        raise ValueError('recipe does not match reviewed build settings')
    if not text.startswith('# syntax=' + FRONTEND.replace('docker/dockerfile@', 'docker/dockerfile:1.9.0@') + '\n'):
        raise ValueError('recipe frontend pin mismatch')
    from_lines = [line for line in text.splitlines() if line.startswith('FROM ')]
    if len(from_lines) != 4 or sum(discovery.BASE in line for line in from_lines) != 2 or any(':latest' in line for line in from_lines):
        raise ValueError('recipe base pin mismatch')


def output_path(output, source):
    parent = output.parent.resolve(strict=True)
    target = parent / output.name
    if target.exists() or target.is_symlink() or target == source or source in target.parents:
        raise ValueError('output must be a new path outside source')
    return target


def validate_base_inventory(evidence, actual):
    for kind in ('build', 'runtime'):
        if [tuple(row) for row in evidence['closures'][kind]['base']] != actual:
            raise ValueError('base inventory mismatch')


def copy_source(source, destination, commit, tree):
    if not re.fullmatch('[0-9a-f]{40}', commit) or not re.fullmatch('[0-9a-f]{40}', tree):
        raise ValueError('invalid source identity')
    require_clean_source(source)
    head = run_bounded(['git', '-C', str(source), 'rev-parse', 'HEAD'], env=git_environment(), timeout=30).decode().strip()
    actual_tree = run_bounded(['git', '-C', str(source), 'rev-parse', commit + '^{tree}'], env=git_environment(), timeout=30).decode().strip()
    if head != commit or actual_tree != tree:
        raise ValueError('source commit or tree mismatch')
    manifest = run_bounded(['git', '-C', str(source), 'ls-tree', '-rz', '--full-tree', commit], env=git_environment(), timeout=30, limit=16 * 1024 * 1024)
    entries = {}
    for record in manifest.split(b'\0'):
        if not record:
            continue
        header, name = record.split(b'\t', 1)
        mode, kind, digest = header.split(b' ')
        if mode not in (b'100644', b'100755') or kind != b'blob' or name in entries:
            raise ValueError('unsupported tracked source entry')
        entries[name] = digest.decode('ascii')
    if not entries:
        raise ValueError('empty source tree')
    data = run_bounded(['git', '-C', str(source), 'archive', '--format=tar', commit], env=git_environment(), timeout=60, limit=256 * 1024 * 1024)
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        members = archive.getmembers()
        if len(members) > 100000:
            raise ValueError('source archive member limit')
        for entry in members:
            parts = Path(entry.name).parts
            if not parts or entry.name.startswith('/') or '..' in parts or not (entry.isfile() or entry.isdir()):
                raise ValueError('unsafe source archive')
        files = [entry for entry in members if entry.isfile()]
        if len(files) != len(entries):
            raise ValueError('archive differs from source tree')
        seen = set()
        for entry in files:
            name = os.fsencode(entry.name)
            if name not in entries or name in seen:
                raise ValueError('archive differs from source tree')
            seen.add(name)
            stream = archive.extractfile(entry)
            if stream is None:
                raise ValueError('archive file missing')
            digest = hashlib.sha1(b'blob ' + str(entry.size).encode() + b'\0')
            while chunk := stream.read(65536):
                digest.update(chunk)
            if digest.hexdigest() != entries[name]:
                raise ValueError('archive blob mismatch')
        archive.extractall(destination, filter='data')
    validate_attributes(destination)


def overall_remaining(maximum):
    remaining = min(maximum, getattr(closure, '_OVERALL_END', time.monotonic() + maximum) - time.monotonic())
    if remaining <= 0:
        raise TimeoutError('overall preparation deadline exceeded')
    return remaining


def fetch_github(url, limit):
    approved = (VIPS_URL, 'https://github.com/docker/buildx/releases/download/v0.21.2/buildx-v0.21.2.linux-amd64')
    if url not in approved:
        raise ValueError('unapproved URL')

    class Redirect(urllib.request.HTTPRedirectHandler):
        max_redirections = 5
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urllib.parse.urlsplit(newurl)
            if target.scheme != 'https' or target.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com') or target.username or target.password or target.port not in (None, 443):
                raise ValueError('unapproved redirect URL')
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    request = urllib.request.Request(url)
    with closure.deadline(overall_remaining(120)), urllib.request.build_opener(Redirect()).open(request, timeout=overall_remaining(60)) as response:
        data = response.read(limit + 1)
    overall_remaining(120)
    if len(data) > limit:
        raise ValueError('download size limit')
    return data


def extract_go(data, dest):
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = archive.getmembers()
        if len(members) > 100000:
            raise ValueError('archive member limit')
        for entry in members:
            parts = Path(entry.name).parts
            if not parts or parts[0] != 'go' or any(x in ('.', '..') for x in parts) or entry.name.startswith('/') or not (entry.isfile() or entry.isdir()):
                raise ValueError('unsafe Go archive member')
        archive.extractall(dest, filter='data')


def compare_versions(left, operator, right):
    if not all(re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,200}', value) for value in (left, right)) or operator not in ('<<', '<=', '=', '>=', '>>'):
        raise ValueError('invalid version comparator')
    try:
        run_bounded(['dpkg', '--compare-versions', left, operator, right], env=git_environment(), timeout=10)
        return True
    except closure.reference.ProbeExit as exc:
        if exc.code == 1:
            return False
        raise


def remove_owned_container(name, env):
    if not re.fullmatch(r'runtime-build-base-[a-z0-9]+', name):
        raise ValueError('invalid owned container name')
    old_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    started = time.monotonic()
    try:
        closure.reference.bounded_run(['docker', 'rm', '-f', name], env=env, timeout=30, limit=65536)
        remaining = closure.reference.bounded_run(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'], env=env, timeout=30, limit=65536)
        if remaining.strip():
            raise ValueError('owned container cleanup failed')
    finally:
        if old_timer[0]:
            remaining = max(0.001, old_timer[0] - (time.monotonic() - started))
            signal.setitimer(signal.ITIMER_REAL, remaining, old_timer[1])


def measure_base(temp, metadata):
    name = 'runtime-build-base-' + os.urandom(12).hex()
    env = {'PATH': os.defpath, 'HOME': str(temp), 'DOCKER_CONFIG': str(temp / 'docker-config'),
           'DOCKER_HOST': 'unix:///var/run/docker.sock', 'LC_ALL': 'C'}
    def run(args, timeout):
        return run_bounded(args, env=env, timeout=timeout)
    run(['docker', 'pull', '--platform=linux/amd64', BASE], 300)
    image_id = closure.image_identity(run(['docker', 'image', 'inspect', BASE], 30), metadata)
    try:
        run(['docker', 'create', '--name', name, '--platform=linux/amd64', '--network=none',
             '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=128',
             '--memory=256m', '--cpus=1', '--pull=never', '--entrypoint=/bin/sh', image_id,
             '-c', closure.DPKG_QUERY], 60)
        data = run(['docker', 'start', '-a', name], 60)
        return closure.parse_inventory(data)
    finally:
        with closure.cleanup_stage('none'):
            remove_owned_container(name, env)


def prepare(lock, evidence, metadata, keyring, output, go, vips, validator, modules, source, actual_base):
    validate_tools(lock['build_tools'])
    if lock['platform'] != 'linux/amd64' or lock['builder_image'] != BASE or lock['runtime_image'] != BASE:
        raise ValueError('unexpected platform or base')
    config = lock['build']
    if (config['configure'] != CONFIGURE or config['cflags'] != ['-g', '-O3']
            or config['cxxflags'] != ['-D_GLIBCXX_USE_CXX11_ABI=0', '-g', '-O3']
            or config['go_flags'] != ['-trimpath'] or config['module_mode'] != 'readonly'):
        raise ValueError('unexpected build configuration')
    validate_lock_cli(validator, lock['_path'])
    for name in ('go.mod', 'go.sum'):
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != config[name.replace('.', '_') + '_sha256']:
            raise ValueError('module file hash mismatch')
    tool = lock['go_toolchain']
    if tool != {'version': '1.20.14', 'url': 'https://dl.google.com/go/go1.20.14.linux-amd64.tar.gz', 'size': 100448992, 'sha256': GO_SHA256}:
        raise ValueError('unexpected Go pin')
    verify_blob(go.read_bytes(), tool['size'], GO_SHA256)
    blob = vips.read_bytes()
    if lock['libvips'] != {'version': '8.10.0', 'archive_url': VIPS_URL, 'archive_sha256': VIPS_SHA256} or hashlib.sha256(blob).hexdigest() != VIPS_SHA256:
        raise ValueError('vips digest mismatch')
    if output.exists():
        raise ValueError('output already exists')
    if lock['snapshot']['date'] != '20240408T000000Z':
        raise ValueError('unexpected snapshot date')
    for name in ('main', 'security'):
        item, recorded = lock['snapshot'][name], metadata['snapshot'][name]
        for key in ('inrelease', 'packages'):
            if item[key]['url'] != recorded[key + '_url'] or item[key]['sha256'] != recorded[key + '_sha256']:
                raise ValueError('snapshot lock mismatch')
        with closure.deadline(overall_remaining(60)):
            payload = discovery.fetch(item['inrelease']['url'], limit=item['inrelease']['size'])
        overall_remaining(60)
        verify_blob(payload, item['inrelease']['size'], item['inrelease']['sha256'])
        if item['packages']['size'] != recorded['packages_size']:
            raise ValueError('snapshot size mismatch')
        key_info = item['keyring']
        if key_info['image'] != BASE or key_info['path'] != '/usr/share/keyrings/debian-archive-keyring.gpg' or keyring.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('invalid keyring identity')
        verify_blob(keyring.read_bytes(), key_info['size'], key_info['sha256'])
        if item['signing_key_fingerprint'].upper() != discovery.SIGNERS[name]:
            raise ValueError('snapshot signer mismatch')
        expected = discovery.RELEASES[name]
        release = item['release_identity']
        for field, key in [('origin', 'Origin'), ('label', 'Label'), ('codename', 'Codename'), ('suite', 'Suite'), ('date', 'Date'), ('valid_until', 'Valid-Until')]:
            if release[field] != expected[key]:
                raise ValueError('snapshot release mismatch')
    indexes = closure.authenticated_indexes(metadata, keyring)
    validate_base_inventory(evidence, actual_base)
    packages = validate_closure(evidence, indexes, lock, compare_versions)
    if sum(int(row['package']['Size']) for rows, _ in packages.values() for row in rows) > 4 * 1024**3:
        raise ValueError('aggregate package download limit')
    output.mkdir()
    (output / 'inputs').mkdir()
    shutil.copyfile(go, output / 'inputs/go.tar.gz')
    shutil.copyfile(vips, output / 'inputs/vips.tar.gz')
    shutil.copytree(source, output / 'source')
    shutil.copyfile(source / 'Dockerfile', output / 'Dockerfile')
    shutil.copytree(modules, output / 'inputs/modules', symlinks=False)
    for kind in ('build', 'runtime'):
        dest = output / 'inputs' / kind
        mirror = dest / 'mirror'
        mirror.mkdir(parents=True)
        closure.write_mirror(mirror, metadata['snapshot'])
        debs = mirror / 'debs'
        debs.mkdir()
        rows, post = packages[kind]
        for row in rows:
            package = row['package']
            data = closure.official_fetch(closure.archive_root(row['repository']) + package['Filename'], limit=int(package['Size']))
            closure.verify_deb(package, data)
            name = package['Package'] + '_' + package['Version'].replace(':', '%3a') + '_' + package['Architecture'] + '.deb'
            (debs / name).write_bytes(data)
        (dest / 'install.sh').write_text(install_script(lock[kind + '_roots'], post))
    normalize_times(output, config['source_date_epoch'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', required=True, type=Path)
    parser.add_argument('--closure', required=True, type=Path)
    parser.add_argument('--discovery', required=True, type=Path)
    parser.add_argument('--keyring', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tree', required=True)
    parser.add_argument('--recipe-sha256', required=True)
    parser.add_argument('--workflow', required=True)
    parser.add_argument('--workflow-sha256', required=True)
    args = parser.parse_args()
    closure.require_ci()
    validate_ci_identity(os.environ, args.commit)
    closure._OVERALL_END = time.monotonic() + 3600
    source = args.source.resolve(strict=True)
    output = output_path(args.output, source)
    lock_path = args.lock.resolve(strict=True)
    lock = json.loads(lock_path.read_bytes())
    lock['_path'] = lock_path
    validate_tools(lock['build_tools'])
    go_pin = lock['go_toolchain']
    if go_pin != {'version': '1.20.14', 'url': 'https://dl.google.com/go/go1.20.14.linux-amd64.tar.gz', 'size': 100448992, 'sha256': GO_SHA256}:
        raise ValueError('unexpected Go pin')
    metadata = closure.read_discovery(args.discovery)
    if args.closure.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('closure exceeds limit')
    evidence = json.loads(args.closure.read_bytes())
    with tempfile.TemporaryDirectory(prefix='runtime-build-', dir=output.parent) as temp:
        temp = Path(temp)
        source_copy = temp / 'source'
        copy_source(source, source_copy, args.commit, args.tree)
        lock_digest = validate_lock_source(lock_path, source_copy)
        lock_path = source_copy / 'docs/runtime-image.lock.yaml'
        lock = json.loads(lock_path.read_bytes())
        lock['_path'] = lock_path
        validate_tools(lock['build_tools'])
        recipe = source_copy / 'Dockerfile'
        workflow = Path(args.workflow)
        if workflow.is_absolute() or '..' in workflow.parts or not args.workflow.startswith('.github/workflows/'):
            raise ValueError('invalid workflow path')
        if hash_file(recipe) != args.recipe_sha256 or hash_file(source_copy / workflow) != args.workflow_sha256:
            raise ValueError('recipe or workflow identity mismatch')
        validate_recipe(recipe.read_text())
        validate_modules(source_copy, lock['build'])
        with closure.deadline(overall_remaining(120)):
            go_blob = discovery.fetch(go_pin['url'], limit=go_pin['size'])
        overall_remaining(120)
        verify_blob(go_blob, go_pin['size'], GO_SHA256)
        go_file = temp / 'go.tar.gz'
        go_file.write_bytes(go_blob)
        goroot = temp / 'goroot'
        goroot.mkdir()
        extract_go(go_blob, goroot)
        go_exe = goroot / 'go/bin/go'
        vips_blob = fetch_github(VIPS_URL, 64 * 1024 * 1024)
        if hashlib.sha256(vips_blob).hexdigest() != VIPS_SHA256:
            raise ValueError('vips digest mismatch')
        vips_file = temp / 'vips.tar.gz'
        vips_file.write_bytes(vips_blob)
        buildx_pin = lock['build_tools']['buildx']
        verify_blob(fetch_github(buildx_pin['url'], buildx_pin['size']), buildx_pin['size'], buildx_pin['sha256'])
        env = go_environment(goroot / 'go', temp)
        validator = temp / 'source-lock'
        run_bounded(['/bin/sh', '-c', 'cd "$1" && shift && exec "$@"', 'go', str(source_copy), str(go_exe), 'build', '-o', str(validator), './cmd/source-lock'], env=env, timeout=120)
        validate_modules(source_copy, lock['build'])
        validate_lock_cli(validator, lock_path)
        before = {name: (source_copy / name).read_bytes() for name in ('go.mod', 'go.sum')}
        run_bounded(['/bin/sh', '-c', 'cd "$1" && shift && exec "$@"', 'go', str(source_copy), str(go_exe), 'mod', 'download'], env=env, timeout=600)
        validate_modules(source_copy, lock['build'])
        run_bounded(['/bin/sh', '-c', 'cd "$1" && shift && exec "$@"', 'go', str(source_copy), str(go_exe), 'mod', 'verify'], env=env, timeout=300)
        validate_modules(source_copy, lock['build'])
        if any((source_copy / name).read_bytes() != data for name, data in before.items()):
            raise ValueError('module files changed')
        actual_base = measure_base(temp, metadata)
        staged = temp / 'context'
        prepare(lock, evidence, metadata, args.keyring.resolve(strict=True), staged, go_file, vips_file, validator, temp / 'modules', source_copy, actual_base)
        manifest = context_manifest(staged)
        manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
        if len(manifest_bytes) > 32 * 1024 * 1024:
            raise ValueError('context manifest size limit')
        (staged / 'context-manifest.json').write_bytes(manifest_bytes)
        provenance = {'commit': args.commit, 'tree': args.tree, 'recipe_sha256': hash_file(staged / 'Dockerfile'),
                      'workflow_path': args.workflow, 'workflow_sha256': hash_file(staged / 'source' / workflow),
                      'lock_sha256': lock_digest, 'context_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                      'qualification': 'unqualified', 'features': lock['features']}
        (staged / 'provenance.json').write_text(json.dumps(provenance, sort_keys=True) + '\n')
        os.utime(staged / 'context-manifest.json', (lock['build']['source_date_epoch'],) * 2)
        os.utime(staged / 'provenance.json', (lock['build']['source_date_epoch'],) * 2)
        os.utime(staged, (lock['build']['source_date_epoch'],) * 2)
        if output.exists() or output.is_symlink():
            raise ValueError('output already exists')
        os.rename(staged, output)
    print('Context prepared; feature equivalence remains unqualified.')


if __name__ == '__main__':
    with closure.deadline(3600):
        main()
