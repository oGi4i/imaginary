#!/usr/bin/env python3
"""Convert a validated OCI archive to a Docker load archive without executing it."""
import argparse
import ctypes
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import zlib

spec = importlib.util.spec_from_file_location('verify_image', Path(__file__).with_name('verify_image.py'))
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

MAX_OUTPUT = 4 * 1024 * 1024 * 1024
CHUNK = 1024 * 1024
TAG = re.compile(r'runtime-experiment:(?:one|two)\Z')


class HashedReader:
    def __init__(self, stream):
        self.stream = stream
        self.hash = hashlib.sha256()
        self.size = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.hash.update(data)
        self.size += len(data)
        return data


class BoundedWriter:
    def __init__(self, stream):
        self.stream = stream
        self.size = 0

    def write(self, data):
        if self.size + len(data) > MAX_OUTPUT:
            raise ValueError('output exceeds limit')
        self.size += len(data)
        return self.stream.write(data)

    def tell(self):
        return self.size

    def flush(self):
        self.stream.flush()


def convert(source, output, tag):
    """Validate and convert one image, publishing only a complete new archive."""
    source, output = Path(source), Path(output)
    if not TAG.fullmatch(tag):
        raise ValueError('invalid tag')
    # Reject even dangling symlinks. link() also enforces this at publication.
    if os.path.lexists(output):
        raise FileExistsError('output exists')
    fd, staging = tempfile.mkstemp(prefix='.image-load-', dir=output.parent)
    try:
        with os.fdopen(fd, 'w+b') as temp:
            def build(oci, entries, config_desc, layers, identity):
                def read_blob(desc):
                    stream = oci.extractfile(entries['blobs/sha256/' + desc['digest'][7:]])
                    if stream is None:
                        raise ValueError('missing blob')
                    return HashedReader(stream)

                def verify_blob(reader, desc):
                    if reader.size != desc['size'] or reader.hash.hexdigest() != desc['digest'][7:]:
                        raise ValueError('blob changed')

                def add(archive, name, stream, size):
                    member = tarfile.TarInfo(name)
                    member.size = size
                    member.mode = 0o600
                    archive.addfile(member, stream)

                config_stream = read_blob(config_desc)
                config = config_stream.read(verify.MAX_JSON + 1)
                verify_blob(config_stream, config_desc)
                config_name = config_desc['digest'][7:] + '.json'
                layer_names = [f'{i:03d}/layer.tar' for i in range(len(layers))]
                manifest = json.dumps([{'Config': config_name, 'RepoTags': [tag], 'Layers': layer_names}], separators=(',', ':')).encode()
                total = len(config) + len(manifest)
                with tarfile.open(fileobj=BoundedWriter(temp), mode='w|') as docker:
                    add(docker, config_name, io.BytesIO(config), len(config))
                    for layer, expected, name in zip(layers, identity['diff_ids'], layer_names):
                        compressed = read_blob(layer)
                        stream = gzip.GzipFile(fileobj=compressed) if layer['mediaType'].endswith('+gzip') else compressed
                        hasher = hashlib.sha256()
                        size = 0
                        with tempfile.TemporaryFile(dir=output.parent) as spool:
                            while True:
                                chunk = stream.read(min(CHUNK, verify.MAX_UNCOMPRESSED_LAYER + 1 - size, MAX_OUTPUT + 1 - total))
                                if not chunk:
                                    break
                                size += len(chunk)
                                total += len(chunk)
                                if size > verify.MAX_UNCOMPRESSED_LAYER or total > MAX_OUTPUT:
                                    raise ValueError('expanded data exceeds limit')
                                hasher.update(chunk)
                                spool.write(chunk)
                            verify_blob(compressed, layer)
                            if 'sha256:' + hasher.hexdigest() != expected:
                                raise ValueError('layer digest mismatch')
                            spool.seek(0)
                            add(docker, name, spool, size)
                    add(docker, 'manifest.json', io.BytesIO(manifest), len(manifest))
            identity = verify.inspect(source, _validated=build)
            temp.flush()
        # Linux renameat2 publishes atomically without replacing any existing path.
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(output), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        staging = None
        return identity
    finally:
        if staging is not None:
            os.unlink(staging)


def validate_loaded(expected, loaded):
    if (not isinstance(expected, dict) or not isinstance(expected.get('config'), str)
            or not verify.DIGEST.fullmatch(expected['config'])
            or not isinstance(expected.get('diff_ids'), list)
            or len(expected['diff_ids']) > 256
            or any(not isinstance(d, str) or not verify.DIGEST.fullmatch(d) for d in expected['diff_ids'])
            or not isinstance(loaded, list) or len(loaded) != 1 or not isinstance(loaded[0], dict)):
        raise ValueError('invalid identity')
    image = loaded[0]
    rootfs = image.get('RootFS')
    if (image.get('Id') != expected['config'] or image.get('Os') != 'linux'
            or image.get('Architecture') != 'amd64' or not isinstance(rootfs, dict)
            or rootfs.get('Layers') != expected['diff_ids']):
        raise ValueError('loaded image identity mismatch')


def read_json(path):
    with open(path, 'rb') as stream:
        data = stream.read(verify.MAX_JSON + 1)
    if len(data) > verify.MAX_JSON:
        raise ValueError('JSON exceeds limit')
    # Reuse the duplicate-key check while allowing Docker's top-level array.
    return verify._json(b'{"value":' + data + b'}')['value']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    convert_parser = sub.add_parser('convert')
    convert_parser.add_argument('--oci', type=Path, required=True)
    convert_parser.add_argument('--output', type=Path, required=True)
    convert_parser.add_argument('--tag', required=True)
    check = sub.add_parser('validate-loaded')
    check.add_argument('--expected', type=Path, required=True)
    check.add_argument('--inspect', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'convert':
            print(json.dumps(convert(args.oci, args.output, args.tag), sort_keys=True))
        else:
            validate_loaded(read_json(args.expected), read_json(args.inspect))
    except (OSError, EOFError, ValueError, tarfile.TarError, zlib.error):
        parser.exit(1, 'image load verification failed\n')


if __name__ == '__main__':
    main()
