#!/usr/bin/env python3
"""Validate and compare the content-addressed objects in two OCI archives.

This does not execute an image, inspect layer contents, or establish trust in
its producer. In particular, equality here is not runtime qualification.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import sys
import tarfile

MAX_ARCHIVE = 4 * 1024 * 1024 * 1024
MAX_BLOB = 2 * 1024 * 1024 * 1024
MAX_JSON = 4 * 1024 * 1024
MAX_UNCOMPRESSED_LAYER = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 10000
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
CONFIG = 'application/vnd.oci.image.config.v1+json'
LAYERS = {'application/vnd.oci.image.layer.v1.tar',
          'application/vnd.oci.image.layer.v1.tar+gzip'}


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError('invalid JSON') from exc
    if not isinstance(value, dict):
        raise ValueError('expected JSON object')
    return value


def inspect(path):
    """Return identities of the single amd64 OCI manifest and its objects."""
    if path.stat().st_size > MAX_ARCHIVE:
        raise ValueError('archive exceeds size limit')
    entries = {}
    with tarfile.open(path, 'r:') as archive:
        for member in archive:
            name = member.name
            while name.startswith('./'):
                name = name[2:]
            parts = PurePosixPath(name).parts
            if (not name or name.startswith('/') or '..' in parts or '\\' in name
                    or len(parts) > 3):
                raise ValueError('unsafe archive path')
            if member.isdir():
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError('archive entry is not a regular file')
            if name in entries or len(entries) >= MAX_ENTRIES:
                raise ValueError('duplicate or excessive archive entries')
            if member.size > MAX_BLOB:
                raise ValueError('archive entry exceeds size limit')
            entries[name] = member

        def content(name, maximum):
            member = entries.get(name)
            if member is None or member.size > maximum:
                raise ValueError('missing or oversized archive entry: ' + name)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError('unreadable archive entry')
            data = stream.read(maximum + 1)
            if len(data) != member.size:
                raise ValueError('truncated archive entry')
            return data

        def descriptor(desc, media_types, json_blob=False):
            if not isinstance(desc, dict) or desc.get('mediaType') not in media_types:
                raise ValueError('unexpected descriptor type')
            digest, size = desc.get('digest'), desc.get('size')
            if (not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                    or type(size) is not int or size < 0 or size > MAX_BLOB):
                raise ValueError('invalid descriptor')
            name = 'blobs/sha256/' + digest[7:]
            member = entries.get(name)
            if member is None or member.size != size or (json_blob and size > MAX_JSON):
                raise ValueError('missing or wrong-sized blob')
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError('unreadable blob')
            hasher = hashlib.sha256()
            chunks = []
            total = 0
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                hasher.update(chunk)
                if json_blob:
                    chunks.append(chunk)
            if total != size or hasher.hexdigest() != digest[7:]:
                raise ValueError('blob digest mismatch')
            return _json(b''.join(chunks)) if json_blob else digest

        if _json(content('oci-layout', MAX_JSON)) != {'imageLayoutVersion': '1.0.0'}:
            raise ValueError('unsupported OCI layout')
        index = _json(content('index.json', MAX_JSON))
        manifests = index.get('manifests')
        if index.get('schemaVersion') != 2 or not isinstance(manifests, list) or len(manifests) != 1:
            raise ValueError('expected one image manifest')
        selected = manifests[0]
        if not isinstance(selected, dict) or selected.get('platform') != {'architecture': 'amd64', 'os': 'linux'}:
            raise ValueError('expected linux/amd64')
        manifest = descriptor(selected, {MANIFEST}, True)
        if manifest.get('schemaVersion') != 2 or manifest.get('mediaType') != MANIFEST:
            raise ValueError('invalid image manifest')
        config_desc = manifest.get('config')
        config = descriptor(config_desc, {CONFIG}, True)
        if config.get('architecture') != 'amd64' or config.get('os') != 'linux':
            raise ValueError('config platform mismatch')
        layers = manifest.get('layers')
        if not isinstance(layers, list) or len(layers) > 256:
            raise ValueError('invalid layers')
        layer_digests = [descriptor(layer, LAYERS) for layer in layers]
        rootfs = config.get('rootfs')
        if (not isinstance(rootfs, dict) or rootfs.get('type') != 'layers'
                or not isinstance(rootfs.get('diff_ids'), list)
                or len(rootfs['diff_ids']) != len(layers)
                or any(not isinstance(d, str) or not DIGEST.fullmatch(d) for d in rootfs['diff_ids'])):
            raise ValueError('invalid rootfs')
        for layer, expected in zip(layers, rootfs['diff_ids']):
            stream = archive.extractfile(entries['blobs/sha256/' + layer['digest'][7:]])
            if stream is None:
                raise ValueError('unreadable layer')
            if layer['mediaType'].endswith('+gzip'):
                stream = gzip.GzipFile(fileobj=stream)
            hasher = hashlib.sha256()
            total = 0
            try:
                while True:
                    chunk = stream.read(min(1024 * 1024, MAX_UNCOMPRESSED_LAYER + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UNCOMPRESSED_LAYER:
                        raise ValueError('expanded layer exceeds limit')
                    hasher.update(chunk)
            except (OSError, EOFError) as exc:
                raise ValueError('invalid compressed layer') from exc
            if 'sha256:' + hasher.hexdigest() != expected:
                raise ValueError('uncompressed layer digest mismatch')
        return {'manifest': selected['digest'], 'config': config_desc['digest'],
                'layers': layer_digests, 'diff_ids': rootfs['diff_ids']}


def image_dimensions(data, kind):
    """Read dimensions from a bounded HTTP response without a native decoder."""
    try:
        if kind == 'png' and data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 29:
            if data[8:16] != b'\x00\x00\x00\rIHDR':
                raise ValueError('invalid PNG header')
            return struct.unpack('>II', data[16:24])
        if kind == 'jpeg' and data[:2] == b'\xff\xd8':
            pos = 2
            while pos + 4 <= len(data):
                if data[pos] != 255:
                    break
                while pos < len(data) and data[pos] == 255:
                    pos += 1
                if pos >= len(data):
                    break
                marker = data[pos]
                pos += 1
                if marker in (0xd9, 0xda):
                    break
                if marker == 1 or 0xd0 <= marker <= 0xd7:
                    continue
                if pos + 2 > len(data):
                    break
                size = int.from_bytes(data[pos:pos + 2], 'big')
                if size < 2 or pos + size > len(data):
                    break
                if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf) and size >= 8:
                    return (int.from_bytes(data[pos + 5:pos + 7], 'big'),
                            int.from_bytes(data[pos + 3:pos + 5], 'big'))
                pos += size
        if kind == 'webp' and len(data) >= 30 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            if int.from_bytes(data[4:8], 'little') + 8 != len(data):
                raise ValueError('invalid WebP length')
            chunk = data[12:16]
            size = int.from_bytes(data[16:20], 'little')
            if 20 + size + (size % 2) > len(data):
                raise ValueError('truncated WebP chunk')
            if chunk == b'VP8X' and size >= 10:
                return (1 + int.from_bytes(data[24:27], 'little'), 1 + int.from_bytes(data[27:30], 'little'))
            if chunk == b'VP8 ' and size >= 10 and data[23:26] == b'\x9d\x01\x2a':
                return (int.from_bytes(data[26:28], 'little') & 0x3fff, int.from_bytes(data[28:30], 'little') & 0x3fff)
            if chunk == b'VP8L' and size >= 5 and data[20] == 0x2f:
                bits = int.from_bytes(data[21:25], 'little')
                return (1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff))
    except (IndexError, struct.error) as exc:
        raise ValueError('invalid image header') from exc
    raise ValueError('invalid or unsupported image header')


def validate_response(status, kind, data, width, height):
    if status != 200 or image_dimensions(data, kind) != (width, height):
        raise ValueError('unexpected response status or dimensions')


def main():
    if len(sys.argv) == 7 and sys.argv[1] == 'validate-response':
        _, _, status, kind, path, width, height = sys.argv
        try:
            response = Path(path)
            if response.stat().st_size > 1024 * 1024:
                raise ValueError('response exceeds limit')
            validate_response(int(status), kind, response.read_bytes(), int(width), int(height))
        except (OSError, ValueError) as exc:
            sys.exit('response verification failed: ' + str(exc))
        print(json.dumps({'format': kind, 'status': int(status), 'width': int(width), 'height': int(height)}, sort_keys=True))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('first', type=Path)
    parser.add_argument('second', type=Path)
    args = parser.parse_args()
    try:
        first, second = inspect(args.first), inspect(args.second)
        if first != second:
            raise ValueError('OCI image objects differ')
        print(json.dumps(first, sort_keys=True))
    except (OSError, tarfile.TarError, ValueError) as exc:
        parser.exit(1, 'image verification failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
