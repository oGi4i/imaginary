"""Offline tests for the hosted builder contract."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from contextlib import redirect_stderr
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('builder_policy', ROOT / 'tooling/runtime-image/builder_policy.py')
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


def daemon():
    return {'Name': '/daemon', 'Config': {'Image': policy.IMAGE, 'User': '',
            'Entrypoint': ['/usr/bin/buildkitd'], 'Cmd': ['--config', '/dev/null', '--cdi-disabled']},
            'HostConfig': {'Privileged': True, 'NetworkMode': 'bridge', 'PortBindings': {},
                'Binds': None, 'Devices': [], 'DeviceRequests': [], 'DeviceCgroupRules': None,
                'CapAdd': None, 'CapDrop': None, 'SecurityOpt': None, 'GroupAdd': None,
                'VolumesFrom': None, 'PublishAllPorts': False, 'PidMode': '', 'IpcMode': 'private',
                'UTSMode': '', 'UsernsMode': '', 'CgroupnsMode': 'private',
                'Memory': 4294967296, 'NanoCpus': 2000000000, 'PidsLimit': 1024},
            'NetworkSettings': {'Ports': {}, 'Networks': {'bridge': {}}},
            'Mounts': [{'Type': 'volume', 'Name': 'state', 'Destination': '/var/lib/buildkit', 'RW': True}],
            'State': {'Running': True}}


def observation():
    return {'Uid': '0 0 0 0', 'CapEff': '00000000a80425fb', 'CapPrm': '00000000a80425fb',
            'CapBnd': '00000000a80425fb', 'CapInh': '0000000000000000',
            'CapAmb': '0000000000000000', 'NoNewPrivs': '0', 'Seccomp': '2',
            'interfaces': ['lo'], 'apparmor': 'docker-default (enforce)'}


class BuilderPolicyTests(unittest.TestCase):
    def test_daemon_accepts_contract(self):
        policy.validate_daemon([daemon()], 'daemon', 'state')

    def test_daemon_rejects_mutations(self):
        mutations = [
            ('Config', 'Image', 'wrong'), ('Config', 'User', '1000'),
            ('Config', 'Cmd', ['--addr', 'tcp://0.0.0.0:1234']),
            ('Config', 'Cmd', ['--allow-insecure-entitlement', 'network.host']),
            ('HostConfig', 'Privileged', False), ('HostConfig', 'NetworkMode', 'host'),
            ('HostConfig', 'PortBindings', {'1234/tcp': [{'HostPort': '1234'}]}),
            ('HostConfig', 'Binds', ['/var/run/docker.sock:/var/run/docker.sock']),
            ('HostConfig', 'Devices', [{}]), ('HostConfig', 'PidMode', 'host'),
            ('HostConfig', 'Memory', 0), ('HostConfig', 'NanoCpus', -1),
            ('HostConfig', 'PidsLimit', 0), ('HostConfig', 'CapAdd', ['SYS_ADMIN']),
            ('NetworkSettings', 'Ports', {'1234/tcp': [{'HostPort': '1234'}]}),
            ('NetworkSettings', 'Networks', {'host': {}}),
        ]
        for section, field, value in mutations:
            with self.subTest(section=section, field=field, value=value):
                d = daemon(); d[section][field] = value
                with self.assertRaises(ValueError): policy.validate_daemon([d], 'daemon', 'state')
        for mount in ([], [{'Type': 'bind', 'Destination': '/var/lib/buildkit'}], daemon()['Mounts'] * 2):
            d = daemon(); d['Mounts'] = mount
            with self.assertRaises(ValueError): policy.validate_daemon([d], 'daemon', 'state')

    def test_daemon_reports_only_fixed_failure_codes(self):
        for section, field, value, reason in [
                ('Config', 'Image', 'https://private.invalid/token', 'image'),
                ('HostConfig', 'SecurityOpt', ['private-path-secret'], 'security_opt'),
                ('HostConfig', 'CgroupnsMode', 'host', 'cgroupns_mode')]:
            d = daemon(); d[section][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '^' + reason + '$'):
                policy.validate_daemon([d], 'daemon', 'state')
        d = daemon(); del d['HostConfig']['Binds']
        with self.assertRaisesRegex(ValueError, '^inspect_shape$'):
            policy.validate_daemon([d], 'daemon', 'state')
        stderr = io.StringIO()
        with mock.patch.object(policy, 'identity'), mock.patch.object(policy, 'read_bounded', return_value=json.dumps(d).encode()), \
                mock.patch('sys.argv', ['builder_policy', 'daemon', '--input', 'private-path',
                                      '--name', 'runtime-one-1-1-daemon', '--volume', 'runtime-one-1-1-daemon-state']), \
                redirect_stderr(stderr):
            self.assertEqual(policy.main(), 1)
        self.assertRegex(stderr.getvalue(), r'^builder policy validation failed reason=[a-z_]+\n$')
        self.assertNotIn('private', stderr.getvalue())

    def test_preflight_contract_and_mutations(self):
        policy.validate_preflight(observation())
        for field, value in [('Uid', '1000 1000 1000 1000'), ('CapEff', '0'),
                             ('CapPrm', '0'), ('CapBnd', '0'), ('CapInh', '1'),
                             ('CapAmb', '1'), ('NoNewPrivs', '1'), ('Seccomp', '0'),
                             ('interfaces', ['lo', 'eth0']), ('apparmor', 'arbitrary')]:
            with self.subTest(field=field):
                data = observation(); data[field] = value
                with self.assertRaises(ValueError): policy.validate_preflight(data)
        for data in ({}, {**observation(), 'extra': 'x'}, {**observation(), 'interfaces': 'lo'}):
            with self.assertRaises(ValueError): policy.validate_preflight(data)

    def test_preflight_input_bounds_and_duplicate_keys(self):
        data = observation()
        text = '\n'.join(f'{k}: {" ".join(v) if isinstance(v, list) else v}' for k, v in data.items())
        self.assertEqual(policy.parse_preflight(text.encode()), data)
        for bad in (text + '\nUid: 0 0 0 0', text + '\nextra: x', text.replace('CapEff:', 'CapEff: 1 #')):
            with self.assertRaises(ValueError): policy.parse_preflight(bad.encode())

    def test_identity_rejects_non_ci(self):
        with self.assertRaises(ValueError): policy.identity({})

    def test_workflow_orders_preflight_and_cleanup(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        self.assertNotIn('name: Block runtime image execution', workflow)
        self.assertIn('--driver remote', workflow)
        self.assertIn('docker-container://', workflow)
        self.assertNotIn('--driver docker-container', workflow)
        self.assertNotIn('--oci-worker-no-process-sandbox', workflow)
        self.assertLess(workflow.index('trap cleanup EXIT', workflow.index('name: Build and inspect')), workflow.index('docker create --name "$daemon"'))
        self.assertLess(workflow.index('builder_policy.py preflight'), workflow.index('docker buildx build --builder "$builder" --platform linux/amd64 --target runtime'))
        self.assertIn('docker volume rm', workflow)
        self.assertIn('docker volume inspect', workflow)


if __name__ == '__main__': unittest.main()
