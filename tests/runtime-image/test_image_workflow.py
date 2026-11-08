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
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('verify_image', ROOT / 'tooling/runtime-image/verify_image.py')
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


def image(path, layer=b'layer', *, bad_digest=False, extra=None, compression='', diff_id=None, payload=None, second_layer=None):
    blobs = {}

    def blob(data, media_type):
        digest = hashlib.sha256(data).hexdigest()
        blobs['blobs/sha256/' + digest] = data
        return {'mediaType': media_type, 'digest': 'sha256:' + digest, 'size': len(data)}

    compressed = gzip.compress(layer, mtime=0) if compression == '+gzip' else layer
    if payload is not None:
        compressed = payload
    layer_desc = blob(compressed, 'application/vnd.oci.image.layer.v1.tar' + compression)
    layers = [layer_desc]
    diff_ids = [diff_id or 'sha256:' + hashlib.sha256(layer).hexdigest()]
    if second_layer is not None:
        layers.append(blob(second_layer, 'application/vnd.oci.image.layer.v1.tar'))
        diff_ids.append('sha256:' + hashlib.sha256(second_layer).hexdigest())
    config = json.dumps({'architecture': 'amd64', 'os': 'linux', 'rootfs': {'type': 'layers', 'diff_ids': diff_ids}}, sort_keys=True).encode()
    config_desc = blob(config, 'application/vnd.oci.image.config.v1+json')
    manifest = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'config': config_desc, 'layers': layers}, sort_keys=True).encode()
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


class LoadTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('image_load', ROOT / 'tooling/runtime-image/image_load.py')
        self.load = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.load)

    def test_raw_and_gzip_preserve_config_and_layer(self):
        for compression in ('', '+gzip'):
            with self.subTest(compression=compression), tempfile.TemporaryDirectory() as temp:
                source, output = Path(temp) / 'oci.tar', Path(temp) / 'docker.tar'
                image(source, compression=compression)
                identity = self.load.convert(source, output, 'runtime-experiment:one')
                with tarfile.open(source) as oci, tarfile.open(output) as docker:
                    manifest = json.load(docker.extractfile('manifest.json'))[0]
                    self.assertEqual(manifest['RepoTags'], ['runtime-experiment:one'])
                    self.assertEqual(docker.extractfile(manifest['Config']).read(), oci.extractfile('blobs/sha256/' + identity['config'][7:]).read())
                    self.assertEqual(docker.extractfile(manifest['Layers'][0]).read(), b'layer')
                self.assertEqual(identity['diff_ids'], ['sha256:' + hashlib.sha256(b'layer').hexdigest()])

    def test_layer_order_and_aggregate_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'oci.tar', Path(temp) / 'docker.tar'
            image(source, compression='+gzip', second_layer=b'second')
            identity = self.load.convert(source, output, 'runtime-experiment:two')
            with tarfile.open(output) as archive:
                manifest = json.load(archive.extractfile('manifest.json'))[0]
                self.assertEqual([archive.extractfile(name).read() for name in manifest['Layers']], [b'layer', b'second'])
            self.assertEqual(identity['diff_ids'], ['sha256:' + hashlib.sha256(data).hexdigest() for data in (b'layer', b'second')])
            output.unlink()
            self.load.MAX_OUTPUT = 1024
            with self.assertRaises(ValueError):
                self.load.convert(source, output, 'runtime-experiment:two')
            self.assertEqual(set(Path(temp).iterdir()), {source})

    def test_duplicate_and_oversized_input(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'oci.tar', Path(temp) / 'docker.tar'
            image(source)
            self.load.verify.MAX_ARCHIVE = 1
            with self.assertRaises(ValueError):
                self.load.convert(source, output, 'runtime-experiment:one')
            self.load.verify.MAX_ARCHIVE = 4 * 1024 * 1024 * 1024
            with tarfile.open(source, 'a') as archive:
                member = tarfile.TarInfo('index.json')
                member.size = 2
                archive.addfile(member, io.BytesIO(b'{}'))
            with self.assertRaises(ValueError):
                self.load.convert(source, output, 'runtime-experiment:one')
            self.assertEqual(set(Path(temp).iterdir()), {source})

    def test_reject_bad_archives_without_output_or_temp(self):
        for kwargs in ({'bad_digest': True}, {'diff_id': 'sha256:' + '1' * 64}, {'compression': '+zstd'}, {'compression': '+gzip', 'payload': gzip.compress(b'layer')[:-3]}, {'extra': {'../escape': b'x'}}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as temp:
                source, output = Path(temp) / 'oci.tar', Path(temp) / 'docker.tar'
                image(source, **kwargs)
                with self.assertRaises((ValueError, OSError, tarfile.TarError, EOFError)):
                    self.load.convert(source, output, 'runtime-experiment:one')
                self.assertEqual(set(Path(temp).iterdir()), {source})

    def test_existing_output_not_overwritten_including_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'oci.tar', Path(temp) / 'docker.tar'
            image(source)
            output.write_bytes(b'existing')
            with self.assertRaises(FileExistsError):
                self.load.convert(source, output, 'runtime-experiment:one')
            self.assertEqual(output.read_bytes(), b'existing')
            output.unlink()
            output.symlink_to('missing')
            with self.assertRaises(FileExistsError):
                self.load.convert(source, output, 'runtime-experiment:one')
            self.assertTrue(output.is_symlink())

    def test_mutated_consumed_blob_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'oci.tar', Path(temp) / 'docker.tar'
            image(source)
            original = self.load.verify.inspect

            def mutate(path, *, _validated):
                def callback(archive, entries, config, layers, identity):
                    member = entries['blobs/sha256/' + layers[0]['digest'][7:]]
                    with open(source, 'r+b') as stream:
                        stream.seek(member.offset_data)
                        stream.write(b'WRONG')
                    _validated(archive, entries, config, layers, identity)
                return original(path, _validated=callback)

            with mock.patch.object(self.load.verify, 'inspect', mutate):
                with self.assertRaises(ValueError):
                    self.load.convert(source, output, 'runtime-experiment:one')
            self.assertEqual(set(Path(temp).iterdir()), {source})

    def test_loaded_identity_checks_config_rootfs_and_platform(self):
        expected = {'config': 'sha256:' + '1' * 64, 'diff_ids': ['sha256:' + '2' * 64]}
        inspect = {'Id': expected['config'], 'RootFS': {'Layers': expected['diff_ids']}, 'Os': 'linux', 'Architecture': 'amd64'}
        self.load.validate_loaded(expected, [inspect])
        for field, wrong in [('Id', 'sha256:' + '3' * 64), ('RootFS', {'Layers': []}), ('Os', 'windows'), ('Architecture', 'arm64')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load.validate_loaded(expected, [{**inspect, field: wrong}])

    def test_workflow_gates_runtime_on_loaded_identity(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        convert = workflow.index('image_load.py convert')
        load = workflow.index('docker load -i "$RUNNER_TEMP/docker-load.tar"')
        identity = workflow.index('image_load.py validate-loaded')
        probe = workflow.index('docker run --name "$checks"')
        self.assertLess(convert, load)
        self.assertLess(load, identity)
        self.assertLess(identity, probe)
        self.assertNotIn('docker load -i "$RUNNER_TEMP/evidence/artifact/image.oci.tar"', workflow)


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

    def test_workflow_requires_builder_contract(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        self.assertLess(workflow.index('builder_policy.py preflight'),
                        workflow.index('docker buildx build --builder "$builder" --platform linux/amd64 --target runtime'))

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
