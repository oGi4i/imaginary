import hashlib
import json
import signal
import os
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('verify_closure', ROOT / 'tooling/runtime-image/verify_closure.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class VerifyClosureTests(unittest.TestCase):
    def test_payload_authentication_and_limits(self):
        pkg = {'Package': 'a', 'Version': '1', 'Architecture': 'amd64',
               'Filename': 'pool/a.deb', 'Size': '4', 'SHA256': hashlib.sha256(b'data').hexdigest()}
        rows = {'build': ([{'repository': 'main', 'package': pkg}], []), 'runtime': ([], [])}
        with mock.patch.object(verify.closure, 'official_fetch', return_value=b'data') as fetch:
            verify.verify_payloads(rows)
            self.assertEqual(fetch.call_count, 1)
        with mock.patch.object(verify.closure, 'official_fetch', return_value=b'evil'):
            with self.assertRaises(ValueError):
                verify.verify_payloads(rows)
        pkg['Size'] = str(256 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            verify.verify_payloads(rows)

    def test_candidate_matches_populated_lock(self):
        source = {'bimg_patch': {'identity': 'synthetic'}}
        original = {'build_packages': None, 'runtime_packages': None, 'features': None, 'source': source}
        populated = {'build_packages': ['a'], 'runtime_packages': ['b'], 'features': None,
                     'source': source, 'build_roots': ['a'], 'runtime_roots': ['b']}
        with mock.patch.object(verify.build.populate_lock, 'populate', return_value=populated):
            self.assertEqual(verify.make_candidate(original, {}), populated)
            self.assertEqual(verify.make_candidate(populated, {}), populated)
            with self.assertRaises(ValueError):
                verify.make_candidate(dict(populated, build_packages=None), {})
            with self.assertRaises(ValueError):
                verify.make_candidate(dict(populated, build_roots=['wrong']), {})
            with self.assertRaises(ValueError):
                verify.make_candidate(dict(populated, source={'bimg_patch': {'identity': 'changed'}}), {})

    def test_publication_is_new_and_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'verified'
            verify.publish(output, b'candidate', b'report')
            self.assertEqual({p.name for p in output.iterdir()}, {'candidate-lock.json', 'verification.json'})
            with self.assertRaises(ValueError):
                verify.publish(output, b'other', b'other')

    def test_deadline_cleanup_and_real_publication(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            data = {name: root / name for name in ('closure', 'discovery', 'lock', 'keyring')}
            for path in data.values():
                path.write_bytes(b'{}')
            args = SimpleNamespace(source=source, output=root / 'output', commit='a' * 40,
                                   tree='b' * 40, **data)
            env = {'GITHUB_ACTIONS': 'true', 'GITHUB_SHA': args.commit, 'GITHUB_REPOSITORY': 'ogi4i/imaginary',
                   'GITHUB_REF': 'refs/heads/mitigate-native-memory', 'GITHUB_WORKFLOW_REF':
                   'ogi4i/imaginary/.github/workflows/runtime-inputs.yml@refs/heads/mitigate-native-memory',
                   'GITHUB_RUN_ID': '12', 'GITHUB_RUN_ATTEMPT': '1'}
            def copy_source(src, dest, commit, tree):
                (dest / 'docs').mkdir(parents=True)
                (dest / 'docs/runtime-image.lock.yaml').write_bytes(b'{}')
                (dest / '.github/workflows').mkdir(parents=True)
                (dest / verify.WORKFLOW).write_bytes(b'workflow')
                (source / '.github/workflows').mkdir(parents=True, exist_ok=True)
                (source / verify.WORKFLOW).write_bytes(b'workflow')
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(verify, 'EXPECTED_CLOSURE_SIZE', 2))
                stack.enter_context(mock.patch.object(verify, 'EXPECTED_CLOSURE_SHA256', verify.digest(b'{}')))
                stack.enter_context(mock.patch.object(verify.build, 'copy_source', side_effect=copy_source))
                stack.enter_context(mock.patch.object(verify.discovery, 'registry_manifest', return_value=None))
                pin = stack.enter_context(mock.patch.object(verify.build, 'validate_snapshot_pins', side_effect=ValueError('bad key')))
                with mock.patch.object(verify, 'publish') as published:
                    with self.assertRaises(ValueError):
                        verify.run(args, env)
                    published.assert_not_called()
                self.assertFalse(args.output.exists())
                def alarm(*unused, **kwargs):
                    os.kill(os.getpid(), signal.SIGALRM)
                pin.side_effect = alarm
                with mock.patch.object(verify, 'publish') as published:
                    with self.assertRaises(TimeoutError):
                        verify.run(args, env)
                    published.assert_not_called()
                self.assertFalse(args.output.exists())
                pin.side_effect = None
                stack.enter_context(mock.patch.object(verify.closure, 'authenticated_indexes'))
                stack.enter_context(mock.patch.object(verify.build, 'measure_base'))
                stack.enter_context(mock.patch.object(verify.build, 'validate_base_inventory'))
                stack.enter_context(mock.patch.object(verify.build, 'validate_closure_evidence'))
                stack.enter_context(mock.patch.object(verify, 'verify_payloads'))
                stack.enter_context(mock.patch.object(verify, 'make_candidate', return_value={}))
                stack.enter_context(mock.patch.object(verify.build, 'validate_closure'))
                real_temp = tempfile.TemporaryDirectory
                class FailedCleanup:
                    def __init__(self, *args, **kwargs):
                        self.context = real_temp(*args, **kwargs)
                    def __enter__(self):
                        return self.context.__enter__()
                    def __exit__(self, *args):
                        self.context.__exit__(*args)
                        raise OSError('cleanup failed')
                with mock.patch.object(verify.tempfile, 'TemporaryDirectory', FailedCleanup):
                    with mock.patch.object(verify, 'publish') as published:
                        with self.assertRaises(OSError):
                            verify.run(args, env)
                        published.assert_not_called()
                self.assertFalse(args.output.exists())
                real_cdll = verify.ctypes.CDLL
                real_libc = real_cdll(None, use_errno=True)
                def rename(*params):
                    self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
                    result = real_libc.renameat2(*params)
                    self.assertEqual(result, 0)
                    os.kill(os.getpid(), signal.SIGALRM)
                    return result
                old = signal.signal(signal.SIGALRM, lambda *_: None)
                try:
                    with mock.patch.object(verify.ctypes, 'CDLL', return_value=SimpleNamespace(renameat2=rename)):
                        verify.run(args, env)
                finally:
                    signal.signal(signal.SIGALRM, old)
                self.assertEqual({p.name for p in args.output.iterdir()}, {'candidate-lock.json', 'verification.json'})
                candidate = (args.output / 'candidate-lock.json').read_bytes()
                report = json.loads((args.output / 'verification.json').read_bytes())
                self.assertEqual(candidate, b'{}\n')
                self.assertEqual(report['candidate_sha256'], verify.digest(candidate))
                self.assertFalse(report['qualified'])

    def test_identity_rejects_wrong_workflow_and_run(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_SHA': 'a' * 40, 'GITHUB_REPOSITORY': 'ogi4i/imaginary',
               'GITHUB_REF': 'refs/heads/mitigate-native-memory', 'GITHUB_WORKFLOW_REF': 'ogi4i/imaginary/.github/workflows/runtime-inputs.yml@refs/heads/mitigate-native-memory',
               'GITHUB_RUN_ID': '12', 'GITHUB_RUN_ATTEMPT': '1'}
        verify.validate_identity(env, 'a' * 40)
        mixed = dict(env, GITHUB_REPOSITORY='oGi4i/imaginary', GITHUB_WORKFLOW_REF='oGi4i/imaginary/.github/workflows/runtime-inputs.yml@refs/heads/mitigate-native-memory')
        verify.validate_identity(mixed, 'a' * 40)
        for field in ('GITHUB_WORKFLOW_REF', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify.validate_identity(dict(env, **{field: '../wrong'}), 'a' * 40)

    def test_immutable_bounded_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input'
            path.write_bytes(b'abc')
            self.assertEqual(verify.read_input(path, 3), b'abc')
            with self.assertRaises(ValueError):
                verify.read_input(path, 2)
            alias = Path(tmp) / 'alias'
            alias.symlink_to(path)
            with self.assertRaises((ValueError, OSError)):
                verify.read_input(alias, 3)
