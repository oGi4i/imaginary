import importlib.util
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / 'tooling/runtime-image/reference_inventory.py'
spec = importlib.util.spec_from_file_location('reference_inventory', SCRIPT)
inv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inv)


class InventoryTests(unittest.TestCase):
    def inspect(self):
        return json.dumps([{'Id': inv.CONFIG, 'Os': 'linux', 'Architecture': 'amd64',
                            'RepoDigests': [inv.IMAGE], 'Config': {'Volumes': None}}]).encode()

    def runner(self, overrides=None):
        overrides = overrides or {}
        def run(argv, **kwargs):
            if argv[1:3] == ['image', 'inspect']:
                return overrides.get('inspect', self.inspect())
            if argv[1] == 'pull':
                return b''
            if argv[1] == 'rm':
                return b''
            if argv[1] == 'run':
                probe = next(name for name, command in inv.PROBES.items() if argv[-1] == command)
                value = overrides.get(probe, {
                    'packages': b'ii \tlibc6\t2.28-10\tamd64\n',
                    'libc': b'2.28\n',
                    'vips_version': b'vips-8.10.0\n',
                    'vips_config': b'jpeg: yes\npng: no\n',
                    'loaders': b'VipsForeignLoadJpeg\n',
                    'elf': b'Dynamic section\n 0x0001 (NEEDED) Shared library: [libvips.so.42]\n',
                    'vips_elf': b'Dynamic section\n 0x0001 (NEEDED) Shared library: [libc.so.6]\n',
                    'dependencies': b'libvips.so.42 => /usr/local/lib/libvips.so.42\n',
                    'vips_dependencies': b'libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6\n',
                }[probe])
                if isinstance(value, Exception):
                    raise value
                return value
            raise AssertionError(argv)
        return run

    def test_complete_inventory_and_sandbox(self):
        calls = []
        base = self.runner()
        def run(argv, **kw):
            calls.append(argv)
            return base(argv, **kw)
        result = inv.collect(run)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['packages'][0]['name'], 'libc6')
        self.assertEqual(result['identity']['config'], inv.CONFIG)
        for cmd in (c for c in calls if c[1] == 'run'):
            for option in ('--network=none', '--read-only', '--cap-drop=ALL',
                           '--security-opt=no-new-privileges', '--user=65534:65534'):
                self.assertIn(option, cmd)
            self.assertIn('--entrypoint=/bin/sh', cmd)
            self.assertEqual(cmd[-3:-1], [inv.CONFIG, '-c'])
            self.assertIn('--pull=never', cmd)
            self.assertNotIn('--privileged', cmd)
        self.assertEqual(len([c for c in calls if c[1] == 'rm']), len(inv.PROBES))

    def test_rejects_wrong_identity_before_execution(self):
        for key, value in [('Id', 'sha256:' + '0' * 64), ('Os', 'windows'),
                           ('Architecture', 'arm64'), ('RepoDigests', []),
                           ('Config', {'Volumes': {'/data': {}}})]:
            image = json.loads(self.inspect())
            image[0][key] = value
            calls = []
            base = self.runner({'inspect': json.dumps(image).encode()})
            def run(argv, **kw):
                calls.append(argv)
                return base(argv, **kw)
            with self.subTest(key=key), self.assertRaises(inv.InventoryError):
                inv.collect(run)
            self.assertFalse(any(c[1] == 'run' for c in calls))

    def test_invalid_inspect_and_required_packages_fail(self):
        for override in ({'inspect': b'not json'}, {'packages': b'bad'},
                         {'packages': inv.CommandError('exit')}):
            with self.subTest(override=override), self.assertRaises(inv.InventoryError):
                inv.collect(self.runner(override))

    def test_optional_unavailable_or_malformed_is_incomplete(self):
        for override in ({'loaders': inv.ProbeExit(127)}, {'elf': b'not elf'},
                         {'dependencies': b'libx.so => not found\n'},
                         {'libc': b'secret-with-invalid-format\n'}):
            with self.subTest(override=override):
                result = inv.collect(self.runner(override))
                self.assertEqual(result['status'], 'incomplete')
                self.assertNotIn('secret-with-invalid-format', json.dumps(result))
                self.assertEqual(len(result['packages']), 1)

    def test_probe_failure_still_removes_container(self):
        calls = []
        base = self.runner({'loaders': inv.CommandError('timeout')})
        def run(argv, **kw):
            calls.append(argv)
            return base(argv, **kw)
        with self.assertRaises(inv.CommandError):
            inv.collect(run)
        runs = [c for c in calls if c[1] == 'run']
        removals = [c for c in calls if c[1] == 'rm']
        self.assertEqual([c[c.index('--name') + 1] for c in runs], [c[-1] for c in removals])

    def test_adapter_failures_are_fatal_not_unavailable(self):
        for reason in ('timeout', 'size', 'unavailable', 'could not reap'):
            with self.subTest(reason=reason), self.assertRaises(inv.CommandError):
                inv.collect(self.runner({'loaders': inv.CommandError(reason)}))

    def test_cleanup_failure_is_fatal(self):
        base = self.runner()
        def run(argv, **kw):
            if argv[1] == 'rm':
                raise inv.CommandError('cleanup failed')
            return base(argv, **kw)
        with self.assertRaises(inv.InventoryError):
            inv.collect(run)

    def test_ci_guard_precedes_any_subprocess(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(inv, 'bounded_run') as run:
            with self.assertRaises(inv.InventoryError):
                inv.require_ci()
            run.assert_not_called()

    def test_docker_launch_failure_is_fatal(self):
        for code in (125, 126, -9):
            with self.subTest(code=code), self.assertRaises(inv.ProbeExit):
                inv.collect(self.runner({'loaders': inv.ProbeExit(code)}))

    def test_exited_parent_with_open_pipe_kills_group_on_timeout(self):
        proc = mock.Mock()
        proc.pid = 1234
        proc.poll.return_value = 0
        selector = mock.MagicMock()
        selector.select.return_value = []
        with mock.patch.object(subprocess, 'Popen', return_value=proc), \
             mock.patch.object(inv.selectors, 'DefaultSelector', return_value=selector), \
             mock.patch.object(inv.os, 'killpg') as kill, \
             mock.patch.object(inv.time, 'monotonic', side_effect=[0, 0, 2]):
            with self.assertRaises(inv.CommandError):
                inv.bounded_run(['docker', 'version'], timeout=1, limit=8, env={})
            kill.assert_called_once_with(1234, inv.signal.SIGKILL)
            proc.wait.assert_called()

    def test_selector_setup_failure_kills_reaps_and_closes_pipe(self):
        for stage in ('construct', 'register'):
            proc = mock.Mock()
            proc.pid = 1234
            selector = mock.MagicMock()
            if stage == 'register':
                selector.register.side_effect = OSError('private error')
            construction = OSError('private error') if stage == 'construct' else selector
            with self.subTest(stage=stage), \
                 mock.patch.object(subprocess, 'Popen', return_value=proc), \
                 mock.patch.object(inv.selectors, 'DefaultSelector', side_effect=construction if stage == 'construct' else None, return_value=selector), \
                 mock.patch.object(inv.os, 'killpg') as kill:
                with self.assertRaises(inv.CommandError) as failure:
                    inv.bounded_run(['docker', 'version'], env={})
                self.assertNotIn('private error', str(failure.exception))
                kill.assert_called_once_with(1234, inv.signal.SIGKILL)
                proc.wait.assert_called_once_with(timeout=5)
                proc.stdout.close.assert_called_once()
                if stage == 'register':
                    selector.close.assert_called_once()

    def test_reap_failure_is_fatal_and_keeps_timeout_context(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired('docker', 5)
        selector = mock.MagicMock()
        selector.select.return_value = []
        with mock.patch.object(subprocess, 'Popen', return_value=proc), \
             mock.patch.object(inv.selectors, 'DefaultSelector', return_value=selector), \
             mock.patch.object(inv.os, 'killpg'), \
             mock.patch.object(inv.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaises(inv.CommandError) as failure:
                inv.bounded_run(['docker', 'version'], timeout=1, limit=8, env={})
            self.assertEqual(str(failure.exception), 'command reap failed')
            self.assertIsInstance(failure.exception.__context__, subprocess.TimeoutExpired)
            self.assertIsInstance(failure.exception.__context__.__context__, inv.CommandError)
            proc.wait.assert_called_once_with(timeout=5)

    def test_process_exit_timeout_and_output_limit(self):
        for outcome in ('exit', 'timeout', 'size'):
            proc = mock.Mock()
            proc.poll.return_value = 1 if outcome == 'exit' else None
            proc.wait.return_value = 1
            proc.stdout.fileno.return_value = 12
            selector = mock.MagicMock()
            selector.select.return_value = [] if outcome == 'timeout' else [(mock.Mock(), 1)]
            data = b'x' * 9 if outcome == 'size' else b''
            clock = [0, 2] if outcome == 'timeout' else [0, 0, 0]
            with self.subTest(outcome=outcome), \
                 mock.patch.object(subprocess, 'Popen', return_value=proc), \
                 mock.patch.object(inv.selectors, 'DefaultSelector', return_value=selector), \
                 mock.patch.object(inv.os, 'read', return_value=data), \
                 mock.patch.object(inv.os, 'killpg') as kill, \
                 mock.patch.object(inv.time, 'monotonic', side_effect=clock):
                with self.assertRaises(inv.CommandError):
                    inv.bounded_run(['docker', 'version'], timeout=1, limit=8, env={})
                proc.wait.assert_called()
                if outcome in ('timeout', 'size'):
                    kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
