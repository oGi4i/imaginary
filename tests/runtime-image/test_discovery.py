import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock
import tempfile
import subprocess
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

    def test_incomplete_result_shape(self):
        archive = {'filename': 'go1.20.14.linux-amd64.tar.gz', 'os': 'linux',
                   'arch': 'amd64', 'kind': 'archive', 'sha256': 'a' * 64}
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'evidence.json'
            with mock.patch.object(d, 'registry_manifest', return_value={'manifest': d.BASE}), \
                 mock.patch.object(d, 'fetch', return_value=json.dumps([{'version': 'go1.20.14', 'files': [archive]}]).encode()), \
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
                 self.assertRaises(ValueError):
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
