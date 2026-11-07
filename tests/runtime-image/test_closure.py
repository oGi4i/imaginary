import hashlib
import importlib.util
import lzma
import json
import tempfile
import subprocess
from pathlib import Path
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parents[2] / 'tooling/runtime-image/closure.py'
spec = importlib.util.spec_from_file_location('closure', PATH)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def record(name='example', version='1', arch='amd64'):
    return {'Package': name, 'Version': version, 'Architecture': arch,
            'Filename': 'pool/main/e/example/example_1_amd64.deb',
            'Size': '4', 'SHA256': hashlib.sha256(b'data').hexdigest(),
            'Depends': 'foo (>= 1) | bar', 'Provides': 'virtual (= 1)'}


class ClosureTest(unittest.TestCase):
    def test_index_and_exact_installed_match(self):
        package = record()
        text = '\n'.join(f'{k}: {v}' for k, v in package.items()) + '\n\n'
        blob = lzma.compress(text.encode())
        index = c.parse_index(blob, len(blob), hashlib.sha256(blob).hexdigest())
        result = c.match_installed([('example', '1', 'amd64')], [('main', index)])
        self.assertEqual(result[0]['package'], package)
        self.assertEqual(result[0]['repository'], 'main')
        self.assertEqual(c.verify_deb(package, b'data'), b'data')

    def test_missing_and_ambiguous_match_are_errors(self):
        with self.assertRaises(c.ClosureError):
            c.match_installed([('example', '2', 'amd64')], [('main', [record()])])
        with self.assertRaises(c.ClosureError):
            c.match_installed([('example', '1', 'amd64')], [('main', [record(), record()])])

    def test_corrupt_index_and_deb_fail(self):
        blob = lzma.compress(b'Package: example\n\n')
        with self.assertRaises(c.ClosureError):
            c.parse_index(blob, len(blob) + 1, hashlib.sha256(blob).hexdigest())
        with self.assertRaises(c.ClosureError):
            c.parse_index(blob, len(blob), '0' * 64)
        with self.assertRaises(c.ClosureError):
            c.verify_deb(record(), b'evil')

    def test_invalid_records_and_duplicate_inventory_fail(self):
        with self.assertRaises(c.ClosureError):
            c.match_installed([('example', '1', 'amd64')] * 2, [('main', [record()])])
        invalid = record()
        invalid['Filename'] = '../etc/passwd'
        with self.assertRaises(c.ClosureError):
            c.match_installed([('example', '1', 'amd64')], [('main', [invalid])])

    def test_case_insensitive_fields_and_private_field_grammar(self):
        payload = b'package: example\nx_custom: ok\nVERSION: 1\n\n'
        blob = lzma.compress(payload)
        result = c.parse_index(blob, len(blob), hashlib.sha256(blob).hexdigest())
        self.assertEqual(result[0]['Package'], 'example')
        self.assertEqual(result[0]['Version'], '1')
        self.assertNotIn('x_custom', result[0])
        for payload in (b'X_custom: a\nx_CUSTOM: b\n\n',
                        b'-invalid: x\n\n', b'#invalid: x\n\n'):
            blob = lzma.compress(payload)
            with self.subTest(payload=payload), self.assertRaises(c.ClosureError):
                c.parse_index(blob, len(blob), hashlib.sha256(blob).hexdigest())

    def test_rejects_case_insensitive_duplicate_known_field(self):
        blob = lzma.compress(b'Package: a\npAcKaGe: b\n\n')
        with self.assertRaises(c.ClosureError):
            c.parse_index(blob, len(blob), hashlib.sha256(blob).hexdigest())

    def test_rejects_oversized_size(self):
        package = record()
        package['Size'] = '1' * 5000
        with self.assertRaises(c.ClosureError):
            c.verify_deb(package, b'data')

    def test_rejects_wrong_lzma_format(self):
        blob = lzma.compress(b'Package: example\n\n', format=lzma.FORMAT_ALONE)
        with self.assertRaises(c.ClosureError):
            c.parse_index(blob, len(blob), hashlib.sha256(blob).hexdigest())

    def test_root_lists_match_reference_dockerfile(self):
        dockerfile = subprocess.run(
            ['git', '-C', str(PATH.parents[2]), 'show',
             '9357624e976790905f1b1bf4d059f8643c21e6fb:Dockerfile'],
            check=True, capture_output=True, text=True, timeout=10).stdout
        import re
        matches = re.findall(r'apt-get install --no-install-recommends -y\s+(.+?)\s*&&', dockerfile, re.S)
        self.assertEqual(len(matches), 2)
        roots = [tuple(part for part in match.split() if part != '\\') for match in matches]
        self.assertEqual(roots, [c.ROOTS['build'], c.ROOTS['runtime']])

    def test_guard_and_inventory(self):
        with mock.patch.dict('os.environ', {}, clear=True), self.assertRaises(c.ClosureError):
            c.require_ci()
        with mock.patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}, clear=True):
            c.require_ci()
        self.assertEqual(c.parse_inventory(b'ii \texample\t1\tamd64\n'), [('example', '1', 'amd64')])
        for value in (b'', b'ii \texample\t1\tamd64\nii \texample\t2\tamd64\n', b'iU \texample\t1\tamd64\n'):
            with self.subTest(value=value), self.assertRaises(c.ClosureError):
                c.parse_inventory(value)

    def test_solver_plan(self):
        result = c.parse_solver(b'Inst example (1 Debian:10/oldoldstable [amd64])\n')
        self.assertEqual(result, [('example', '1', 'amd64')])
        for value in (b'Inst example\n', b'Inst example (1 Debian [arm64])\n',
                      b'Remv example [1]\n'):
            with self.subTest(value=value), self.assertRaises(c.ClosureError):
                c.parse_solver(value)

    def test_reconcile_detects_drift_and_retains_edges(self):
        base = [('example', '1', 'amd64')]
        selected = [('new', '2', 'all')]
        new = record('new', '2', 'all')
        rows = c.reconcile(base, selected, base + selected, [('main', [record(), new])])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['package']['Depends'], 'foo (>= 1) | bar')
        with self.assertRaises(c.ClosureError):
            c.reconcile(base, selected, base, [('main', [record(), new])])

    def test_deadline_context_nesting_restores_remaining_not_original(self):
        with mock.patch.object(c.time, 'monotonic', side_effect=[10, 14]), \
                mock.patch.object(c.signal, 'signal', return_value='old') as signals, \
                mock.patch.object(c.signal, 'setitimer', side_effect=[(8.0, 0), (0, 0)]) as timers:
            with c.deadline(2):
                pass
        self.assertEqual(timers.call_args_list[-1].args, (c.signal.ITIMER_REAL, 4.0, 0))
        self.assertEqual(signals.call_count, 2)

    def test_guard_precedes_any_side_effects(self):
        with mock.patch.dict('os.environ', {}, clear=True), mock.patch.object(c, 'collect') as collect, \
                mock.patch('sys.argv', ['closure', '--discoveryJSON', 'x', '--output', 'y']), \
                self.assertRaises(c.DiagnosticError):
            c.main()
        collect.assert_not_called()

    def test_relations_preserve_alternatives_and_versioned_provides(self):
        package = record()
        self.assertEqual(c.relations(package), {
            'Depends': [[{'name': 'foo', 'operator': '>=', 'version': '1'}, {'name': 'bar'}]],
            'Provides': [[{'name': 'virtual', 'operator': '=', 'version': '1'}]],
        })
        package['Depends'] = 'foo ()'
        with self.assertRaises(c.ClosureError):
            c.relations(package)

    def test_authenticated_indexes_reject_discovery_drift(self):
        blobs = {}
        metadata = {}
        for name, archive, dist in [('main', 'debian', 'buster'),
                                     ('security', 'debian-security', 'buster/updates')]:
            url = f'https://snapshot.debian.org/archive/{archive}/20240408T000000Z/dists/{dist}/main/binary-amd64/Packages.xz'
            blob = lzma.compress(('\n'.join(f'{k}: {v}' for k, v in record().items()) + '\n\n').encode())
            blobs[url] = blob
            metadata[name] = {'packages_url': url, 'packages_sha256': hashlib.sha256(blob).hexdigest(),
                              'packages_size': len(blob)}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(c.discovery, 'snapshot_metadata', return_value=metadata), \
                mock.patch.object(c.discovery, 'fetch', side_effect=lambda url, **kw: blobs[url]):
            indexes = c.authenticated_indexes({'snapshot': metadata}, Path(directory) / 'key')
            self.assertEqual([name for name, _ in indexes], ['main', 'security'])
            with self.assertRaises(c.ClosureError):
                c.authenticated_indexes({'snapshot': {}}, Path(directory) / 'key')

    def test_collection_orchestration_uses_isolated_independent_roots(self):
        base = [('example', '1', 'amd64')]
        new = record('new', '2', 'all')
        indexes = [('main', [record(), new])]
        class FakeRunner:
            def __init__(self):
                self.calls = []
                self.inventories = {}
            def __call__(self, args, **kwargs):
                self.calls.append(args)
                if args[1:3] == ['image', 'inspect']:
                    return json.dumps([{'Id': 'sha256:' + 'a' * 64, 'Os': 'linux',
                        'Architecture': 'amd64', 'Config': {'Volumes': None},
                        'RepoDigests': [c.BASE_IMAGE]}]).encode()
                if args[1] == 'exec' and args[-1] == c.DPKG_QUERY:
                    self.inventories[args[2]] = self.inventories.get(args[2], 0) + 1
                    text = 'ii \texample\t1\tamd64\n'
                    if self.inventories[args[2]] == 3:
                        text += 'ii \tnew\t2\tall\n'
                    return text.encode()
                if args[1] == 'exec' and args[-1].startswith(c.SIMULATE):
                    return b'Inst new (2 Debian [all])\n'
                return b''
        runner = FakeRunner()
        document = {'platform': 'linux/amd64', 'snapshot': {}, 'base': {'manifest': c.discovery.BASE,
                    'config': 'sha256:' + 'a' * 64}}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(c, 'authenticated_indexes', return_value=indexes), \
                mock.patch.object(c, 'download_payloads') as payloads, \
                mock.patch.object(c, 'copy_archives'), mock.patch.object(c, 'write_mirror'), \
                mock.patch.object(c, 'resolve_edges', return_value=[]):
            result = c.collect(document, Path(tmp), runner)
        self.assertEqual(set(result['closures']), {'build', 'runtime'})
        self.assertEqual(payloads.call_count, 4)
        creates = [args for args in runner.calls if args[1] == 'create']
        self.assertEqual(len(creates), 2)
        self.assertTrue(all('--network=none' in args for args in creates))
        self.assertEqual(len([args for args in runner.calls if args[1:3] == ['rm', '-f']]), 2)

    def test_cache_copy_uses_verified_downloads_and_rejects_collisions(self):
        calls = []
        rows = [{'repository': 'main', 'package': record(), 'download_path': 'payload/package.deb'}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / rows[0]['download_path']
            path.parent.mkdir()
            path.write_bytes(b'data')
            c.copy_archives(rows, root, 'own-container', lambda args, **kw: calls.append(args))
        self.assertEqual(calls[0][1], 'cp')
        self.assertIn(':/var/cache/apt/archives/', calls[0][-1])
        with self.assertRaises(c.ClosureError):
            c.copy_archives(rows * 2, Path('/unused'), 'own-container', lambda *a, **kw: None)

    def test_download_each_payload_in_separate_empty_directory(self):
        rows = [{'repository': 'main', 'package': record()}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(c.discovery, 'fetch', return_value=b'data'):
            root = Path(tmp)
            c.download_payloads(rows, root)
            path = root / rows[0]['download_path']
            self.assertEqual(path.read_bytes(), b'data')
            self.assertEqual(len(list(path.parent.iterdir())), 1)
            self.assertTrue(rows[0]['artifact_url'].startswith(c.archive_root('main')))

    def test_cleanup_runs_after_container_operation_failure(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if args[1:3] == ['image', 'inspect']:
                return json.dumps([{'Id': 'sha256:' + 'a' * 64, 'Os': 'linux',
                                    'Architecture': 'amd64', 'Config': {'Volumes': None},
                                    'RepoDigests': [c.BASE_IMAGE]}]).encode()
            if args[1] == 'start':
                raise c.ClosureError('start failed')
            return b''
        document = {'platform': 'linux/amd64', 'base': {'manifest': c.discovery.BASE,
                    'config': 'sha256:' + 'a' * 64}}
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(c.DiagnosticError) as failure:
            c.collect(document, Path(tmp), runner)
        self.assertEqual(c.diagnostic_message(failure.exception),
                         'closure failure stage=start kind=build category=closure_error cleanup=succeeded')
        self.assertEqual(len([args for args in calls if args[1:3] == ['rm', '-f']]), 1)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'closure.json'
            output.write_text('old')
            with mock.patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}, clear=True), \
                    mock.patch('sys.argv', ['closure', '--discoveryJSON', 'absent', '--output', str(output)]), \
                    mock.patch.object(c, 'collect') as collect, self.assertRaises(c.DiagnosticError):
                c.main()
            collect.assert_not_called()
            self.assertEqual(output.read_text(), 'old')

    def test_bounded_input_reads_one_byte_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'huge'
            path.write_bytes(b'a' * 20)
            with self.assertRaises(c.ClosureError):
                c.read_discovery(path, limit=10)

    def test_atomic_output_never_leaves_partial_or_clobbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.json'
            with mock.patch.object(c.os, 'fsync', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    c.write_output(path, {'ok': True})
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])
            c.write_output(path, {'ok': True})
            self.assertEqual(json.loads(path.read_text()), {'ok': True})
            with self.assertRaises(FileExistsError):
                c.write_output(path, {'ok': False})
            self.assertEqual(json.loads(path.read_text()), {'ok': True})
            with self.assertRaises(c.ClosureError):
                c.write_output(Path(tmp) / 'large', {'value': 'x' * 100}, limit=10)
            self.assertFalse((Path(tmp) / 'large').exists())

    def test_diagnostics_redact_exception_and_bound_exit_code(self):
        with self.assertRaises(c.DiagnosticError) as caught:
            with c.stage('solver', 'runtime'):
                raise c.reference.ProbeExit(7)
        self.assertEqual(c.diagnostic_message(caught.exception),
                         'closure failure stage=solver kind=runtime category=command_exit code=7 cleanup=not_attempted')
        with self.assertRaises(c.DiagnosticError) as caught:
            with c.stage('install', 'build'):
                raise ValueError('private URL or path')
        message = c.diagnostic_message(caught.exception)
        self.assertEqual(message, 'closure failure stage=install kind=build category=invalid_data cleanup=not_attempted')
        self.assertNotIn('private', message)
        with self.assertRaises(ValueError):
            with c.stage('private path', 'build'):
                pass

    def test_all_stages_and_categories_redact_payloads(self):
        samples = [(c.ClosureError('secret'), 'closure_error'),
                   (TimeoutError('secret'), 'timeout'),
                   (c.reference.CommandError('secret'), 'command_error'),
                   (OSError('secret'), 'io_error'), (RuntimeError('secret'), 'other')]
        for where in c.STAGES:
            for error, category in samples:
                with self.subTest(where=where, category=category), self.assertRaises(c.DiagnosticError) as caught:
                    with c.stage(where, 'build'):
                        raise error
                message = c.diagnostic_message(caught.exception)
                self.assertEqual(message, f'closure failure stage={where} kind=build category={category} cleanup=not_attempted')
        with self.assertRaises(ValueError):
            c.DiagnosticError('input', 'none', 'leak', cleanup='not_attempted')
        with self.assertRaises(c.DiagnosticError) as caught:
            with c.stage('solver'):
                raise c.reference.ProbeExit(99999)
        self.assertNotIn('code=', c.diagnostic_message(caught.exception))

    def test_diagnostic_cleanup_preserves_primary_cause(self):
        original = ValueError('secret')
        with self.assertRaises(c.DiagnosticError) as caught:
            with c.stage('install', 'build'):
                try:
                    raise original
                finally:
                    with c.cleanup_stage():
                        raise OSError('secret cleanup')
        self.assertIs(caught.exception.__cause__, original)
        self.assertEqual(c.diagnostic_message(caught.exception),
                         'closure failure stage=install kind=build category=invalid_data cleanup=failed')

    def test_discovery_wrapped_type_redaction(self):
        for cause, category in [(TimeoutError('secret'), 'timeout'), (ValueError('secret'), 'invalid_data')]:
            with self.subTest(category=category), self.assertRaises(c.DiagnosticError) as caught:
                with c.stage('auth_index', 'build'):
                    with c.discovery.stage('inrelease-main'):
                        raise cause
            self.assertEqual(c.diagnostic_message(caught.exception),
                             f'closure failure stage=auth_index kind=build category={category} cleanup=not_attempted')
        with self.assertRaises(c.DiagnosticError) as caught:
            with c.stage('auth_index', 'build'):
                error = c.discovery.DiscoveryError('private')
                error.category = 'secret'; error.stage = 'private'
                raise error
        self.assertEqual(c.diagnostic_message(caught.exception),
                         'closure failure stage=auth_index kind=build category=other cleanup=not_attempted')
        first = c.discovery.DiscoveryError('private')
        second = c.discovery.DiscoveryError('private')
        first.__context__ = second
        second.__context__ = first
        self.assertEqual(c.classify(first), ('other', None))

    def test_auth_and_keyring_stages_are_not_misidentified(self):
        for needle, expected in [('/usr/share/keyrings/', 'keyring'), ('__auth__', 'auth_index')]:
            calls = []
            def runner(args, **kwargs):
                calls.append(args)
                if args[1:3] == ['image', 'inspect']:
                    return json.dumps([{'Id': 'sha256:' + 'a' * 64, 'Os': 'linux',
                        'Architecture': 'amd64', 'Config': {'Volumes': None}, 'RepoDigests': [c.BASE_IMAGE]}]).encode()
                if args[1] == 'exec':
                    return b'ii \texample\t1\tamd64\n'
                if args[1] == 'cp' and needle in args[2]:
                    raise c.reference.ProbeExit(1)
                return b''
            document = {'platform': 'linux/amd64', 'base': {'manifest': c.discovery.BASE,
                        'config': 'sha256:' + 'a' * 64}}
            auth = mock.Mock(side_effect=c.ClosureError('secret'))
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(c, 'authenticated_indexes', auth), \
                    self.assertRaises(c.DiagnosticError) as caught:
                c.collect(document, Path(tmp), runner)
            self.assertEqual(caught.exception.where, expected)
            self.assertEqual(caught.exception.kind, 'build')
            self.assertEqual(caught.exception.cleanup, 'succeeded')

    def test_staged_error_keeps_original_cause_when_cleanup_fails(self):
        original = ValueError('secret')
        with self.assertRaises(c.DiagnosticError) as caught:
            try:
                with c.stage('install', 'build'):
                    raise original
            finally:
                with c.cleanup_stage('build'):
                    raise OSError('secret cleanup')
        self.assertIs(caught.exception.__cause__, original)
        self.assertEqual(caught.exception.cleanup, 'failed')

    def test_published_output_reports_temp_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.json'
            with mock.patch.object(c.os, 'unlink', side_effect=PermissionError('private path')):
                with self.assertRaises(c.PublishedCleanupError) as failure:
                    c.write_output(path, {'ok': True})
            self.assertEqual(json.loads(path.read_text()), {'ok': True})
            self.assertIsInstance(failure.exception.__cause__, PermissionError)
            self.assertNotIn('private path', str(failure.exception))

    def test_prepublication_failure_is_preserved_when_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.json'
            with mock.patch.object(c.os, 'link', side_effect=FileExistsError('occupied')), \
                    mock.patch.object(c.os, 'unlink', side_effect=PermissionError('cleanup')), \
                    self.assertRaises(FileExistsError) as failure:
                c.write_output(path, {'ok': True})
            self.assertIsInstance(failure.exception.__cause__, PermissionError)
            self.assertFalse(path.exists())

    def test_resolved_edges_reject_missing_and_ambiguous(self):
        source = record('source')
        source['Depends'] = 'foo (>= 1:2~rc1) | virtual (>= 3)'
        foo = record('foo', '1:2', 'amd64')
        foo.pop('Depends'); foo.pop('Provides')
        source.pop('Provides')
        rows = [{'package': source}, {'package': foo}]
        compare = mock.Mock(return_value=True)
        edges = c.resolve_edges(rows, compare)
        self.assertEqual(edges[0]['provider'], {'name': 'foo', 'version': '1:2', 'architecture': 'amd64'})
        compare.assert_called_once_with('1:2', '>=', '1:2~rc1')
        with self.assertRaises(c.ClosureError):
            c.resolve_edges([{'package': source}], compare)
        provider = record('provider')
        provider.pop('Depends'); provider['Provides'] = 'virtual (= 4)'
        with self.assertRaises(c.ClosureError):
            c.resolve_edges(rows + [{'package': provider}], compare)
        with self.assertRaises(c.ClosureError):
            c.resolve_edges(rows, lambda *args: False)

    def test_dpkg_compare_uses_argument_array_and_checks_exit(self):
        runner = mock.Mock(return_value=b'')
        self.assertTrue(c.compare_version(runner, 'owned', '1:2~rc1', '>=', '1:2'))
        self.assertEqual(runner.call_args.args[0], ['docker', 'exec', 'owned', 'dpkg',
                                                   '--compare-versions', '1:2~rc1', '>=', '1:2'])
        runner.side_effect = c.reference.ProbeExit(1)
        self.assertFalse(c.compare_version(runner, 'owned', '1', '=', '2'))
        runner.side_effect = c.reference.ProbeExit(2)
        with self.assertRaises(c.ClosureError):
            c.compare_version(runner, 'owned', '1', '=', '2')
        with self.assertRaises(c.ClosureError):
            c.compare_version(runner, 'owned', '1;bad', '=', '2')

    def test_virtual_version_and_arch_qualifiers(self):
        source = record('source')
        source.pop('Provides'); source['Pre-Depends'] = 'virtual:any (>= 2)'; source.pop('Depends')
        provider = record('provider', '9')
        provider.pop('Depends'); provider['Provides'] = 'virtual (= 2)'; provider['Multi-Arch'] = 'allowed'
        cmp = mock.Mock(return_value=True)
        edges = c.resolve_edges([{'package': source}, {'package': provider}], cmp)
        self.assertEqual(edges[0]['via'], 'virtual')
        cmp.assert_called_once_with('2', '>=', '2')
        provider['Provides'] = 'virtual'
        with self.assertRaises(c.ClosureError):
            c.resolve_edges([{'package': source}, {'package': provider}], cmp)
        provider['Provides'] = 'virtual (= 2)'; provider['Multi-Arch'] = 'no'
        with self.assertRaises(c.ClosureError):
            c.resolve_edges([{'package': source}, {'package': provider}], cmp)

    def test_decompression_and_duplicate_fields_are_bounded(self):
        blob = lzma.compress(b'Package: a\nPackage: b\n\n')
        with self.assertRaises(c.ClosureError):
            c.parse_index(blob, len(blob), hashlib.sha256(blob).hexdigest())
        blob = lzma.compress(b'x' * 1024)
        with self.assertRaises(c.ClosureError):
            c.parse_index(blob, len(blob), hashlib.sha256(blob).hexdigest(), max_uncompressed=10)


if __name__ == '__main__':
    unittest.main()
