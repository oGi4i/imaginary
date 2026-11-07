#!/usr/bin/env python3
"""Fail-closed helpers for matching installed packages to authenticated indexes.

The caller must authenticate index metadata before using these functions. This
module is experimental; actual CI and independent review are required before
its output can be used as lock evidence.
"""
import argparse
import importlib.util
import json
import lzma
import os
import signal
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
import re


_spec = importlib.util.spec_from_file_location('runtime_image_discovery',
                                               Path(__file__).with_name('discovery.py'))
discovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(discovery)
_spec = importlib.util.spec_from_file_location('runtime_image_reference',
                                               Path(__file__).with_name('reference_inventory.py'))
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)


class ClosureError(ValueError):
    pass


class PublishedCleanupError(ClosureError):
    pass


AUTH_REASONS = frozenset(('mismatched_snapshot', 'unexpected_index_identity',
                          'overall_deadline', 'index_parse', 'base_missing', 'base_ambiguous',
                          'base_invalid'))


APT_REASONS = frozenset(('missing_key', 'invalid_signature', 'missing_release', 'not_found',
                         'compression', 'permission', 'unsupported_method', 'unknown'))


def sanitize_apt_lines(data):
    """Retain only the explicitly approved, bounded public-build error lines."""
    result = []
    for line in data[:1024 * 1024].decode('ascii', errors='replace').splitlines():
        if not line.startswith(('E:', 'W:')):
            continue
        line = re.sub(r'[^\x20-\x7e]', '?', line)
        line = re.sub(r'\b(?:https?|file):\S+', '[url]', line, flags=re.I)
        line = re.sub(r'(?<!\w)/[^\s\'"<>]*', '[path]', line)
        line = re.sub(r'\b(?:Bearer|Basic)\s+\S+', '[credential]', line, flags=re.I)
        line = re.sub(r'\b(?:token|password|secret|authorization|signature|credential)\b\s*[:=].*',
                      '[credential]', line, flags=re.I)
        line = re.sub(r'\S*[/\\\\]\S*', '[path]', line)
        line = re.sub(r'[A-Za-z0-9_%+./=-]{24,}', '[opaque]', line)
        result.append(line[:160])
        if len(result) == 8:
            break
    return tuple(result)


class AptConfigureError(ClosureError):
    def __init__(self, reason, errors=()):
        if reason not in APT_REASONS:
            raise ValueError('invalid APT diagnostic')
        super().__init__('APT configure rejected')
        self.reason = reason
        self.errors = sanitize_apt_lines('\n'.join(errors).encode('ascii', errors='replace'))


class AuthIndexError(ClosureError):
    def __init__(self, reason):
        if reason not in AUTH_REASONS:
            raise ValueError('invalid authentication diagnostic')
        super().__init__('authenticated index rejected')
        self.reason = reason


STAGES = frozenset(('input', 'setup', 'base_pull', 'base_inspect', 'create', 'start', 'base_inventory',
                    'keyring', 'auth_index', 'mirror', 'configure', 'pre_inventory', 'solver',
                    'download', 'install', 'check', 'post', 'graph', 'cleanup', 'publish'))
KINDS = frozenset(('none', 'build', 'runtime'))
CATEGORIES = frozenset(('published_cleanup', 'command_exit', 'command_error', 'timeout',
                        'closure_error', 'invalid_data', 'io_error', 'other'))
CLEANUP_STATES = frozenset(('not_attempted', 'succeeded', 'failed'))


class DiagnosticError(Exception):
    def __init__(self, where, kind, category, code=None, cleanup='not_attempted', reason=None, errors=()):
        if (where not in STAGES or kind not in KINDS or category not in CATEGORIES
                or cleanup not in CLEANUP_STATES or (code is not None and
                (category != 'command_exit' or type(code) is not int or not 1 <= code <= 255))
                or (reason is not None and (category != 'closure_error' or not (
                    where == 'auth_index' and reason in AUTH_REASONS
                    or where == 'configure' and reason in APT_REASONS)))):
            raise ValueError('invalid diagnostic')
        super().__init__('closure operation failed')
        self.where, self.kind, self.category, self.code, self.cleanup = where, kind, category, code, cleanup
        self.reason = reason
        self.errors = (sanitize_apt_lines('\n'.join(errors).encode('ascii', errors='replace'))
                       if where == 'configure' else ())


def classify(exc):
    if type(exc) is discovery.DiscoveryError:
        # discovery.stage suppresses but retains the original exception context.
        context = exc.__context__
        if isinstance(context, TimeoutError):
            return 'timeout', None
        if isinstance(context, (ValueError, KeyError, TypeError)):
            return 'invalid_data', None
        if isinstance(context, OSError):
            return 'io_error', None
        return 'other', None
    if isinstance(exc, PublishedCleanupError):
        return 'published_cleanup', None
    if isinstance(exc, reference.ProbeExit):
        return 'command_exit', exc.code if isinstance(exc.code, int) and 1 <= exc.code <= 255 else None
    if isinstance(exc, reference.CommandError):
        return 'command_error', None
    if isinstance(exc, TimeoutError):
        return 'timeout', None
    if isinstance(exc, ClosureError):
        return 'closure_error', None
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return 'invalid_data', None
    if isinstance(exc, OSError):
        return 'io_error', None
    return 'other', None


def diagnostic_message(exc):
    message = f'closure failure stage={exc.where} kind={exc.kind} category={exc.category}'
    if exc.code is not None:
        message += f' code={exc.code}'
    if exc.reason is not None:
        message += f' reason={exc.reason}'
    message += f' cleanup={exc.cleanup}'
    if exc.errors:
        message += '\napt_errors=' + json.dumps(exc.errors, ensure_ascii=True)
    return message


@contextmanager
def stage(where, kind='none'):
    if where not in STAGES or kind not in KINDS:
        raise ValueError('invalid diagnostic stage')
    try:
        yield
    except DiagnosticError:
        raise
    except Exception as exc:
        category, code = classify(exc)
        cleanup = 'failed' if getattr(exc, '_closure_cleanup_failed', False) else 'not_attempted'
        reason = None
        if where == 'auth_index' and type(exc) is AuthIndexError and exc.reason in AUTH_REASONS:
            reason = exc.reason
        if where == 'configure' and type(exc) is AptConfigureError and exc.reason in APT_REASONS:
            reason = exc.reason
        errors = exc.errors if where == 'configure' and type(exc) is AptConfigureError else ()
        raise DiagnosticError(where, kind, category, code, cleanup, reason, errors) from exc


@contextmanager
def cleanup_stage(kind='none'):
    if kind not in KINDS:
        raise ValueError('invalid diagnostic kind')
    original = sys.exception()
    try:
        yield
    except Exception as cleanup:
        if original is not None:
            if isinstance(original, DiagnosticError):
                original.cleanup = 'failed'
                original.add_note('container cleanup failed')
                original.__context__ = cleanup
                raise original
            original.add_note('container cleanup failed')
            original._closure_cleanup_failed = True
            raise original from cleanup
        category, code = classify(cleanup)
        raise DiagnosticError('cleanup', kind, category, code, 'failed') from cleanup
    else:
        if isinstance(original, DiagnosticError):
            original.cleanup = 'succeeded'


ROOTS = {
    'build': ('ca-certificates', 'automake', 'build-essential', 'curl',
              'gobject-introspection', 'gtk-doc-tools', 'libglib2.0-dev', 'libjpeg62-turbo-dev',
              'libpng-dev', 'libwebp-dev', 'libtiff5-dev', 'libgif-dev', 'libexif-dev',
              'libxml2-dev', 'libpoppler-glib-dev', 'swig', 'libmagickwand-dev',
              'libpango1.0-dev', 'libmatio-dev', 'libopenslide-dev', 'libcfitsio-dev',
              'libgsf-1-dev', 'fftw3-dev', 'liborc-0.4-dev', 'librsvg2-dev',
              'libimagequant-dev', 'libheif-dev'),
    'runtime': ('procps', 'libglib2.0-0', 'libjpeg62-turbo', 'libpng16-16', 'libopenexr23',
                'libwebp6', 'libwebpmux3', 'libwebpdemux2', 'libtiff5', 'libgif7', 'libexif12',
                'libxml2', 'libpoppler-glib8', 'libmagickwand-6.q16-6', 'libpango1.0-0',
                'libmatio4', 'libopenslide0', 'libgsf-1-114', 'fftw3', 'liborc-0.4-0',
                'librsvg2-2', 'libcfitsio7', 'libimagequant0', 'libheif1')}

FIELDS = {name.lower(): name for name in (
    'Package', 'Version', 'Architecture', 'Filename', 'Size', 'SHA256',
    'Depends', 'Pre-Depends', 'Provides', 'Conflicts', 'Breaks', 'Replaces',
    'Recommends', 'Suggests', 'Enhances', 'Multi-Arch')}


ARCHIVES = {'main': ('debian', 'buster'), 'security': ('debian-security', 'buster/updates')}


def archive_root(name):
    return 'https://snapshot.debian.org/archive/' + ARCHIVES[name][0] + '/20240408T000000Z/'


@contextmanager
def deadline(seconds):
    def expired(signum, frame):
        raise TimeoutError('overall deadline exceeded')
    started = time.monotonic()
    old_handler = signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        remaining = max(0.001, old_timer[0] - (time.monotonic() - started)) if old_timer[0] else 0
        signal.setitimer(signal.ITIMER_REAL, remaining, old_timer[1])
        signal.signal(signal.SIGALRM, old_handler)


def official_fetch(url, *, limit):
    end = globals().get('_OVERALL_END')
    remaining = 60 if end is None else min(60, end - time.monotonic())
    if remaining <= 0:
        raise ClosureError('overall deadline exceeded')
    with deadline(remaining):
        return discovery.fetch(url, limit=limit)


def authenticated_indexes(document, keyring):
    end = globals().get('_OVERALL_END')
    remaining = 600 if end is None else min(600, end - time.monotonic())
    if remaining <= 0:
        raise AuthIndexError('overall_deadline')
    with deadline(remaining):
        metadata = discovery.snapshot_metadata(keyring)
    if document.get('snapshot') != metadata or set(metadata) != set(ARCHIVES):
        raise AuthIndexError('mismatched_snapshot')
    indexes = []
    for name, (_, dist) in ARCHIVES.items():
        info = metadata[name]
        url = archive_root(name) + 'dists/' + dist + '/main/binary-amd64/Packages.xz'
        if info['packages_url'] != url or info['packages_size'] > 32 * 1024 * 1024:
            raise AuthIndexError('unexpected_index_identity')
        blob = official_fetch(url, limit=info['packages_size'])
        try:
            index = parse_index(blob, info['packages_size'], info['packages_sha256'])
        except ClosureError as exc:
            raise AuthIndexError('index_parse') from exc
        indexes.append((name, index))
    return indexes


def require_ci():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ClosureError('GitHub Actions required')


def parse_inventory(data):
    try:
        result, names = [], set()
        for line in data.decode('ascii').splitlines():
            status, name, version, arch = line.split('\t')
            if status in ('un ', 'rc '):
                continue
            if ':' in name:
                name, qualifier = name.split(':', 1)
                if qualifier != arch:
                    raise ValueError()
            if status != 'ii ' or not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', name):
                raise ValueError()
            if not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,200}', version) or arch not in ('amd64', 'all'):
                raise ValueError()
            if name in names:
                raise ValueError()
            names.add(name)
            result.append((name, version, arch))
        if not result or len(result) > 10000:
            raise ValueError()
        return sorted(result)
    except (ValueError, UnicodeError) as exc:
        raise ClosureError('invalid dpkg inventory') from exc


def parse_solver(data):
    result, names = [], set()
    try:
        for line in data.decode('ascii').splitlines():
            if line.startswith('Remv '):
                raise ValueError()
            if not line.startswith('Inst '):
                continue
            match = re.fullmatch(r'Inst ([a-z0-9][a-z0-9+.-]*)(?: \[[^]\n]+\])? \(([A-Za-z0-9.+:~_-]{1,200}) [^\n]*\[(amd64|all)\]\)', line)
            if not match or match[1] in names:
                raise ValueError()
            names.add(match[1])
            result.append(match.groups())
        return sorted(result)
    except (ValueError, UnicodeError) as exc:
        raise ClosureError('invalid apt solver output') from exc


def parse_index(data, size, sha256, *, max_uncompressed=128 * 1024 * 1024):
    """Parse a Packages.xz whose size and hash come from authenticated metadata."""
    if (not isinstance(size, int) or isinstance(size, bool) or size != len(data)
            or not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256)):
        raise ClosureError('invalid index size or digest')
    try:
        discovery.verify_digest(data, 'sha256:' + sha256)
        if not isinstance(max_uncompressed, int) or max_uncompressed < 1:
            raise ClosureError('invalid decompression limit')
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=256 * 1024 * 1024)
        text = decoder.decompress(data, max_length=max_uncompressed + 1)
        if len(text) > max_uncompressed or not decoder.eof or decoder.unused_data:
            raise ClosureError('index exceeds limit or is incomplete')
        lines = text.decode('utf-8').splitlines()
    except (ValueError, lzma.LZMAError, UnicodeError, EOFError) as exc:
        raise ClosureError('invalid index') from exc
    records, fields, seen, last = [], {}, set(), None
    for line in lines + ['']:
        if not line:
            if seen:
                records.append(fields)
                fields, seen, last = {}, set(), None
        elif line[0] in ' \t':
            if last is None:
                raise ClosureError('orphan continuation')
            if last in FIELDS:
                fields[FIELDS[last]] += '\n' + line
        else:
            name, separator, value = line.partition(':')
            if (not separator or not name or name[0] in '#-'
                    or not re.fullmatch(r'[!-9;-~]+', name) or name.lower() in seen):
                raise ClosureError('invalid or duplicate field')
            last = name.lower()
            seen.add(last)
            if last in FIELDS:
                fields[FIELDS[last]] = value.strip(' \t')
    if not records:
        raise ClosureError('empty index')
    return records


def _validate_package(package):
    for field in ('Package', 'Version', 'Architecture', 'Filename', 'Size', 'SHA256'):
        if not isinstance(package.get(field), str) or not package[field]:
            raise ClosureError('missing package field')
    if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', package['Package']):
        raise ClosureError('invalid package name')
    if package['Architecture'] not in ('amd64', 'all'):
        raise ClosureError('invalid architecture')
    path = package['Filename']
    if not re.fullmatch(r'pool/[A-Za-z0-9+._~/\-]+\.deb', path) or any(
            part in ('', '.', '..') for part in path.split('/')):
        raise ClosureError('invalid package path')
    if (not re.fullmatch(r'[0-9]{1,19}', package['Size'])
            or not 0 < int(package['Size']) <= 2**63 - 1):
        raise ClosureError('invalid package size')
    if not re.fullmatch(r'[0-9a-f]{64}', package['SHA256']):
        raise ClosureError('invalid package hash')


class MatchError(ClosureError):
    def __init__(self, reason):
        super().__init__('installed package match rejected')
        self.reason = reason


def match_installed(installed, indexes):
    """Match exact installed versions, retaining equivalent signed origins."""
    by_key = {}
    for repository, records in indexes:
        if repository not in ('main', 'security'):
            raise ClosureError('invalid repository')
        for package in records:
            key = (package.get('Package'), package.get('Version'), package.get('Architecture'))
            by_key.setdefault(key, []).append((repository, package))
    result, seen = [], set()
    for item in installed:
        key = tuple(item)
        if len(key) != 3 or key in seen:
            raise ClosureError('invalid or duplicate installed package')
        seen.add(key)
        matches = by_key.get(key, [])
        if not matches:
            raise MatchError('base_missing')
        for _, package in matches:
            _validate_package(package)
        # An index must not repeat a tuple; repositories may carry the same deb
        # at different pool paths. Every other retained field must agree.
        if len({repo for repo, _ in matches}) != len(matches):
            raise MatchError('base_ambiguous')
        matches.sort(key=lambda entry: (entry[0] != 'main', entry[0]))
        repository, package = matches[0]
        canonical = {key: value for key, value in package.items() if key != 'Filename'}
        if any({key: value for key, value in other.items() if key != 'Filename'} != canonical
               for _, other in matches[1:]):
            raise MatchError('base_ambiguous')
        origins = [{'repository': repo, 'filename': other['Filename']} for repo, other in matches]
        result.append({'repository': repository, 'package': package, 'origins': origins})
    if not result:
        raise ClosureError('empty installed inventory')
    return result


def match_base(installed, indexes):
    try:
        return match_installed(installed, indexes)
    except MatchError as exc:
        raise AuthIndexError(exc.reason) from exc
    except ClosureError as exc:
        raise AuthIndexError('base_invalid') from exc


def relations(package):
    result = {}
    for field in ('Pre-Depends', 'Depends', 'Provides'):
        if field not in package:
            continue
        groups = []
        for group in package[field].split(','):
            alternatives = []
            for term in group.split('|'):
                match = re.fullmatch(r'\s*([a-z0-9][a-z0-9+.-]*(?::(?:any|native|amd64))?)(?:\s*\((<<|<=|=|>=|>>)\s*([A-Za-z0-9.+:~_-]{1,200})\))?\s*', term)
                if not match or (field == 'Provides' and match[2] not in (None, '=')):
                    raise ClosureError('invalid dependency relation')
                item = {'name': match[1]}
                if match[2]:
                    item.update(operator=match[2], version=match[3])
                alternatives.append(item)
            if field == 'Provides' and len(alternatives) != 1:
                raise ClosureError('invalid Provides alternatives')
            groups.append(alternatives)
        result[field] = groups
    return result


def resolve_edges(rows, compare):
    """Resolve each dependency against installed packages; ambiguous groups fail."""
    packages = [row['package'] for row in rows]
    providers = {}
    for package in packages:
        _validate_package(package)
        entries = [(package['Package'], package['Version'])]
        for group in relations(package).get('Provides', []):
            entry = group[0]
            if ':' in entry['name']:
                raise ClosureError('qualified Provides unsupported')
            entries.append((entry['name'], entry.get('version')))
        for name, version in entries:
            providers.setdefault(name, []).append((package, version))
    edges = []
    for package in packages:
        for field, groups in relations(package).items():
            if field == 'Provides':
                continue
            for group in groups:
                candidates = []
                for alternative in group:
                    name, _, qualifier = alternative['name'].partition(':')
                    for candidate, version in providers.get(name, []):
                        if qualifier == 'any' and candidate.get('Multi-Arch') not in ('allowed', 'foreign'):
                            continue
                        if qualifier in ('native', 'amd64') and candidate['Architecture'] not in ('amd64', 'all'):
                            continue
                        if 'operator' in alternative and (version is None or not compare(
                                version, alternative['operator'], alternative['version'])):
                            continue
                        candidates.append((candidate, name, version, alternative))
                if len(candidates) != 1:
                    raise ClosureError('dependency missing or ambiguous')
                candidate, name, version, choice = candidates[0]
                edges.append({'source': {'name': package['Package'], 'version': package['Version'],
                                         'architecture': package['Architecture']},
                              'field': field, 'alternatives': group, 'choice': choice,
                              'provider': {'name': candidate['Package'], 'version': candidate['Version'],
                                           'architecture': candidate['Architecture']},
                              'via': name, 'provided_version': version})
    return edges


def compare_version(run, container, left, operator, right):
    if (not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,200}', left)
            or not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,200}', right)
            or operator not in ('<<', '<=', '=', '>=', '>>')):
        raise ClosureError('invalid version comparison')
    try:
        run(['docker', 'exec', container, 'dpkg', '--compare-versions', left, operator, right], timeout=30)
        return True
    except reference.ProbeExit as exc:
        if exc.code == 1:
            return False
        raise ClosureError('dpkg version comparison failed') from exc


def reconcile(base, selected, post, indexes):
    expected = {name: (name, version, arch) for name, version, arch in base}
    for item in selected:
        expected[item[0]] = item
    if len(post) != len(expected) or set(post) != set(expected.values()):
        raise ClosureError('solver and installed inventory differ')
    # The original base version must also exist, including when upgraded.
    match_installed(base, indexes)
    return match_installed(post, indexes)


def verify_deb(package, data):
    _validate_package(package)
    if len(data) != int(package['Size']):
        raise ClosureError('package size mismatch')
    try:
        return discovery.verify_digest(data, 'sha256:' + package['SHA256'])
    except ValueError as exc:
        raise ClosureError('package digest mismatch') from exc


BASE_IMAGE = 'debian@' + discovery.BASE
DPKG_QUERY = "dpkg-query -W -f='${db:Status-Abbrev}\\t${binary:Package}\\t${Version}\\t${Architecture}\\n'"
SIMULATE = 'apt-get -s --no-install-recommends install '  # roots appended below
APT_OPTIONS = ('-o Acquire::Check-Valid-Until=false -o Acquire::Languages=none '
               '-o Acquire::Retries=0 -o APT::Get::List-Cleanup=0')
CONFIGURE = ("set -eu; rm -f /etc/apt/sources.list.d/*; "
             "printf '%s\\n' 'deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] file:/mirror/main buster main' "
             "'deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] file:/mirror/security buster/updates main' > /etc/apt/sources.list; "
             "printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d; chmod 755 /usr/sbin/policy-rc.d; "
             "log=/tmp/runtime-closure-apt.log; pipe=/tmp/runtime-closure-apt.fifo; "
             "mkfifo \"$pipe\"; "
             "( dd bs=1M count=1 iflag=fullblock of=\"$log\" 2>/dev/null; "
             "dd bs=1 count=1 of=\"$log.overflow\" 2>/dev/null; dd bs=64K of=/dev/null 2>/dev/null ) <\"$pipe\" & "
             "reader=$!; status=0; apt-get " + APT_OPTIONS + " update >\"$pipe\" 2>&1 || status=$?; "
             "wait \"$reader\"; rm -f \"$pipe\"; "
             "if [ -s \"$log.overflow\" ]; then printf 'APT_RESULT=unknown\\n'; "
             "elif [ \"$status\" -eq 0 ] && ! grep -Eq '^(W|E):' \"$log\"; then "
             "printf 'APT_RESULT=ok\\n'; else "
             "if grep -qi 'NO_PUBKEY' \"$log\"; then reason=missing_key; "
             "elif grep -Eqi 'BADSIG|EXPKEYSIG|invalid signature|signatures were invalid' \"$log\"; then reason=invalid_signature; "
             "elif grep -qi 'does not have a Release file' \"$log\"; then reason=missing_release; "
             "elif grep -qi 'Permission denied' \"$log\"; then reason=permission; "
             "elif grep -Eqi 'method.*(unsupported|not supported|could not be found)' \"$log\"; then reason=unsupported_method; "
             "elif grep -Eqi 'decompress|unsupported compression|unsupported.*xz' \"$log\"; then reason=compression; "
             "elif grep -Eqi 'File not found|No such file or directory|404[[:space:]]+Not Found' \"$log\"; then reason=not_found; "
             "else reason=unknown; fi; printf 'APT_RESULT=%s\\n' \"$reason\"; fi; "
             "awk '/^[EW]:/ { if (++n <= 8) print substr($0, 1, 4096) }' \"$log\"; "
             "rm -f \"$log\" \"$log.overflow\"")


def configure_result(data):
    if len(data) > 64 * 1024:
        raise ClosureError('invalid APT diagnostic output')
    marker, separator, remainder = data.partition(b'\n')
    if not separator or any(not line.startswith((b'E:', b'W:')) for line in remainder.splitlines()):
        raise ClosureError('invalid APT diagnostic output')
    errors = sanitize_apt_lines(remainder)
    if marker == b'APT_RESULT=ok' and not errors:
        return
    for reason in APT_REASONS:
        if marker == ('APT_RESULT=' + reason).encode('ascii'):
            raise AptConfigureError(reason, errors)
    raise ClosureError('invalid APT diagnostic output')


def write_mirror(directory, metadata):
    for name, (_, dist) in ARCHIVES.items():
        info = metadata[name]
        for suffix, expected_size, expected_sha in (
            ('InRelease', None, info['inrelease_sha256']),
            ('main/binary-amd64/Packages.xz', info['packages_size'], info['packages_sha256'])):
            url = archive_root(name) + 'dists/' + dist + '/' + suffix
            if suffix == 'InRelease' and info['inrelease_url'] != url:
                raise ClosureError('unexpected InRelease URL')
            with tempfile.TemporaryDirectory(prefix='fetch-', dir=directory.parent) as temp:
                blob = official_fetch(url, limit=expected_size or 2 * 1024 * 1024)
                if expected_size is not None and len(blob) != expected_size:
                    raise ClosureError('mirror index size mismatch')
                discovery.verify_digest(blob, 'sha256:' + expected_sha)
                downloaded = Path(temp) / 'download'
                downloaded.write_bytes(blob)
                target = directory / name / 'dists' / dist / suffix
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(downloaded.read_bytes())


def download_payloads(rows, directory):
    for row in rows:
        package, name = row['package'], row['repository']
        size = int(package['Size'])
        if size > 256 * 1024 * 1024:
            raise ClosureError('package exceeds download limit')
        url = archive_root(name) + package['Filename']
        target_dir = directory / ('payload-' + uuid.uuid4().hex)
        target_dir.mkdir()
        payload = verify_deb(package, official_fetch(url, limit=size))
        target = target_dir / 'package.deb'
        target.write_bytes(payload)
        target.chmod(0o644)
        row['download_path'] = str(target.relative_to(directory))
        row['artifact_url'] = url
        row['relations'] = relations(package)


def copy_archives(rows, directory, container, run):
    targets = []
    for row in rows:
        package = row['package']
        # apt uses the filename's canonical name with encoded epoch.
        filename = (package['Package'] + '_' + package['Version'].replace(':', '%3a')
                    + '_' + package['Architecture'] + '.deb')
        targets.append(filename)
    if len(set(targets)) != len(targets):
        raise ClosureError('duplicate apt cache path')
    for row, filename in zip(rows, targets):
        source = directory / row['download_path']
        verify_deb(row['package'], source.read_bytes())
        run(['docker', 'cp', str(source), container + ':/var/cache/apt/archives/' + filename], timeout=60)


def image_identity(data, document):
    try:
        image, = json.loads(data)
        digests = image['RepoDigests']
        if (document['platform'] != 'linux/amd64' or document['base']['manifest'] != discovery.BASE
                or image['Id'] != document['base']['config'] or image['Os'] != 'linux'
                or image['Architecture'] != 'amd64' or image['Config'].get('Volumes')
                or not isinstance(digests, list)
                or not any(d in (BASE_IMAGE, 'debian@' + discovery.BASE,
                                  'docker.io/library/debian@' + discovery.BASE,
                                  'library/debian@' + discovery.BASE) for d in digests)):
            raise ValueError()
        return image['Id']
    except (ValueError, KeyError, TypeError) as exc:
        raise ClosureError('base identity mismatch') from exc


def collect(document, temp, run):
    with stage('base_pull'):
        run(['docker', 'pull', '--platform=linux/amd64', BASE_IMAGE], timeout=300)
    with stage('base_inspect'):
        image = image_identity(run(['docker', 'image', 'inspect', BASE_IMAGE], timeout=30), document)
    results = {}
    base_common = None
    for kind, roots in ROOTS.items():
        name = 'runtime-closure-' + uuid.uuid4().hex
        with stage('mirror', kind):
            mirror = temp / ('mirror-' + kind)
            mirror.mkdir()
        try:
            with stage('create', kind):
                run(['docker', 'create', '--name', name, '--platform=linux/amd64', '--network=none',
                     '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=DAC_OVERRIDE',
                     '--cap-add=FOWNER', '--cap-add=SETUID', '--cap-add=SETGID',
                     '--security-opt=no-new-privileges', '--pids-limit=512',
                     '--memory=4g', '--cpus=2', '--pull=never', '--entrypoint=/bin/sh', image,
                     '-c', 'while :; do sleep 60; done'], timeout=30)
            with stage('start', kind):
                run(['docker', 'start', name], timeout=30)
            with stage('base_inventory', kind):
                base = parse_inventory(run(['docker', 'exec', name, '/bin/sh', '-c', DPKG_QUERY], timeout=30))
                if base_common is not None and base != base_common:
                    raise ClosureError('base drift between roots')
                base_common = base
            with stage('keyring', kind):
                keypath = temp / ('key-' + kind)
                keypath.mkdir()
                run(['docker', 'cp', name + ':/usr/share/keyrings/debian-archive-keyring.gpg',
                     str(keypath / 'keyring.gpg')], timeout=30)
            with stage('auth_index', kind):
                indexes = authenticated_indexes(document, keypath / 'keyring.gpg')
                match_base(base, indexes)
            with stage('mirror', kind):
                write_mirror(mirror, document['snapshot'])
                run(['docker', 'cp', str(mirror), name + ':/mirror'], timeout=60)
            with stage('configure', kind):
                configure_result(run(['docker', 'exec', name, '/bin/sh', '-c', CONFIGURE], timeout=180))
            with stage('pre_inventory', kind):
                pre = parse_inventory(run(['docker', 'exec', name, '/bin/sh', '-c', DPKG_QUERY], timeout=30))
                if pre != base:
                    raise ClosureError('pre-install inventory changed')
            with stage('solver', kind):
                simulation = SIMULATE + ' '.join(roots)
                plan_data = run(['docker', 'exec', name, '/bin/sh', '-c', simulation], timeout=120)
                plan = parse_solver(plan_data)
                selected = match_installed(plan, indexes) if plan else []
            with stage('download', kind):
                download_payloads(selected, mirror)
                run(['docker', 'exec', name, '/bin/sh', '-c', 'chmod 755 /var/cache/apt/archives'], timeout=30)
                copy_archives(selected, mirror, name, run)
            with stage('install', kind):
                install = ('DEBIAN_FRONTEND=noninteractive apt-get ' + APT_OPTIONS
                           + ' --no-download --no-install-recommends -y install ' + ' '.join(roots))
                run(['docker', 'exec', name, '/bin/sh', '-c', install], timeout=600)
            with stage('check', kind):
                run(['docker', 'exec', name, '/bin/sh', '-c', 'apt-get ' + APT_OPTIONS + ' check'], timeout=120)
            with stage('post', kind):
                post = parse_inventory(run(['docker', 'exec', name, '/bin/sh', '-c', DPKG_QUERY], timeout=30))
                final = reconcile(base, plan, post, indexes)
            with stage('download', kind):
                download_payloads(final, mirror)
            with stage('graph', kind):
                edges = resolve_edges(final, lambda left, op, right: compare_version(run, name, left, op, right))
                for row in final:
                    row.pop('download_path', None)
                results[kind] = {'roots': list(roots), 'base': base, 'pre': pre, 'post': post,
                                 'solver': plan_data.decode('ascii'), 'packages': final,
                                 'dependency_edges': edges}
        finally:
            with cleanup_stage(kind):
                run(['docker', 'rm', '-f', name], timeout=30)
    return {'schema_version': 1, 'platform': 'linux/amd64', 'base_manifest': discovery.BASE,
            'closures': results, 'limitations': ['Base provenance does not prove installed deb bytes.',
                                                  'Unreviewed discovery is not a reproducible image.']}


def read_discovery(path, *, limit=2 * 1024 * 1024):
    with path.open('rb') as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ClosureError('discovery JSON too large')
    return json.loads(data)


def write_output(path, result, *, limit=8 * 1024 * 1024):
    data = (json.dumps(result, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(data) > limit:
        raise ClosureError('output exceeds limit')
    fd, tmp = tempfile.mkstemp(prefix='.closure-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(tmp, path)
    except BaseException as first:
        try:
            os.unlink(tmp)
        except OSError as cleanup:
            first.add_note('temporary output cleanup failed')
            raise first from cleanup
        raise
    else:
        try:
            os.unlink(tmp)
        except OSError as exc:
            raise PublishedCleanupError('evidence published; temporary cleanup failed') from exc


def main():
    global _OVERALL_END
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--discoveryJSON', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    with stage('input'):
        require_ci()
        if args.output.exists() or args.output.is_symlink():
            raise ClosureError('output already exists')
        document = read_discovery(args.discoveryJSON)
    with tempfile.TemporaryDirectory(prefix='runtime-closure-') as tmp:
        root = Path(tmp)
        config = root / 'docker-config'
        config.mkdir()
        env = {'PATH': os.defpath, 'HOME': str(config), 'DOCKER_CONFIG': str(config),
               'DOCKER_HOST': 'unix:///var/run/docker.sock', 'LC_ALL': 'C'}
        end = time.monotonic() + 1800
        _OVERALL_END = end
        def run(args, *, timeout):
            remaining = end - time.monotonic()
            cleanup = args[1:3] == ['rm', '-f']
            if remaining <= 0 and not cleanup:
                raise ClosureError('overall deadline exceeded')
            if cleanup:
                # Cleanup retains its own deadline even after the overall alarm.
                old_timer = signal.setitimer(signal.ITIMER_REAL, 0)
                try:
                    return reference.bounded_run(args, timeout=timeout, env=env)
                finally:
                    if old_timer[0] > 0:
                        signal.setitimer(signal.ITIMER_REAL, max(0.001, end - time.monotonic()))
            return reference.bounded_run(args, timeout=min(timeout, remaining), env=env)
        def expired(signum, frame):
            raise TimeoutError('overall deadline exceeded')
        previous_handler = signal.signal(signal.SIGALRM, expired)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, 1800)
        try:
            result = collect(document, root, run)
        finally:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            signal.signal(signal.SIGALRM, previous_handler)
        with stage('publish'):
            write_output(args.output, result)


if __name__ == '__main__':
    try:
        main()
    except DiagnosticError as exc:
        print(diagnostic_message(exc), file=sys.stderr)
        if exc.category == 'published_cleanup':
            print('evidence published; temporary cleanup failed', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('closure failure stage=setup kind=none category=other cleanup=not_attempted', file=sys.stderr)
        sys.exit(1)
