import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('bimg_patch', ROOT / 'tooling/runtime-image/bimg_patch.py')
bimg_patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bimg_patch)


class BimgPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = self.root / 'module'
        self.module.mkdir()
        (self.module / 'resizer.go').write_bytes(b'original')
        (self.module / 'other.go').write_bytes(b'other')
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'go.mod').write_text('module example.com/app\n\nrequire (\n github.com/h2non/bimg v1.1.4\n)\n')
        (self.source / 'go.sum').write_text('sum\n')
        self.patchdir = self.source / 'patches/bimg-v1.1.4-webp'
        self.patchdir.mkdir(parents=True)
        (self.patchdir / 'resizer.go.txt').write_bytes(b'patched')
        self.identity = {'module': 'github.com/h2non/bimg', 'version': 'v1.1.4',
                         'source_url': 'https://example.invalid/resizer.go',
                         'original_sha256': hashlib.sha256(b'original').hexdigest(), 'original_size': 8,
                         'patched_sha256': hashlib.sha256(b'patched').hexdigest(), 'patched_size': 7}
        (self.patchdir / 'identity.json').write_text(json.dumps(self.identity))
        pin = patch.object(bimg_patch, 'IDENTITY', self.identity)
        pin.start()
        self.addCleanup(pin.stop)

    def stage(self):
        return bimg_patch.stage(self.source, self.module, self.root / 'stage', '/bimg-patch/module')

    def test_stage_preserves_files_and_uses_one_replace(self):
        self.stage()
        self.assertEqual((self.root / 'stage/module/resizer.go').read_bytes(), b'patched')
        self.assertEqual((self.root / 'stage/module/other.go').read_bytes(), b'other')
        self.assertEqual((self.root / 'stage/module/go.mod').read_bytes(), b'module github.com/h2non/bimg\n')
        self.assertFalse((self.module / 'go.mod').exists())
        self.assertIn(hashlib.sha256(b'module github.com/h2non/bimg\n').hexdigest() + '  module/go.mod',
                      (self.root / 'stage/manifest.sha256').read_text())
        text = (self.root / 'stage/patch.mod').read_text()
        self.assertEqual(text.count('replace '), 1)
        self.assertIn('replace github.com/h2non/bimg => /bimg-patch/module', text)
        self.assertEqual((self.root / 'stage/patch.sum').read_text(), 'sum\n')
        self.assertEqual((self.source / 'go.sum').read_text(), 'sum\n')

    def test_rejects_unbound_patch_identity_and_original(self):
        for path, content in [(self.patchdir / 'resizer.go.txt', b'changed'),
                              (self.module / 'resizer.go', b'changed'),
                              (self.patchdir / 'identity.json', b'{}')]:
            original = path.read_bytes()
            path.write_bytes(content)
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.stage()
            path.write_bytes(original)

    def test_rejects_module_mutation_during_copy(self):
        original = bimg_patch.tree
        calls = 0

        def changing_tree(path):
            nonlocal calls
            result = original(path)
            calls += 1
            if calls == 2:
                result['extra.go'] = b'unverified'
            return result

        with patch.object(bimg_patch, 'tree', side_effect=changing_tree):
            with self.assertRaises(ValueError):
                self.stage()

    def test_rejects_invalid_replacement_and_existing_output(self):
        for path in ('relative', '/tmp/../elsewhere', '/tmp/bad path'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                bimg_patch.stage(self.source, self.module, self.root / 'stage', path)
        (self.root / 'stage').mkdir()
        with self.assertRaises(ValueError):
            self.stage()

    def test_rejects_symlink_and_unapproved_replace(self):
        (self.module / 'link').symlink_to('other.go')
        with self.assertRaises((ValueError, OSError)):
            self.stage()
        (self.module / 'link').unlink()
        (self.source / 'go.mod').write_text('module example.com/app\nreplace x => y\n')
        with self.assertRaises(ValueError):
            self.stage()

    def test_rejects_existing_module_descriptor(self):
        (self.module / 'go.mod').write_bytes(b'module github.com/h2non/bimg\n')
        with self.assertRaises(ValueError):
            self.stage()

    def test_regular_file_rejects_symlinks_and_oversize(self):
        link = self.root / 'link'
        link.symlink_to(self.module / 'other.go')
        with self.assertRaises((OSError, ValueError)):
            bimg_patch.read_regular(link)
        with self.assertRaises(ValueError):
            bimg_patch.read_regular(self.module / 'other.go', 2)

    def test_recipe_rejects_every_original_file_mismatch(self):
        recipe = (ROOT / 'Dockerfile').read_text()
        self.assertIn('test ! -e go.mod', recipe)
        self.assertIn("printf 'module github.com/h2non/bimg\\n' | cmp - go.mod", recipe)
        self.assertIn('find . -type f ! -path ./go.mod', recipe)
        self.assertIn('cmp /tmp/bimg-original-files /tmp/bimg-staged-files', recipe)
        self.assertIn('cmp "$file" "/go/pkg/mod/github.com/h2non/bimg@v1.1.4/$file" || exit 1', recipe)

    def test_cli_redacts_errors(self):
        result = subprocess.run([sys.executable, '-I', str(ROOT / 'tooling/runtime-image/bimg_patch.py'),
                                 '--go', '/private/no-such-go', '--source', str(self.source),
                                 '--module', str(self.module), '--output', str(self.root / 'stage'),
                                 '--replace-path', '/bimg-patch/module'], capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b'bimg patch staging failed\n')
        self.assertEqual(result.stdout, b'')

    def test_cli_verifies_cache_before_staging(self):
        args = ['bimg_patch.py', '--go', '/mock/go', '--source', str(self.source),
                '--module', str(self.module), '--output', str(self.root / 'stage'),
                '--replace-path', '/bimg-patch/module']
        cache = self.root / 'cache'
        expected = cache / 'github.com/h2non/bimg@v1.1.4'
        args[args.index('--module') + 1] = str(expected)
        with patch('sys.argv', args), patch.object(bimg_patch.reference, 'bounded_run', return_value=(str(cache) + '\n').encode()) as run, \
                patch.object(bimg_patch, 'stage') as stage:
            bimg_patch.main()
            self.assertEqual(run.call_args_list[0].kwargs['limit'], 4096)
            self.assertEqual(run.call_args_list[1].args[0][-3:], ['/mock/go', 'mod', 'verify'])
            self.assertEqual(run.call_args_list[1].kwargs['env']['GOFLAGS'], '-mod=readonly')
            stage.assert_called_once()
        args[args.index('--module') + 1] = str(self.module)
        with patch('sys.argv', args), patch.object(bimg_patch.reference, 'bounded_run', return_value=(str(cache) + '\n').encode()), \
                patch.object(bimg_patch, 'stage') as stage:
            with self.assertRaises(ValueError):
                bimg_patch.main()
            stage.assert_not_called()

    def test_cli_runs_fake_go_with_workflow_environment(self):
        cache = self.root / 'cache'
        expected = cache / 'github.com/h2non/bimg@v1.1.4'
        expected.parent.mkdir(parents=True)
        shutil.copytree(self.module, expected)
        fake = self.root / 'fake-go'
        log = self.root / 'calls'
        fake.write_text(f'''#!{sys.executable} -I
import os, sys
assert os.getcwd() == {str(self.source)!r}
assert os.environ['GOWORK'] == 'off'
assert os.environ['GOFLAGS'] == '-mod=readonly'
assert os.environ['GO111MODULE'] == 'on'
with open({str(log)!r}, 'a') as out:
    out.write(' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1:] == ['env', 'GOMODCACHE']:
    print({str(cache)!r})
elif sys.argv[1:] != ['mod', 'verify']:
    sys.exit(1)
''')
        fake.chmod(0o755)
        args = ['bimg_patch.py', '--go', str(fake), '--source', str(self.source),
                '--module', str(expected), '--output', str(self.root / 'stage'),
                '--replace-path', '/bimg-patch/module']
        with patch('sys.argv', args):
            bimg_patch.main()
        self.assertEqual((self.root / 'stage/module/go.mod').read_bytes(), b'module github.com/h2non/bimg\n')
        self.assertEqual(log.read_text().splitlines(), ['env GOMODCACHE', 'mod verify', 'mod verify'])

    def test_committed_lock_binds_exact_patch(self):
        lock = json.loads((ROOT / 'docs/runtime-image.lock.yaml').read_text())
        self.assertEqual(lock['source']['bimg_patch'], json.loads((ROOT / 'patches/bimg-v1.1.4-webp/identity.json').read_text()))

    def test_patch_source_is_not_discovered_as_root_go_package(self):
        self.assertEqual(list((ROOT / 'patches/bimg-v1.1.4-webp').glob('*.go')), [])
