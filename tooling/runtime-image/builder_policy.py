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


def require(condition):
    if not condition:
        raise ValueError('builder policy mismatch')


def identity(env):
    require(env.get('GITHUB_ACTIONS') == 'true')
    require(env.get('GITHUB_REPOSITORY', '').lower() == 'ogi4i/imaginary')
    require(env.get('GITHUB_REF') == 'refs/heads/mitigate-native-memory')
    require(re.fullmatch(r'[0-9a-f]{40}', env.get('GITHUB_SHA', '')) is not None)
    require(re.fullmatch(r'[1-9][0-9]{0,19}', env.get('GITHUB_RUN_ID', '')) is not None)
    require(re.fullmatch(r'[1-9][0-9]{0,9}', env.get('GITHUB_RUN_ATTEMPT', '')) is not None)
    require(env.get('GITHUB_WORKFLOW_REF') == env['GITHUB_REPOSITORY'] + '/.github/workflows/runtime-image.yml@refs/heads/mitigate-native-memory')


def validate_daemon(value, name, volume):
    require(isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict))
    d = value[0]
    try:
        c, h, n = d['Config'], d['HostConfig'], d['NetworkSettings']
        require(d['Name'] == '/' + name and d['State']['Running'] is True)
        require(c['Image'] == IMAGE and c['User'] in ('', '0', 'root'))
        require(c['Entrypoint'] == ['/usr/bin/buildkitd'] and c['Cmd'] == ['--config', '/dev/null', '--cdi-disabled'])
        require(h['Privileged'] is True and h['NetworkMode'] == 'bridge')
        for key in ('PortBindings', 'Binds', 'Devices', 'DeviceRequests', 'DeviceCgroupRules',
                    'CapAdd', 'CapDrop', 'SecurityOpt', 'GroupAdd', 'VolumesFrom'):
            require(h[key] in (None, [], {}))
        require(not h['PublishAllPorts'] and not h['NetworkMode'].startswith('container:'))
        for key in ('PidMode', 'UTSMode', 'UsernsMode'):
            require(h[key] == '')
        require(h['IpcMode'] in ('private', '') and h['CgroupnsMode'] in ('private', ''))
        require(h['Memory'] == 4294967296 and h['NanoCpus'] == 2000000000 and h['PidsLimit'] == 1024)
        require(isinstance(n['Ports'], dict) and all(v is None or v == [] for v in n['Ports'].values()))
        require(set(n['Networks']) == {'bridge'})
        mounts = d['Mounts']
        require(len(mounts) == 1 and mounts[0]['Type'] == 'volume' and mounts[0]['Name'] == volume
                and mounts[0]['Destination'] == '/var/lib/buildkit' and mounts[0]['RW'] is True)
    except (KeyError, TypeError, IndexError):
        raise ValueError('builder policy mismatch') from None


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
    except (ValueError, TypeError, OSError, json.JSONDecodeError):
        print('builder policy validation failed', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
