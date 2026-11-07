#!/usr/bin/env python3
"""Fail closed on the approved hosted BuildKit daemon and RUN contract."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

IMAGE = 'docker.io/moby/buildkit@sha256:8c8514715aab54e12f65b6a38a219084ab926d49c52d519ac17a8e79befb9c75'
FRONTEND = 'docker.io/docker/dockerfile:1.9.0@sha256:dc9e236567481e0aca4c1f52351af213b9a176622f10e3f4a86e5cc48919fa01'
BASE = 'debian@sha256:1a43c9ee12ad03a221ffa57e9c41a73388b6cf4cb84c50c3ed734e9ce8ca3c6a'
FIELDS = {'Uid': '0 0 0 0', 'CapEff': '00000000a80425fb', 'CapPrm': '00000000a80425fb',
          'CapBnd': '00000000a80425fb', 'CapInh': '0000000000000000',
          'CapAmb': '0000000000000000', 'NoNewPrivs': '0', 'Seccomp': '2'}
APPARMOR = {'docker-default (enforce)', 'unconfined'}


REASONS = frozenset(('mismatch', 'inspect_shape', 'name', 'running', 'image', 'user',
                     'entrypoint', 'command', 'privileged', 'network_mode', 'port_bindings',
                     'binds', 'devices', 'device_requests', 'device_cgroup_rules', 'cap_add',
                     'cap_drop', 'security_opt', 'group_add', 'volumes_from', 'publish_ports',
                     'pid_mode', 'uts_mode', 'userns_mode', 'ipc_mode', 'cgroupns_mode',
                     'memory', 'cpus', 'pids_limit', 'network_ports', 'networks', 'mounts',
                     'invalid_input'))


def require(condition, reason='mismatch'):
    if not condition:
        raise ValueError(reason if reason in REASONS else 'mismatch')


def identity(env):
    require(env.get('GITHUB_ACTIONS') == 'true')
    require(env.get('GITHUB_REPOSITORY', '').lower() == 'ogi4i/imaginary')
    require(env.get('GITHUB_REF') == 'refs/heads/mitigate-native-memory')
    require(re.fullmatch(r'[0-9a-f]{40}', env.get('GITHUB_SHA', '')) is not None)
    require(re.fullmatch(r'[1-9][0-9]{0,19}', env.get('GITHUB_RUN_ID', '')) is not None)
    require(re.fullmatch(r'[1-9][0-9]{0,9}', env.get('GITHUB_RUN_ATTEMPT', '')) is not None)
    require(env.get('GITHUB_WORKFLOW_REF') == env['GITHUB_REPOSITORY'] + '/.github/workflows/runtime-image.yml@refs/heads/mitigate-native-memory')


def validate_daemon(value, name, volume):
    require(isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict), 'inspect_shape')
    d = value[0]
    try:
        c, h, n = d['Config'], d['HostConfig'], d['NetworkSettings']
        require(d['Name'] == '/' + name, 'name')
        require(d['State']['Running'] is True, 'running')
        require(c['Image'] == IMAGE, 'image')
        require(c['User'] in ('', '0', 'root'), 'user')
        require(c['Entrypoint'] == ['/usr/bin/buildkitd'], 'entrypoint')
        require(c['Cmd'] == ['--config', '/dev/null', '--cdi-disabled'], 'command')
        require(h['Privileged'] is True, 'privileged')
        require(h['NetworkMode'] == 'bridge', 'network_mode')
        for key, reason in (('PortBindings', 'port_bindings'), ('Binds', 'binds'),
                            ('Devices', 'devices'), ('DeviceRequests', 'device_requests'),
                            ('DeviceCgroupRules', 'device_cgroup_rules'), ('CapAdd', 'cap_add'),
                            ('CapDrop', 'cap_drop'), ('SecurityOpt', 'security_opt'),
                            ('GroupAdd', 'group_add'), ('VolumesFrom', 'volumes_from')):
            require(h[key] in (None, [], {}), reason)
        require(not h['PublishAllPorts'], 'publish_ports')
        for key, reason in (('PidMode', 'pid_mode'), ('UTSMode', 'uts_mode'), ('UsernsMode', 'userns_mode')):
            require(h[key] == '', reason)
        require(h['IpcMode'] in ('private', ''), 'ipc_mode')
        require(h['CgroupnsMode'] in ('private', ''), 'cgroupns_mode')
        require(h['Memory'] == 4294967296, 'memory')
        require(h['NanoCpus'] == 2000000000, 'cpus')
        require(h['PidsLimit'] == 1024, 'pids_limit')
        require(isinstance(n['Ports'], dict) and all(v is None or v == [] for v in n['Ports'].values()), 'network_ports')
        require(set(n['Networks']) == {'bridge'}, 'networks')
        mounts = d['Mounts']
        require(len(mounts) == 1 and mounts[0]['Type'] == 'volume' and mounts[0]['Name'] == volume
                and mounts[0]['Destination'] == '/var/lib/buildkit' and mounts[0]['RW'] is True, 'mounts')
    except (KeyError, TypeError, IndexError):
        raise ValueError('inspect_shape') from None


def validate_preflight(value):
    require(isinstance(value, dict) and set(value) == set(FIELDS) | {'interfaces', 'apparmor'})
    require(all(value[k] == v for k, v in FIELDS.items()))
    require(value['interfaces'] == ['lo'] and value['apparmor'] in APPARMOR)


def read_bounded(path, maximum=65536):
    with open(path, 'rb') as stream:
        data = stream.read(maximum + 1)
    require(len(data) <= maximum)
    return data


def parse_preflight(data):
    try:
        lines = data.decode('ascii').splitlines()
        require(len(lines) == len(FIELDS) + 2)
        result = {}
        for line in lines:
            key, value = line.split(':', 1)
            require(key not in result)
            result[key] = ' '.join(value.split())
        result['interfaces'] = result['interfaces'].split()
        validate_preflight(result)
        return result
    except (UnicodeError, ValueError, KeyError):
        raise ValueError('invalid preflight evidence') from None


def unique(pairs):
    obj = {}
    for k, v in pairs:
        require(k not in obj)
        obj[k] = v
    return obj


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('identity', 'daemon', 'preflight'))
    parser.add_argument('--input')
    parser.add_argument('--name')
    parser.add_argument('--volume')
    parser.add_argument('--report')
    args = parser.parse_args()
    try:
        identity(os.environ)
        if args.action == 'daemon':
            require(re.fullmatch(r'runtime-[a-z]+-[0-9]+-[0-9]+-daemon', args.name or '') is not None)
            require(args.volume == args.name + '-state')
            validate_daemon(json.loads(read_bounded(args.input), object_pairs_hook=unique), args.name, args.volume)
        elif args.action == 'preflight':
            result = parse_preflight(read_bounded(args.input, 4096))
            require(re.fullmatch(r'runtime-[a-z]+-[0-9]+-[0-9]+-daemon', args.name or '') is not None)
            report = {'source_sha': os.environ['GITHUB_SHA'], 'buildkit': IMAGE, 'frontend': FRONTEND,
                      'base': BASE, 'daemon': args.name, 'platform': 'linux/amd64', 'run': result}
            Path(args.report).write_text(json.dumps(report, sort_keys=True) + '\n')
    except (ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        reason = str(exc) if type(exc) is ValueError and str(exc) in REASONS else 'invalid_input'
        print('builder policy validation failed reason=' + reason, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
