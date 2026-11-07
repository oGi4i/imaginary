"""Offline tests for OCI image evidence validation."""
import gzip
import hashlib
import importlib.util
import io
import struct
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('verify_image', ROOT / 'tooling/runtime-image/verify_image.py')
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def image(path, layer=b'layer', *, bad_digest=False, extra=None, compression='', diff_id=None, payload=None):
    blobs = {}

    def blob(data, media_type):
        digest = hashlib.sha256(data).hexdigest()
        blobs['blobs/sha256/' + digest] = data
        return {'mediaType': media_type, 'digest': 'sha256:' + digest, 'size': len(data)}

    compressed = gzip.compress(layer, mtime=0) if compression == '+gzip' else layer
    if payload is not None:
        compressed = payload
    layer_desc = blob(compressed, 'application/vnd.oci.image.layer.v1.tar' + compression)
    config = json.dumps({'architecture': 'amd64', 'os': 'linux', 'rootfs': {'type': 'layers', 'diff_ids': [diff_id or 'sha256:' + hashlib.sha256(layer).hexdigest()]}}, sort_keys=True).encode()
    config_desc = blob(config, 'application/vnd.oci.image.config.v1+json')
    manifest = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'config': config_desc, 'layers': [layer_desc]}, sort_keys=True).encode()
    manifest_desc = blob(manifest, 'application/vnd.oci.image.manifest.v1+json')
    manifest_desc['platform'] = {'architecture': 'amd64', 'os': 'linux'}
    index = json.dumps({'schemaVersion': 2, 'manifests': [manifest_desc]}).encode()
    if bad_digest:
        blobs['blobs/sha256/' + layer_desc['digest'][7:]] = b'wrong'
    with tarfile.open(path, 'w') as archive:
        entries = {'oci-layout': b'{"imageLayoutVersion":"1.0.0"}', 'index.json': index, **blobs}
        if extra:
            entries.update(extra)
        for name, data in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


class ImageTests(unittest.TestCase):
    def test_identical_images_match(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b = Path(temp) / 'a', Path(temp) / 'b'
            image(a)
            image(b)
            self.assertEqual(verify.inspect(a), verify.inspect(b))

    def test_changed_layer_differs(self):
        with tempfile.TemporaryDirectory() as temp:
            a, b = Path(temp) / 'a', Path(temp) / 'b'
            image(a)
            image(b, b'other')
            self.assertNotEqual(verify.inspect(a), verify.inspect(b))

    def test_diff_id_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'image'
            image(p, diff_id='sha256:' + '1' * 64)
            with self.assertRaises(ValueError):
                verify.inspect(p)

    def test_gzip_diff_id_and_truncation(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'image'
            image(p, compression='+gzip')
            verify.inspect(p)
            image(p, compression='+gzip', payload=gzip.compress(b'layer', mtime=0)[:-3])
            with self.assertRaises(ValueError):
                verify.inspect(p)

    def test_gzip_expansion_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'image'
            image(p, b'a' * 1024, compression='+gzip')
            original = verify.MAX_UNCOMPRESSED_LAYER
            try:
                verify.MAX_UNCOMPRESSED_LAYER = 100
                with self.assertRaises(ValueError):
                    verify.inspect(p)
            finally:
                verify.MAX_UNCOMPRESSED_LAYER = original

    def test_zstd_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'image'
            image(p, compression='+zstd')
            with self.assertRaises(ValueError):
                verify.inspect(p)

    def test_codec_response_validation(self):
        png = b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + struct.pack('>II', 1, 1) + b'\x08\x02\x00\x00\x00'
        jpeg = b'\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xd9'
        webp = b'RIFF' + struct.pack('<I', 22) + b'WEBPVP8X' + struct.pack('<I', 10) + bytes(10)
        for kind, data in [('png', png), ('jpeg', jpeg), ('webp', webp)]:
            with self.subTest(kind=kind):
                self.assertEqual(verify.image_dimensions(data, kind), (1, 1))
                with self.assertRaises(ValueError):
                    verify.image_dimensions(data[:-8], kind)
                with self.assertRaises(ValueError):
                    verify.validate_response(500, kind, data, 1, 1)
                with self.assertRaises(ValueError):
                    verify.validate_response(200, kind, data, 2, 1)
                verify.validate_response(200, kind, data, 1, 1)
        with self.assertRaises(ValueError):
            verify.image_dimensions(png, 'jpeg')

    def test_workflow_binds_builder_inventory(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        self.assertIn('builder-artifacts.sha256', workflow)
        self.assertIn('runtime-artifacts.sha256', workflow)
        self.assertIn('cmp "$RUNNER_TEMP/evidence/artifact/builder-artifacts.sha256" "$RUNNER_TEMP/evidence/artifact/runtime-artifacts.sha256"', workflow)
        self.assertIn('type=png', workflow)
        self.assertIn('runtime-response-check', workflow)
        self.assertIn('"$status" "$kind" "$RUNNER_TEMP/evidence/response"', workflow)
        checker = (ROOT / 'cmd/runtime-response-check/main.go').read_text()
        self.assertIn('png.Decode(', checker)
        self.assertIn('image bounds', checker)
        self.assertNotIn('-o /dev/null -X POST', workflow)

    def test_workflow_retains_context_manifest(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        self.assertIn('cp "$RUNNER_TEMP/runtime-context/context-manifest.json" "$RUNNER_TEMP/evidence/artifact/context-manifest.json"', workflow)

    def test_workflow_blocks_builder_execution(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        gate = workflow.index('name: Block runtime image execution')
        discovery = workflow.index('name: Discover verified inputs')
        self.assertLess(gate, discovery)
        self.assertIn('exit 1', workflow[gate:discovery])
        self.assertNotIn('if: always()', workflow[gate:discovery])

    def test_workflow_container_cleanup_contract(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        self.assertNotIn('docker run --rm', workflow)
        self.assertNotIn('id=$(timeout', workflow)
        self.assertIn('cleanup_containers()', workflow)
        self.assertIn('cleanup_builder()', workflow)
        self.assertIn('timeout -k 10 30 docker rm -f', workflow)
        self.assertIn('timeout -k 10 30 docker buildx rm', workflow)
        self.assertIn('trap cleanup EXIT', workflow)

    def test_untrusted_blob_digest_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'image'
            image(p, bad_digest=True)
            with self.assertRaises(ValueError):
                verify.inspect(p)

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'image'
            image(p)
            with tarfile.open(p, 'a') as archive:
                member = tarfile.TarInfo('blobs/sha256/link')
                member.type = tarfile.SYMTYPE
                member.linkname = '../../escape'
                archive.addfile(member)
            with self.assertRaises(ValueError):
                verify.inspect(p)

    def test_duplicate_entry_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'image'
            image(p)
            with tarfile.open(p, 'a') as archive:
                member = tarfile.TarInfo('index.json')
                member.size = 2
                archive.addfile(member, io.BytesIO(b'{}'))
            with self.assertRaises(ValueError):
                verify.inspect(p)

    def test_archive_size_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'image'
            image(p)
            old = verify.MAX_ARCHIVE
            try:
                verify.MAX_ARCHIVE = 1
                with self.assertRaises(ValueError):
                    verify.inspect(p)
            finally:
                verify.MAX_ARCHIVE = old

    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'image'
            image(p, extra={'../escape': b'bad'})
            with self.assertRaises(ValueError):
                verify.inspect(p)


if __name__ == '__main__':
    unittest.main()
