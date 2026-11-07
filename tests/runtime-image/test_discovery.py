import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock
import tempfile
import subprocess
import sys
import urllib.error

PATH = Path(__file__).resolve().parents[2] / 'tooling/runtime-image/discovery.py'
spec = importlib.util.spec_from_file_location('discovery', PATH)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class DiscoveryTest(unittest.TestCase):
    def test_digest_rejects_tampering(self):
        payload = b'content'
        digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
        self.assertEqual(d.verify_digest(payload, digest), payload)
        with self.assertRaises(ValueError):
            d.verify_digest(b'changed', digest)
        with self.assertRaises(ValueError):
            d.verify_digest(payload, 'sha256:bad')

    def test_go_requires_exact_unique_archive(self):
        archive = {'filename': 'go1.20.14.linux-amd64.tar.gz', 'os': 'linux',
                   'arch': 'amd64', 'kind': 'archive', 'sha256': 'a' * 64}
        self.assertEqual(d.go_archive([{'version': 'go1.20.14', 'files': [archive]}]), archive)
        for records in ([], [{'version': 'go1.20.14', 'files': [archive, archive]}],
                        [{'version': 'go1.20.14', 'files': [dict(archive, sha256='bad')]}]):
            with self.assertRaises(ValueError):
                d.go_archive(records)

    def test_go_payload_verified_only_after_exact_size_and_hash(self):
        data = b'test archive'
        record = {'filename': 'go1.20.14.linux-amd64.tar.gz', 'size': len(data),
                  'sha256': hashlib.sha256(data).hexdigest()}
        with mock.patch.object(d, 'fetch', return_value=data) as fetch:
            result = d.verify_go_payload(record)
        self.assertTrue(result['payload_verified'])
        self.assertEqual(result['payload_url'], 'https://dl.google.com/go/go1.20.14.linux-amd64.tar.gz')
        self.assertLessEqual(fetch.call_args.kwargs['limit'], 128 * 1024 * 1024)
        for bad in (dict(record, size=len(data) + 1), dict(record, sha256='0' * 64),
                    {k: v for k, v in record.items() if k != 'size'}, dict(record, size=-1)):
            with self.subTest(bad=bad), mock.patch.object(d, 'fetch', return_value=data):
                with self.assertRaises((ValueError, KeyError)):
                    d.verify_go_payload(bad)
        with mock.patch.object(d, 'fetch', side_effect=OSError('network')):
            with self.assertRaises(OSError):
                d.verify_go_payload(record)
        self.assertNotIn('payload_verified', record)

    def test_snapshot_records_bounded_keyring_identity(self):
        release = 'Origin: Debian\n'
        index = b'index'
        with tempfile.TemporaryDirectory() as tmp:
            keyring = Path(tmp) / 'keyring'
            keyring.write_bytes(b'keys')
            with mock.patch.object(d, 'fetch', return_value=index), \
                 mock.patch.object(d, 'verify_digest'), \
                 mock.patch.object(d, 'verify_inrelease', return_value=release), \
                 mock.patch.object(d, 'check_release'), mock.patch.object(d, 'verify_index'):
                result = d.snapshot_metadata(keyring)
            for item in result.values():
                self.assertEqual(item['keyring_size'], 4)
                self.assertEqual(item['keyring_sha256'], hashlib.sha256(b'keys').hexdigest())
            keyring.write_bytes(b'x' * (8 * 1024 * 1024 + 1))
            with mock.patch.object(d, 'fetch') as fetch:
                with self.assertRaises(ValueError):
                    d.snapshot_metadata(keyring)
                fetch.assert_not_called()

    def test_snapshot_verifies_with_frozen_keyring_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / 'keyring'
            original.write_bytes(b'original')
            seen = []
            def fetch(*args, **kwargs):
                original.write_bytes(b'changed')
                return b'index'
            def verify(data, path, signer):
                seen.append((path != original, path.read_bytes(), path.stat().st_mode & 0o777))
                return 'Origin: Debian\n'
            with mock.patch.object(d, 'fetch', side_effect=fetch), \
                 mock.patch.object(d, 'verify_digest'), mock.patch.object(d, 'verify_inrelease', side_effect=verify), \
                 mock.patch.object(d, 'check_release'), mock.patch.object(d, 'verify_index'):
                result = d.snapshot_metadata(original)
            self.assertEqual(seen, [(True, b'original', 0o600)] * 2)
            for entry in result.values():
                self.assertEqual(entry['keyring_sha256'], hashlib.sha256(b'original').hexdigest())
                self.assertEqual(entry['keyring_size'], len(b'original'))

    def test_platform_manifest_checks_config_and_digest(self):
        config = json.dumps({'architecture': 'amd64', 'os': 'linux'}).encode()
        manifest = json.dumps({'schemaVersion': 2, 'config': {
            'digest': 'sha256:' + hashlib.sha256(config).hexdigest(), 'size': len(config)}}).encode()
        self.assertEqual(d.check_manifest(manifest, config)['architecture'], 'amd64')
        with self.assertRaises(ValueError):
            d.check_manifest(manifest, config.replace(b'amd64', b'arm64'))

    def test_signature_must_match_primary_fingerprint(self):
        fp = d.SIGNERS['main']
        status = '[GNUPG:] VALIDSIG ' + 'B' * 40 + ' 2024-04-08 0 0 4 0 1 10 01 ' + fp
        d.check_signature(status, fp)
        with self.assertRaises(ValueError):
            d.check_signature(status, d.SIGNERS['security'])
        with self.assertRaises(ValueError):
            d.check_signature('[GNUPG:] BADSIG x', fp)

    def test_cosigned_release_requires_valid_expected_primary(self):
        expected = d.SIGNERS['main']
        def validsig(primary):
            return '[GNUPG:] VALIDSIG ' + 'B' * 40 + ' 2023-06-10 1686387213 0 4 0 1 8 01 ' + primary
        d.check_signature(validsig('A' * 40) + '\n' + validsig(expected), expected)
        malformed = '[GNUPG:] VALIDSIG ' + 'B' * 40 + ' secret secret secret secret secret secret secret secret ' + expected
        for status in (validsig('A' * 40), validsig(expected) + '\n[GNUPG:] VALIDSIG malformed', malformed):
            with self.assertRaises(ValueError):
                d.check_signature(status, expected)

    def test_signature_status_failure_is_redacted_and_specific(self):
        with self.assertRaises(d.DiscoveryError) as raised:
            with d.stage('snapshot_main_gpgv'):
                d.check_signature('[GNUPG:] VALIDSIG private', d.SIGNERS['main'])
        self.assertEqual(str(raised.exception), 'snapshot_main_gpgv: signer count=1 fields=3 expected=0')
        self.assertNotIn('private', str(raised.exception))

    def test_incomplete_result_shape(self):
        archive = {'filename': 'go1.20.14.linux-amd64.tar.gz', 'os': 'linux',
                   'arch': 'amd64', 'kind': 'archive', 'sha256': 'a' * 64}
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'evidence.json'
            with mock.patch.object(d, 'registry_manifest', return_value={'manifest': d.BASE}), \
                 mock.patch.object(d, 'fetch', return_value=json.dumps([{'version': 'go1.20.14', 'files': [archive]}]).encode()), \
                 mock.patch.object(d, 'verify_go_payload', side_effect=lambda record: dict(record, payload_verified=True)), \
                 mock.patch.object(d, 'snapshot_metadata', return_value={'main': {}, 'security': {}}), \
                 mock.patch('sys.argv', ['discovery', '--keyring', 'keyring', '--output', str(target)]):
                d.main()
            data = json.loads(target.read_text())
            for field in ('build_packages', 'runtime_packages', 'reference_inventory'):
                self.assertIsNone(data[field])

    def test_failure_does_not_create_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'evidence.json'
            with mock.patch.object(d, 'registry_manifest', side_effect=ValueError('bad digest')), \
                 mock.patch('sys.argv', ['discovery', '--keyring', 'keyring', '--output', str(target)]), \
                 self.assertRaises(d.DiscoveryError):
                d.main()
            self.assertFalse(target.exists())

    def test_redirect_policy_rejects_downgrade_and_cross_origin(self):
        handler = d.SafeRedirect()
        request = d.urllib.request.Request('https://go.dev/a', headers={'Authorization': 'Bearer secret'})
        for target in ('http://go.dev/b', 'https://attacker.example/b'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                handler.redirect_request(request, None, 302, 'Found', {}, target)
            error.exception.close()
        redirected = handler.redirect_request(request, None, 302, 'Found', {}, '/b')
        self.assertEqual(redirected.full_url, 'https://go.dev/b')
        self.assertLessEqual(handler.max_redirections, 5)

    def test_registry_blob_redirect_to_approved_cdn_strips_credentials(self):
        handler = d.SafeRedirect()
        request = d.urllib.request.Request(
            'https://registry-1.docker.io/v2/library/debian/blobs/sha256:' + 'a' * 64,
            headers={'Authorization': 'Bearer secret', 'Cookie': 'session=secret', 'X-Api-Key': 'secret', 'Accept': 'application/json'})
        request.add_unredirected_header('Proxy-Authorization', 'Basic secret')
        cdn = 'https://production.cloudfront.docker.com/registry/blob?signature=secret'
        redirected = handler.redirect_request(request, None, 307, 'Temporary Redirect', {}, cdn)
        self.assertEqual(redirected.full_url, cdn)
        self.assertEqual(redirected.header_items(), [])
        for url in ('http://production.cloudfront.docker.com/x',
                    'https://production.cloudfront.docker.com.evil.example/x',
                    'https://user@production.cloudfront.docker.com/x',
                    'https://production.cloudfront.docker.com:444/x'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                handler.redirect_request(request, None, 307, 'Found', {}, url)
            self.assertNotIn('secret', str(error.exception))
            error.exception.close()
        for source in ('https://registry-1.docker.io/v2/library/other/blobs/sha256:' + 'a' * 64,
                       'https://registry-1.docker.io/v2/library/debian/manifests/sha256:' + 'a' * 64,
                       'https://registry-1.docker.io/v2/library/debian/blobs/not-a-digest'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                handler.redirect_request(d.urllib.request.Request(source), None, 307, 'Found', {}, cdn)
            error.exception.close()

    def test_cli_error_does_not_print_signed_url(self):
        secret = 'https://production.cloudfront.docker.com/x?signature=private'
        script = ('import runpy, urllib.error, urllib.request\n'
                  'def fail(*args, **kwargs):\n'
                  f'    raise urllib.error.URLError({secret!r})\n'
                  'urllib.request.OpenerDirector.open = fail\n'
                  f'runpy.run_path({str(PATH)!r}, run_name="__main__")\n')
        proc = subprocess.run([sys.executable, '-I', '-B', '-c', script,
                               '--keyring', 'keyring', '--output', 'output'],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn('private', proc.stderr)
        self.assertNotIn('Traceback', proc.stderr)

    def test_diagnostics_are_stage_scoped_and_redacted(self):
        for stage in ('base_registry', 'go_json', 'snapshot_main_fetch', 'snapshot_main_gpgv',
                      'snapshot_main_index', 'snapshot_security_fetch', 'snapshot_security_gpgv',
                      'snapshot_security_index'):
            with self.subTest(stage=stage):
                with self.assertRaises(d.DiscoveryError) as raised:
                    with d.stage(stage):
                        raise urllib.error.URLError('signed=private')
                self.assertEqual(str(raised.exception), stage + ': network')
                self.assertNotIn('private', str(raised.exception))
        with self.assertRaises(d.DiscoveryError) as raised:
            with d.stage('snapshot_main_gpgv'):
                raise subprocess.TimeoutExpired('private', 30)
        self.assertEqual(str(raised.exception), 'snapshot_main_gpgv: timeout')
        with self.assertRaises(d.DiscoveryError) as raised:
            with d.stage('base_registry'):
                raise urllib.error.URLError(TimeoutError('private'))
        self.assertEqual(str(raised.exception), 'base_registry: timeout')

    def test_cli_redacted_diagnostic_has_registry_stage(self):
        script = ('import runpy, urllib.error, urllib.request\n'
                  'def fail(*args, **kwargs):\n'
                  '    raise urllib.error.URLError("signed=private")\n'
                  'urllib.request.OpenerDirector.open = fail\n'
                  f'runpy.run_path({str(PATH)!r}, run_name="__main__")\n')
        proc = subprocess.run([sys.executable, '-I', '-B', '-c', script,
                               '--keyring', 'keyring', '--output', 'output'],
                              capture_output=True, text=True)
        self.assertIn('base_registry: network', proc.stderr)
        self.assertNotIn('private', proc.stderr)
        self.assertNotIn('Traceback', proc.stderr)

    def test_nested_deadline_uses_earliest_and_restores_remaining(self):
        with mock.patch.object(d.signal, 'signal', return_value='previous') as handler, \
             mock.patch.object(d.signal, 'getitimer', return_value=(3.0, 2.0)), \
             mock.patch.object(d.signal, 'setitimer') as timer, \
             mock.patch.object(d.time, 'monotonic', side_effect=[10.0, 11.0]):
            with self.assertRaisesRegex(RuntimeError, 'first'):
                with d.deadline(60):
                    raise RuntimeError('first')
            self.assertEqual(timer.call_args_list[0], mock.call(d.signal.ITIMER_REAL, 3.0))
            self.assertEqual(timer.call_args_list[1], mock.call(d.signal.ITIMER_REAL, 0))
            self.assertEqual(timer.call_args_list[2], mock.call(d.signal.ITIMER_REAL, 2.0, 2.0))
            self.assertEqual(handler.call_args_list[-1], mock.call(d.signal.SIGALRM, 'previous'))

    def test_expired_outer_timer_rearmed_after_outer_handler(self):
        current = ['outer']
        events = []
        def handler(signum, value):
            old = current[0]
            current[0] = value
            events.append(('handler', value))
            return old
        def timer(which, value, interval=0):
            events.append(('timer', value))
            if value == 0.000001 and current[0] != 'outer':
                current[0](d.signal.SIGALRM, None)
        with mock.patch.object(d.signal, 'signal', side_effect=handler), \
             mock.patch.object(d.signal, 'getitimer', return_value=(1, 0)), \
             mock.patch.object(d.signal, 'setitimer', side_effect=timer), \
             mock.patch.object(d.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaisesRegex(RuntimeError, 'first'):
                with d.deadline(60):
                    raise RuntimeError('first')
        self.assertLess(events.index(('timer', 0)), events.index(('handler', 'outer')))
        self.assertLess(events.index(('handler', 'outer')), events.index(('timer', 0.000001)))

    def test_deadline_restores_inactive_and_expired_outer_timer(self):
        for previous, elapsed, expected in (((0, 0), 1, 0), ((2, 0), 3, 0.000001)):
            with self.subTest(previous=previous), \
                 mock.patch.object(d.signal, 'signal'), \
                 mock.patch.object(d.signal, 'getitimer', return_value=previous), \
                 mock.patch.object(d.signal, 'setitimer') as timer, \
                 mock.patch.object(d.time, 'monotonic', side_effect=[10.0, 10.0 + elapsed]):
                with d.deadline(1):
                    pass
                self.assertEqual(timer.call_args_list[-1], mock.call(d.signal.ITIMER_REAL, expected, 0))

    def test_fetch_has_absolute_deadline(self):
        class Slow:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'x'
        with mock.patch.object(d, 'open_url', return_value=Slow()), \
             mock.patch.object(d.time, 'monotonic', side_effect=[0, 1, 100]):
            with self.assertRaises(TimeoutError):
                d.fetch('https://go.dev/dl/', limit=10)

    def test_release_identity_rejects_wrong_origin_and_replay(self):
        fields = {'Origin': 'Debian', 'Label': 'Debian', 'Codename': 'buster',
                  'Suite': 'oldoldstable', 'Date': 'Sat, 10 Jun 2023 08:53:33 UTC'}
        d.check_release(fields, 'main')
        for changed in ({'Origin': 'Other'}, {'Codename': 'bookworm'},
                        {'Date': 'Sat, 10 Jun 2034 11:09:03 UTC'}):
            with self.assertRaises(ValueError):
                d.check_release(dict(fields, **changed), 'main')
        security = {key: value for key, value in d.RELEASES['security'].items() if key != 'sha256'}
        d.check_release(security, 'security')
        with self.assertRaises(ValueError):
            d.check_release(security, 'main')
        with self.assertRaises(ValueError):
            d.release_fields('Origin: Debian\nOrigin: evil\n')

    def test_gpg_timeout_stops_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(d.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gpgv', 30)) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    d.verify_inrelease(b'bad', Path(tmp) / 'keyring', d.SIGNERS['main'])
                self.assertIsNotNone(run.call_args.kwargs['timeout'])

    def test_release_hash_and_sizes(self):
        body = b'index'
        release = 'SHA256:\n ' + hashlib.sha256(body).hexdigest() + ' 5 main/binary-amd64/Packages.xz\n'
        d.verify_index(release, 'main/binary-amd64/Packages.xz', body)
        with self.assertRaises(ValueError):
            d.verify_index(release, 'main/binary-amd64/Packages.xz', b'wrong')
        with self.assertRaises(ValueError):
            d.verify_index(release + release, 'main/binary-amd64/Packages.xz', body)


if __name__ == '__main__':
    unittest.main()
