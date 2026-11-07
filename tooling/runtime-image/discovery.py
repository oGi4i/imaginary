#!/usr/bin/env python3
"""Verify public metadata for the runtime-input discovery stage."""
import argparse
import hashlib
import json
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

BASE = 'sha256:1a43c9ee12ad03a221ffa57e9c41a73388b6cf4cb84c50c3ed734e9ce8ca3c6a'
SIGNERS = {'main': '80D15823B7FD1561F9F7BCDDDC30D7C23CBBABEE',
           'security': '5E61B217265DA9807A23C5FF4DFAB270CAA96DFA'}
RELEASES = {
    'main': {'sha256': 'd2126c57347cfe5ca81d912ddfecc02e9a741c6e100d8d8295b735f979bc1a9d',
             'Origin': 'Debian', 'Label': 'Debian', 'Codename': 'buster',
             'Suite': 'oldoldstable', 'Date': 'Sat, 10 Jun 2023 08:53:33 UTC',
             'Valid-Until': None},
    'security': {'sha256': 'b1cd95f497755bfc6426218c7d452ad9d06d372cd95f6c1ec89321fba77a7cc1',
                 'Origin': 'Debian', 'Label': 'Debian-Security', 'Codename': 'buster',
                 'Suite': 'oldoldstable', 'Date': 'Sun, 07 Apr 2024 12:12:17 UTC',
                 'Valid-Until': 'Sun, 14 Apr 2024 12:12:17 UTC'},
}
MEDIA = ', '.join(['application/vnd.oci.image.manifest.v1+json',
                   'application/vnd.docker.distribution.manifest.v2+json'])


class DiscoveryError(Exception):
    pass


class SignerError(ValueError):
    def __init__(self, count, field_counts, expected_present):
        super().__init__('signature status rejected')
        self.count = count
        self.field_counts = field_counts
        self.expected_present = expected_present


@contextmanager
def stage(name):
    try:
        yield
    except DiscoveryError:
        raise
    except Exception as error:
        if isinstance(error, SignerError):
            detail = (f'signer count={error.count} fields={error.field_counts} '
                      f'expected={int(error.expected_present)}')
            raise DiscoveryError(name + ': ' + detail) from None
        if (isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) or
                isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError)):
            category = 'timeout'
        elif isinstance(error, urllib.error.HTTPError):
            category = 'http'
        elif isinstance(error, urllib.error.URLError):
            category = 'network'
        elif isinstance(error, subprocess.CalledProcessError):
            category = 'process'
        elif isinstance(error, (ValueError, KeyError, TypeError)):
            category = 'invalid'
        elif isinstance(error, OSError):
            category = 'io'
        else:
            category = 'other'
        raise DiscoveryError(name + ': ' + category) from None


def verify_digest(data, digest):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest) or 'sha256:' + hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('digest mismatch')
    return data


def go_archive(records):
    matches = [f for r in records if r.get('version') == 'go1.20.14'
               for f in r.get('files', []) if f.get('filename') == 'go1.20.14.linux-amd64.tar.gz'
               and f.get('os') == 'linux' and f.get('arch') == 'amd64' and f.get('kind') == 'archive']
    if len(matches) != 1 or not re.fullmatch(r'[0-9a-f]{64}', matches[0].get('sha256', '')):
        raise ValueError('missing or ambiguous official Go archive')
    return matches[0]


def verify_go_payload(record):
    size = record['size']
    if type(size) is not int or not 0 < size <= 128 * 1024 * 1024:
        raise ValueError('invalid Go archive size')
    if record['filename'] != 'go1.20.14.linux-amd64.tar.gz':
        raise ValueError('invalid Go archive filename')
    url = 'https://dl.google.com/go/go1.20.14.linux-amd64.tar.gz'
    data = fetch(url, limit=size)
    if len(data) != size:
        raise ValueError('Go archive size mismatch')
    verify_digest(data, 'sha256:' + record['sha256'])
    return dict(record, payload_url=url, payload_verified=True)


def check_manifest(manifest, config):
    obj = json.loads(manifest)
    descriptor = obj['config']
    if obj.get('schemaVersion') != 2 or len(config) != descriptor['size']:
        raise ValueError('invalid manifest or config size')
    verify_digest(config, descriptor['digest'])
    cfg = json.loads(config)
    if cfg.get('os') != 'linux' or cfg.get('architecture') != 'amd64':
        raise ValueError('image is not linux/amd64')
    return cfg


def check_signature(status, expected):
    valid = [line.split() for line in status.splitlines() if line.startswith('[GNUPG:] VALIDSIG ')]
    timestamp = r'(?:[0-9]+|[0-9]{8}T[0-9]{6})'
    well_formed = all(len(parts) == 12 and re.fullmatch(r'[0-9A-F]{40}', parts[2])
                      and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', parts[3])
                      and re.fullmatch(timestamp, parts[4]) and re.fullmatch(timestamp, parts[5])
                      and all(re.fullmatch(r'[0-9]+', parts[i]) for i in (6, 7, 8, 9))
                      and re.fullmatch(r'[0-9A-Fa-f]{2}', parts[10])
                      and re.fullmatch(r'[0-9A-F]{40}', parts[11]) for parts in valid)
    expected_present = any(len(parts) == 12 and parts[11] == expected for parts in valid)
    if not valid or len(valid) > 16 or not well_formed or not expected_present:
        raise SignerError(min(len(valid), 17), ','.join(str(min(len(p), 99)) for p in valid[:16]),
                          expected_present)


def verify_index(release, path, data):
    section = False
    found = []
    for line in release.splitlines():
        if line == 'SHA256:':
            section = True
        elif section and line and not line[0].isspace():
            section = False
        elif section and line.strip():
            parts = line.split()
            if len(parts) == 3 and parts[2] == path:
                found.append(parts)
    if len(found) != 1 or int(found[0][1]) != len(data):
        raise ValueError('missing or ambiguous index in Release')
    verify_digest(data, 'sha256:' + found[0][0])


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if target.scheme == 'https' and target.netloc == source.netloc:
            return super().redirect_request(req, fp, code, msg, headers, target.geturl())
        registry_blob = (source.scheme == 'https' and source.netloc == 'registry-1.docker.io'
                         and not source.query and not source.fragment
                         and re.fullmatch(r'/v2/library/debian/blobs/sha256:[0-9a-f]{64}', source.path))
        if (code == 307 and req.get_method() == 'GET' and registry_blob
                and target.scheme == 'https' and target.netloc == 'production.cloudfront.docker.com'):
            return urllib.request.Request(target.geturl(), method='GET')
        raise urllib.error.HTTPError('', code, 'unapproved redirect', {}, None)


def open_url(request):
    return urllib.request.build_opener(SafeRedirect()).open(request, timeout=60)


@contextmanager
def deadline(seconds):
    def expired(signum, frame):
        raise TimeoutError('fetch deadline exceeded')
    old = signal.signal(signal.SIGALRM, expired)
    previous = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous)
        signal.signal(signal.SIGALRM, old)


def fetch(url, headers=None, limit=32 * 1024 * 1024):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('HTTPS URL required')
    request = urllib.request.Request(url, headers=headers or {})
    end = time.monotonic() + 60
    chunks = []
    size = 0
    with deadline(60), open_url(request) as response:
        while True:
            if time.monotonic() >= end:
                raise TimeoutError('fetch deadline exceeded')
            chunk = response.read(min(65536, limit + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError('response exceeds size limit')
            chunks.append(chunk)
    return b''.join(chunks)


def _limit_gpg_output():
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))


def verify_inrelease(inrelease, keyring, signer):
    with tempfile.TemporaryDirectory(prefix='runtime-inputs-') as tmp:
        signed = Path(tmp) / 'InRelease'
        clear = Path(tmp) / 'Release'
        signed.write_bytes(inrelease)
        with tempfile.TemporaryFile() as status, tempfile.TemporaryFile() as errors:
            subprocess.run(['gpgv', '--status-fd', '1', '--keyring', str(keyring),
                            '--output', str(clear), str(signed)], stdout=status, stderr=errors,
                           check=True, timeout=30, preexec_fn=_limit_gpg_output)
            if status.tell() > 65536 or errors.tell() > 65536:
                raise ValueError('gpgv output exceeds size limit')
            status.seek(0)
            check_signature(status.read(65536).decode('utf-8'), signer)
        if clear.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Release exceeds size limit')
        return clear.read_text(encoding='utf-8')


def registry_manifest(repository, digest):
    query = urllib.parse.urlencode({'service': 'registry.docker.io', 'scope': 'repository:' + repository + ':pull'})
    token = json.loads(fetch('https://auth.docker.io/token?' + query))['token']
    headers = {'Authorization': 'Bearer ' + token, 'Accept': MEDIA}
    root = 'https://registry-1.docker.io/v2/' + repository
    manifest = verify_digest(fetch(root + '/manifests/' + digest, headers), digest)
    descriptor = json.loads(manifest)['config']
    config = fetch(root + '/blobs/' + descriptor['digest'], headers)
    check_manifest(manifest, config)
    return {'manifest': digest, 'config': descriptor['digest'], 'architecture': 'amd64', 'os': 'linux'}


def release_fields(release):
    fields = {}
    for line in release.splitlines():
        if not line or line[0].isspace() or ':' not in line:
            continue
        key, value = line.split(':', 1)
        if key in fields:
            raise ValueError('duplicate Release field')
        fields[key] = value.strip()
    return fields


def check_release(fields, name):
    expected = RELEASES[name]
    for field in ('Origin', 'Label', 'Codename', 'Suite', 'Date', 'Valid-Until'):
        if fields.get(field) != expected[field]:
            raise ValueError('unexpected Release ' + field)


def snapshot_metadata(keyring):
    with keyring.open('rb') as stream:
        keyring_data = stream.read(8 * 1024 * 1024 + 1)
    if len(keyring_data) > 8 * 1024 * 1024:
        raise ValueError('keyring exceeds size limit')
    with tempfile.TemporaryDirectory(prefix='runtime-keyring-') as tmp:
        with tempfile.NamedTemporaryFile(dir=tmp) as frozen:
            frozen.write(keyring_data)
            frozen.flush()
            return _snapshot_metadata(Path(frozen.name), keyring_data)


def _snapshot_metadata(keyring, keyring_data):
    keyring_hash = hashlib.sha256(keyring_data).hexdigest()
    results = {}
    for name, dist, archive in [('main', 'buster', 'debian'), ('security', 'buster/updates', 'debian-security')]:
        root = 'https://snapshot.debian.org/archive/' + archive + '/20240408T000000Z/'
        with stage('snapshot_' + name + '_fetch'):
            inrelease = fetch(root + 'dists/' + dist + '/InRelease')
            verify_digest(inrelease, 'sha256:' + RELEASES[name]['sha256'])
        with stage('snapshot_' + name + '_gpgv'):
            release = verify_inrelease(inrelease, keyring, SIGNERS[name])
        with stage('snapshot_' + name + '_identity'):
            fields = release_fields(release)
            check_release(fields, name)
        index_path = 'main/binary-amd64/Packages.xz'
        with stage('snapshot_' + name + '_index'):
            index = fetch(root + 'dists/' + dist + '/' + index_path)
            verify_index(release, index_path, index)
        results[name] = {'inrelease_url': root + 'dists/' + dist + '/InRelease',
                         'inrelease_sha256': hashlib.sha256(inrelease).hexdigest(),
                         'signer_primary_fingerprint': SIGNERS[name],
                         'keyring_sha256': keyring_hash,
                         'keyring_size': len(keyring_data),
                         'release_fields': {key: fields.get(key) for key in
                                            ('Origin', 'Label', 'Codename', 'Suite', 'Date', 'Valid-Until')},
                         'packages_url': root + 'dists/' + dist + '/' + index_path,
                         'packages_sha256': hashlib.sha256(index).hexdigest(),
                         'packages_size': len(index),
                         'expired_valid_until_allowed': True}
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--keyring', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    with stage('base_registry'):
        base = registry_manifest('library/debian', BASE)
    with stage('go_json'):
        go = go_archive(json.loads(fetch('https://go.dev/dl/?mode=json&include=all')))
    with stage('go_payload'):
        go = verify_go_payload(go)
    snapshot = snapshot_metadata(args.keyring.resolve())
    result = {'platform': 'linux/amd64', 'base': base, 'go': go, 'snapshot': snapshot,
              'build_packages': None, 'runtime_packages': None, 'reference_inventory': None,
              'limitations': ['No package closure or reference inventory has been collected.',
                              'Metadata discovery is not a reproducible image.']}
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    try:
        main()
    except DiscoveryError as error:
        print('runtime input discovery failed at ' + str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('runtime input discovery failed at other: other', file=sys.stderr)
        sys.exit(1)
