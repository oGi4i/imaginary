import importlib.util
import io
import pathlib
import unittest
import json
import os
import tempfile
from unittest import mock

PATH = pathlib.Path(__file__).resolve().parents[2] / 'tooling/runtime-image/loader_inventory.py'
spec = importlib.util.spec_from_file_location('loader_inventory', PATH)
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


class ProtocolTests(unittest.TestCase):
    def test_valid(self):
        value = loader.parse_protocol(b'VIPS_PROBE 1\nVERSION 8.10.0\nLOAD VipsForeignLoadA\nSAVE VipsForeignSaveB\nEND\n')
        self.assertEqual(value, {'version': '8.10.0', 'loaders': ['VipsForeignLoadA'], 'savers': ['VipsForeignSaveB']})

    def test_saver_matrix_print_subclass(self):
        value = loader.parse_protocol(b'VIPS_PROBE 1\nVERSION 8.10.0\nLOAD VipsForeignLoadA\n'
                                      b'SAVE VipsForeignPrintMatrix\nSAVE VipsForeignSaveB\nEND\n')
        self.assertEqual(value['savers'], ['VipsForeignPrintMatrix', 'VipsForeignSaveB'])
        for lines in (b'LOAD VipsForeignPrintMatrix\nSAVE VipsForeignSaveB\n',
                      b'LOAD VipsForeignLoadA\nSAVE VipsForeignPrintMatrixExtra\n',
                      b'LOAD VipsForeignLoadA\nSAVE VipsForeignPrintMatrix\nSAVE VipsForeignPrintMatrix\n',
                      b'LOAD VipsForeignLoadA\nSAVE VipsForeignSaveB\nSAVE VipsForeignPrintMatrix\n'):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                loader.parse_protocol(b'VIPS_PROBE 1\nVERSION 8.10.0\n' + lines + b'END\n')

    def test_invalid(self):
        for data in (b'', b'VIPS_PROBE 1\nVERSION 10000.1.1\nEND\n', b'VIPS_PROBE 1\nVERSION 8.10.0\nEND',
                     b'VIPS_PROBE 1\nVERSION 8.10.0\nLOAD VipsForeignSaveA\nEND\n',
                     b'VIPS_PROBE 1\nVERSION 8.10.0\nLOAD VipsForeignLoadA\nLOAD VipsForeignLoadA\nEND\n',
                     b'VIPS_PROBE 1\nVERSION 8.10.0\nEND\nextra\n',
                     b'VIPS_PROBE 1\nVERSION 8.10.0\nLOAD VipsForeignLoadZ\nLOAD VipsForeignLoadA\nEND\n',
                     b'VIPS_PROBE 1\nVERSION 8.10.0\nLOAD VipsForeignLoad' + b'a' * 129 + b'\nEND\n'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                loader.parse_protocol(data)

    def test_digest_rejects_symlink_and_special_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / 'target'
            target.write_bytes(b'probe')
            link = root / 'link'
            link.symlink_to(target)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            for path in (link, fifo):
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    loader.digest(path)

    def test_collect_binds_identity_and_reports_difference(self):
        config = 'sha256:' + 'a' * 64
        layers = ['sha256:' + 'b' * 64]
        expected = {'config': config, 'diff_ids': layers}
        inspected = [{'Id': config, 'Os': 'linux', 'Architecture': 'amd64',
                      'RootFS': {'Layers': layers}, 'Config': {'Env': []}}]
        ref_inspect = [{'Id': loader.reference.CONFIG, 'Os': 'linux', 'Architecture': 'amd64',
                        'RepoDigests': [loader.reference.IMAGE], 'Config': {'Env': []}}]
        def run(argv, **kwargs):
            return json.dumps(ref_inspect).encode() if argv[1:3] == ['image', 'inspect'] else b''
        candidate = {'version': '8.10.0', 'loaders': ['VipsForeignLoadA'], 'savers': ['VipsForeignSaveA']}
        reference = {'version': '8.10.0', 'loaders': ['VipsForeignLoadB'], 'savers': ['VipsForeignSaveA']}
        with tempfile.TemporaryDirectory() as directory:
            binary = pathlib.Path(directory) / 'binary'
            source = pathlib.Path(directory) / 'source'
            binary.write_bytes(b'binary')
            source.write_bytes(b'source')
            with mock.patch.object(loader, 'probe', side_effect=[candidate, reference]):
                result = loader.collect(run, expected, inspected, binary, source)
            self.assertFalse(result['registered_types_equal'])
            self.assertFalse(result['features_complete'])
            self.assertEqual(result['candidate_identity']['diff_ids'], layers)
            self.assertEqual(result['differences']['loaders'], {'candidate_only': ['VipsForeignLoadA'], 'reference_only': ['VipsForeignLoadB']})
            inspected[0]['Id'] = 'sha256:' + 'c' * 64
            with self.assertRaises(ValueError):
                loader.collect(run, expected, inspected, binary, source)

    def test_collect_reports_candidate_and_reference_probe_failures(self):
        config = 'sha256:' + 'a' * 64
        layers = ['sha256:' + 'b' * 64]
        expected = {'config': config, 'diff_ids': layers}
        inspected = [{'Id': config, 'Os': 'linux', 'Architecture': 'amd64',
                      'RootFS': {'Layers': layers}, 'Config': {'Env': []}}]
        ref_inspect = [{'Id': loader.reference.CONFIG, 'Os': 'linux', 'Architecture': 'amd64',
                        'RepoDigests': [loader.reference.IMAGE], 'Config': {'Env': []}}]
        valid = b'VIPS_PROBE 1\nVERSION 8.10.0\nLOAD VipsForeignLoadA\nSAVE VipsForeignSaveB\nEND\n'
        for failing_image, reason in ((config, 'candidate-probe'),
                                      (loader.reference.CONFIG, 'reference-probe')):
            def run(argv, **kwargs):
                if argv[1:3] == ['image', 'inspect']:
                    return json.dumps(ref_inspect).encode()
                if argv[1] == 'run':
                    return b'private native output' if failing_image in argv else valid
                return b''
            with self.subTest(stage=reason), tempfile.TemporaryDirectory() as directory:
                binary, source = (pathlib.Path(directory) / name for name in ('binary', 'source'))
                binary.write_bytes(b'binary')
                source.write_bytes(b'source')
                with self.assertRaises(loader.DiagnosticError) as caught:
                    loader.collect(run, expected, inspected, binary, source)
                self.assertEqual(str(caught.exception), reason + ': invalid-data')

    def test_image_environment_rejects_loader_injection(self):
        loader.safe_environment([{'Config': {'Env': ['PATH=/usr/bin', 'HOME=/tmp']}}])
        for value in ('LD_PRELOAD=/tmp/x', 'VIPS_PATH=/tmp/x', 'LD_LIBRARY_PATH=/tmp'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                loader.safe_environment([{'Config': {'Env': [value]}}])

    def test_probe_sandbox_and_cleanup(self):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1] == 'run':
                return b'VIPS_PROBE 1\nVERSION 8.10.0\nLOAD VipsForeignLoadA\nSAVE VipsForeignSaveB\nEND\n'
            return b''
        loader.probe(run, 'sha256:' + 'a' * 64, pathlib.Path('/tmp/probe'))
        args = calls[1][0]
        for flag in ('--network=none', '--read-only', '--user=65534:65534', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges', '--pids-limit=128', '--memory=512m', '--cpus=1'):
            self.assertIn(flag, args)
        self.assertEqual(calls[1][1]['timeout'], 45)
        self.assertEqual(calls[1][1]['limit'], 1048576)
        self.assertIn('type=bind,src=/tmp/probe,dst=' + loader.PROBE + ',readonly', args)
        self.assertEqual(len([arg for arg in args if arg == '--mount']), 1)
        self.assertEqual(calls[-1][0][1:3], ['ps', '-a'])

    def test_cleanup_refuses_unrelated_container(self):
        def run(argv, **kwargs):
            if argv[1] == 'ps':
                return b'loader-inventory-test\n'
            if argv[1] == 'inspect':
                return b'[{"Config":{"Labels":{"imaginary.loader-probe":"other"}}}]'
            self.fail('must not remove unrelated container')
        with self.assertRaises(ValueError):
            loader.cleanup(run, 'loader-inventory-test', 'expected')

    def test_workflow_preserves_gate_and_mismatch_evidence(self):
        workflow = (PATH.parents[2] / '.github/workflows/runtime-image.yml').read_text()
        self.assertLess(workflow.index('validate-loaded --expected'), workflow.index('loader-probe" "$RUNNER_TEMP/evidence/artifact/loader-probe"'))
        self.assertLess(workflow.index('loader-inventory.json" || loader_status'), workflow.index('actions/upload-artifact@'))
        self.assertLess(workflow.index('actions/upload-artifact@'), workflow.index('Enforce loader comparison'))
        self.assertIn('cmp "$RUNNER_TEMP/one/loader-inventory.json" "$RUNNER_TEMP/two/loader-inventory.json"', workflow)

    def test_diagnostics_are_fixed_and_do_not_expose_exception_text(self):
        cases = ((loader.reference.ProbeExit(19), 'command-exit'),
                 (loader.reference.CommandError('private native error'), 'command-failed'),
                 (ValueError('private native error'), 'invalid-data'),
                 (OSError('private native error'), 'io-failed'),
                 (KeyError('private native error'), 'invalid-structure'))
        for error, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(loader.DiagnosticError) as caught:
                    with loader.stage('candidate-probe'):
                        raise error
                self.assertEqual(str(caught.exception), 'candidate-probe: ' + reason)
                self.assertNotIn('private', str(caught.exception))

    def test_main_prints_only_fixed_stage_and_reason(self):
        with mock.patch.object(loader.sys, 'argv', ['loader', '--expected', 'a', '--inspect', 'b',
                                                   '--binary', 'c', '--source', 'd', '--output', 'e']), \
             mock.patch.object(loader.reference, 'require_ci', side_effect=OSError('private error')), \
             mock.patch.object(loader.sys, 'stderr', new_callable=io.StringIO) as output:
            self.assertEqual(loader.main(), 1)
            self.assertEqual(output.getvalue(), 'loader inventory failed: setup: io-failed\n')

    def test_probe_failure_and_cleanup_failure_report_distinct_stages(self):
        def run(argv, **kwargs):
            if argv[1] == 'run':
                raise loader.reference.ProbeExit(1)
            return b''
        with self.assertRaises(loader.DiagnosticError) as caught:
            with loader.stage('candidate-probe'):
                loader.probe(run, 'image', pathlib.Path('/tmp/probe'))
        self.assertEqual(str(caught.exception), 'candidate-probe: command-exit')
        with mock.patch.object(loader, 'cleanup', side_effect=ValueError('secret')):
            with self.assertRaises(loader.DiagnosticError) as caught:
                with loader.stage('candidate-probe'):
                    loader.probe(run, 'image', pathlib.Path('/tmp/probe'))
        self.assertEqual(str(caught.exception), 'cleanup: invalid-data')

    def test_probe_only_maps_allowlisted_exit_codes(self):
        cases = {41: 'probe-setup', 42: 'probe-init', 43: 'probe-version',
                 44: 'probe-classes', 45: 'probe-library', 46: 'probe-stderr',
                 47: 'probe-protocol', 48: 'probe-output',
                 1: 'command-exit', 40: 'command-exit', 49: 'command-exit',
                 125: 'command-exit', 126: 'command-exit', 127: 'command-exit',
                 137: 'command-exit', -1: 'command-exit'}
        for code, reason in cases.items():
            def run(argv, **kwargs):
                if argv[1] == 'run':
                    raise loader.reference.ProbeExit(code)
                return b''
            with self.subTest(code=code):
                with self.assertRaises(loader.DiagnosticError) as caught:
                    with loader.stage('candidate-probe'):
                        loader.probe(run, 'image', pathlib.Path('/tmp/probe'))
                self.assertEqual(str(caught.exception), 'candidate-probe: ' + reason)

    def test_allowlisted_code_outside_probe_run_stays_generic(self):
        def run(argv, **kwargs):
            raise loader.reference.ProbeExit(41)
        with self.assertRaises(loader.DiagnosticError) as caught:
            with loader.stage('candidate-probe'):
                loader.probe(run, 'image', pathlib.Path('/tmp/probe'))
        self.assertEqual(str(caught.exception), 'candidate-probe: command-exit')

    def test_count_limit(self):
        lines = b''.join(('LOAD VipsForeignLoad%04d\n' % i).encode() for i in range(1025))
        with self.assertRaises(ValueError):
            loader.parse_protocol(b'VIPS_PROBE 1\nVERSION 8.10.0\n' + lines + b'END\n')


if __name__ == '__main__':
    unittest.main()
