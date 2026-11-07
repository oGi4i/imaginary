import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('runtime_build', ROOT / 'tooling/runtime-image/build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildPreparationTests(unittest.TestCase):
    def test_closure_requires_complete_authenticated_inventories(self):
        with self.assertRaisesRegex(ValueError, 'closure'):
            build.validate_closure({}, [])

    def test_validator_result_requires_only_feature_gap(self):
        build.validate_lock_result(1, b'{"valid":true,"unresolved":["features"]}')
        for data in (b'{"valid":false,"unresolved":[]}',
                     b'{"valid":true,"unresolved":["build_packages"]}',
                     b'{"valid":true,"unresolved":[]}'):
            with self.assertRaises(ValueError):
                build.validate_lock_result(1, data)

    def test_installer_pins_all_roots_and_checks_inventory(self):
        script = build.install_script(['liba', 'libb'], [['liba', '1', 'amd64']])
        self.assertIn('liba libb', script)
        self.assertIn('apt-get check', script)
        self.assertIn("grep -qx 'APT_RESULT=ok' /tmp/apt-result", script)
        self.assertIn('mkdir -p /var/cache/apt/archives', script)
        self.assertIn('chmod 755 /var/cache/apt/archives', script)
        self.assertIn('dpkg-query', script)
        self.assertIn('diff -u', script)

    def test_dependency_edges_are_recomputed(self):
        with unittest.mock.patch.object(build.closure, 'resolve_edges', return_value=[{'source': {'name': 'a'}, 'provider': {'name': 'b'}}]):
            with self.assertRaisesRegex(ValueError, 'dependency'):
                build.validate_edges([], [], lambda *args: True)

    def test_authenticated_complete_provider_evidence_and_lock_union(self):
        packages = []
        for name in ('a', 'b', 'c'):
            pkg = {'Package': name, 'Version': '1', 'Architecture': 'amd64',
                   'Filename': 'pool/' + name + '.deb', 'Size': '4',
                   'SHA256': hashlib.sha256(b'data').hexdigest()}
            if name == 'a':
                pkg['Depends'] = 'b | c'
            packages.append(pkg)
        indexes = [('main', packages)]
        post = [[p['Package'], p['Version'], p['Architecture']] for p in packages]
        rows = build.closure.match_installed(post, indexes)
        given = []
        locks = []
        for row in rows:
            pkg = row['package']; name = pkg['Package']
            url = build.closure.archive_root('main') + pkg['Filename']
            given.append(dict(row, artifact_url=url, relations=build.closure.relations(pkg)))
            locks.append({'name': name, 'version': '1', 'architecture': 'amd64',
                          'repository': 'main', 'url': url, 'size': 4, 'sha256': pkg['SHA256'],
                          'provenance': 'base', 'dependencies': ['b', 'c'] if name == 'a' else []})
        edges = build.closure.resolve_edges(rows, lambda *args: True)
        part = {'post': post, 'base': post, 'pre': post, 'roots': ['a'],
                'packages': given, 'dependency_edges': edges}
        document = {'schema_version': 1, 'platform': 'linux/amd64', 'base_manifest': build.discovery.BASE,
                    'closures': {'build': copy.deepcopy(part), 'runtime': copy.deepcopy(part)}}
        lock = {'build_roots': ['a'], 'runtime_roots': ['a'],
                'build_packages': copy.deepcopy(locks), 'runtime_packages': copy.deepcopy(locks)}
        roots_patch = unittest.mock.patch.dict(build.closure.ROOTS, {'build': ('a',), 'runtime': ('a',)})
        roots_patch.start()
        self.addCleanup(roots_patch.stop)
        build.validate_closure(document, indexes, lock, lambda *args: True)
        mutations = [lambda e: e.pop(), lambda e: e.append(copy.deepcopy(e[0])),
                     lambda e: e[0]['provider'].update(name='a'),
                     lambda e: e[0]['provider'].update(version='2'),
                     lambda e: e[0].update(via='c'),
                     lambda e: e.append(dict(copy.deepcopy(e[0]), field='Pre-Depends'))]
        for mutate in mutations:
            changed = copy.deepcopy(document)
            mutate(changed['closures']['build']['dependency_edges'])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                build.validate_closure(changed, indexes, lock, lambda *args: True)
        incomplete = copy.deepcopy(lock)
        incomplete['build_packages'][0]['dependencies'].pop()
        with self.assertRaisesRegex(ValueError, 'dependencies'):
            build.validate_closure(document, indexes, incomplete, lambda *args: True)

    def test_virtual_roots_are_bound_to_authenticated_metadata(self):
        pkg = {'Package': 'provider', 'Version': '1', 'Architecture': 'amd64',
               'Filename': 'pool/provider.deb', 'Size': '4', 'SHA256': hashlib.sha256(b'data').hexdigest(),
               'Provides': 'virtual (= 1)'}
        indexes = [('main', [pkg])]
        post = [['provider', '1', 'amd64']]
        row = build.closure.match_installed(post, indexes)[0]
        url = build.closure.archive_root('main') + pkg['Filename']
        given = dict(row, artifact_url=url, relations=build.closure.relations(pkg))
        part = {'post': post, 'base': post, 'pre': post, 'roots': ['virtual'],
                'packages': [given], 'dependency_edges': []}
        document = {'schema_version': 1, 'platform': 'linux/amd64', 'base_manifest': build.discovery.BASE,
                    'closures': {'build': copy.deepcopy(part), 'runtime': copy.deepcopy(part)}}
        item = {'name': 'provider', 'version': '1', 'architecture': 'amd64', 'repository': 'main',
                'url': url, 'size': 4, 'sha256': pkg['SHA256'], 'provenance': 'base', 'dependencies': []}
        lock = {'build_roots': ['provider'], 'runtime_roots': ['provider'],
                'build_packages': [copy.deepcopy(item)], 'runtime_packages': [copy.deepcopy(item)]}
        with unittest.mock.patch.dict(build.closure.ROOTS, {'build': ('virtual',), 'runtime': ('virtual',)}):
            build.validate_closure(document, indexes, lock, lambda *args: True)
            altered = copy.deepcopy(document)
            altered['closures']['build']['roots'] = ['provider']
            with self.assertRaises(ValueError):
                build.validate_closure(altered, indexes, lock, lambda *args: True)
            altered = copy.deepcopy(lock)
            altered['build_roots'] = ['virtual']
            with self.assertRaises(ValueError):
                build.validate_closure(document, indexes, altered, lambda *args: True)
            altered = copy.deepcopy(document)
            altered['closures']['build']['packages'][0]['package']['Provides'] = 'other'
            with self.assertRaises(ValueError):
                build.validate_closure(altered, indexes, lock, lambda *args: True)
            changed_indexes = copy.deepcopy(indexes)
            changed_indexes[0][1][0]['Provides'] = 'other'
            changed_document = copy.deepcopy(document)
            changed_document['closures']['build']['packages'][0]['package']['Provides'] = 'other'
            changed_document['closures']['build']['packages'][0]['relations'] = build.closure.relations(changed_indexes[0][1][0])
            with self.assertRaises(ValueError):
                build.validate_closure(changed_document, changed_indexes, lock, lambda *args: True)

    def test_download_refuses_untrusted_url(self):
        with self.assertRaisesRegex(ValueError, 'URL'):
            build.fetch_github('https://evil.example/archive', 10)

    def test_archive_traversal_rejected(self):
        import io
        import tarfile
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            item = tarfile.TarInfo('../escape')
            item.size = 1
            archive.addfile(item, io.BytesIO(b'x'))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'archive'):
                build.extract_go(data.getvalue(), Path(tmp))

    def test_python_source_copy_rejects_untracked_changes(self):
        with unittest.mock.patch.object(build, 'run_bounded', return_value=b' M go.mod\0'):
            with self.assertRaisesRegex(ValueError, 'clean'):
                build.require_clean_source(Path('/tmp/source'))

    def test_recipe_sources_are_pinned(self):
        self.assertEqual(build.validate_recipe((ROOT / 'Dockerfile').read_text()), None)
        with self.assertRaisesRegex(ValueError, 'recipe'):
            build.validate_recipe('FROM debian:latest')

    def test_replacements_are_forbidden_before_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'go.mod').write_text('module example.org/x\nreplace example.org/a => ../bad\n')
            (p / 'go.sum').write_text('sum')
            config = {'go_mod_sha256': build.hash_file(p / 'go.mod'), 'go_sum_sha256': build.hash_file(p / 'go.sum')}
            with self.assertRaisesRegex(ValueError, 'replace'):
                build.validate_modules(p, config)
            (p / 'go.mod').write_text('altered')
            with self.assertRaisesRegex(ValueError, 'hash'):
                build.validate_modules(p, config)

    def test_base_inventory_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, 'base'):
            build.validate_base_inventory({'closures': {'build': {'base': [['a', '1', 'amd64']]}, 'runtime': {'base': []}}}, [('a', '2', 'amd64')])

    def test_oversized_buildx_rejected(self):
        tools = {'buildkit_image': build.BUILDKIT, 'frontend_image': build.FRONTEND, 'buildx': {'url': 'https://github.com/docker/buildx/releases/download/v0.21.2/buildx-v0.21.2.linux-amd64', 'size': 2**40, 'sha256': build.BUILDX_SHA256}}
        with self.assertRaisesRegex(ValueError, 'tool'):
            build.validate_tools(tools)

    def test_output_alias_cannot_be_inside_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'src'
            source.mkdir()
            (root / 'alias').symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'outside'):
                build.output_path(root / 'alias' / 'context', source)

    def test_workspaces_disabled_for_image_and_host(self):
        self.assertEqual(build.go_environment(Path('/tmp/g'), Path('/tmp/w'))['GOWORK'], 'off')
        self.assertIn('GOWORK=off', (ROOT / 'Dockerfile').read_text())

    def test_ci_identity_must_match(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_SHA': 'a' * 40, 'GITHUB_REPOSITORY': 'oGi4i/imaginary', 'GITHUB_REF': 'refs/heads/mitigate-native-memory'}
        build.validate_ci_identity(env, 'a' * 40)
        with self.assertRaisesRegex(ValueError, 'CI identity'):
            build.validate_ci_identity(env, 'b' * 40)

    def test_archive_attributes_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.gitattributes').write_text('secret export-ignore\n')
            with self.assertRaisesRegex(ValueError, 'attribute'):
                build.validate_attributes(root)

    def test_cleanup_failure_is_fatal(self):
        with unittest.mock.patch.object(build.closure.reference, 'bounded_run', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                build.remove_owned_container('runtime-build-base-test', {})

    def test_cleanup_has_budget_after_overall_deadline(self):
        with unittest.mock.patch.object(build.closure, '_OVERALL_END', 0, create=True), \
             unittest.mock.patch.object(build.closure.reference, 'bounded_run', side_effect=[b'removed', b'']) as run:
            build.remove_owned_container('runtime-build-base-test', {})
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all(call.kwargs['timeout'] > 0 for call in run.call_args_list))

    def test_cleanup_preserves_original_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'primary') as caught:
            try:
                raise RuntimeError('primary')
            finally:
                with build.closure.cleanup_stage('none'):
                    raise ValueError('cleanup')
        self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_recipe_mutated_build_flags_rejected(self):
        recipe = (ROOT / 'Dockerfile').read_text()
        for changed in (recipe.replace('-g -O3', '-g -O0'), recipe.replace('--disable-static', '--enable-static'), recipe.replace('-trimpath', '')):
            with self.assertRaisesRegex(ValueError, 'recipe'):
                build.validate_recipe(changed)

    def test_archived_lock_bytes_must_match_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'docs').mkdir()
            (root / 'docs/runtime-image.lock.yaml').write_bytes(b'archived')
            consumed = root / 'external.lock'
            consumed.write_bytes(b'other')
            with self.assertRaisesRegex(ValueError, 'lock'):
                build.validate_lock_source(consumed, root)
            consumed.write_bytes(b'archived')
            self.assertEqual(build.validate_lock_source(consumed, root), build.hash_file(consumed))

    def test_context_manifest_detects_input_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'input').write_bytes(b'a')
            before = build.context_manifest(root)
            (root / 'input').write_bytes(b'b')
            self.assertNotEqual(before, build.context_manifest(root))

    def test_wrong_payload_hash_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'digest'):
            build.verify_blob(b'bad', 3, '0' * 64)

    def test_wrong_payload_size_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'size'):
            build.verify_blob(b'bad', 2, '0' * 64)

    def test_missing_tool_pins_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'tool'):
            build.validate_tools({})

    def test_context_timestamps_are_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'file').write_text('x')
            build.normalize_times(path, 1234567890)
            self.assertEqual(int((path / 'file').stat().st_mtime), 1234567890)
            self.assertEqual(int(path.stat().st_mtime), 1234567890)

    def test_builder_evidence_is_copied_from_verification(self):
        recipe = (ROOT / 'Dockerfile').read_text()
        self.assertIn('builder-vips.txt', recipe)
        self.assertIn('artifact-sha256.txt', recipe)
        self.assertIn('COPY --from=verification /imaginary-build/', recipe)
        self.assertIn('vips --vips-config', recipe)

    def test_recipe_is_offline_and_has_both_targets(self):
        recipe = (ROOT / 'Dockerfile').read_text()
        self.assertIn(' AS verification', recipe)
        self.assertIn(' AS runtime', recipe)
        self.assertNotIn('curl ', recipe)
        self.assertNotIn('--without-rsvg', recipe)
        self.assertIn('USER nobody', recipe)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/imaginary"]', recipe)
        for line in recipe.splitlines():
            if line.startswith('RUN '):
                self.assertTrue(line.startswith('RUN --network=none '), line)


if __name__ == '__main__':
    unittest.main()
