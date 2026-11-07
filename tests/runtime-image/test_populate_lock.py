import copy
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[2] / 'tooling/runtime-image/populate_lock.py'
SPEC = importlib.util.spec_from_file_location('populate_lock', PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BASE = 'sha256:' + 'a' * 64
HASH = 'b' * 64


def fixture():
    lock = {'schema_version': 2, 'platform': 'linux/amd64',
            'builder_image': 'docker.io/library/debian@' + BASE,
            'runtime_image': 'docker.io/library/debian@' + BASE,
            'build_roots': ['a'], 'runtime_roots': ['a'],
            'build_packages': None, 'runtime_packages': None,
            'features': None, 'source': {'unchanged': True}, 'snapshot': {'date': '20240408T000000Z'}}
    package = {'Package': 'a', 'Version': '1', 'Architecture': 'amd64',
               'Filename': 'pool/a.deb', 'Size': '12', 'SHA256': HASH}
    row = {'repository': 'main', 'package': package,
           'origins': [{'repository': 'main', 'filename': 'pool/a.deb'}],
           'artifact_url': 'https://snapshot.debian.org/archive/debian/20240408T000000Z/pool/a.deb',
           'relations': {}}
    part = {'roots': ['a'], 'base': [['a', '1', 'amd64']],
            'pre': [['a', '1', 'amd64']], 'post': [['a', '1', 'amd64']],
            'solver': '', 'packages': [row], 'dependency_edges': []}
    doc = {'schema_version': 1, 'platform': 'linux/amd64', 'base_manifest': BASE,
           'closures': {'build': copy.deepcopy(part), 'runtime': copy.deepcopy(part)},
           'limitations': ['Base provenance does not prove installed deb bytes.',
                           'Unreviewed discovery is not a reproducible image.']}
    return lock, doc


class PopulateLockTests(unittest.TestCase):
    def test_maps_without_mutating_inputs(self):
        lock, doc = fixture()
        before = copy.deepcopy((lock, doc))
        result = MODULE.populate(lock, doc)
        self.assertEqual((lock, doc), before)
        self.assertEqual(result['features'], None)
        self.assertEqual(result['source'], lock['source'])
        self.assertEqual(result['build_packages'], [{
            'name': 'a', 'version': '1', 'architecture': 'amd64', 'repository': 'main',
            'url': doc['closures']['build']['packages'][0]['artifact_url'], 'size': 12,
            'sha256': HASH, 'dependencies': [], 'provenance': 'base'}])

    def test_virtual_roots_resolve_raw_installed_provides(self):
        lock, doc = fixture()
        for kind, root in (('build', 'fftw3-dev'), ('runtime', 'fftw3')):
            lock[kind + '_roots'] = [root]
            part = doc['closures'][kind]
            part['roots'] = [root]
            row = part['packages'][0]
            name = 'libfftw3-dev' if kind == 'build' else 'libfftw3-3'
            row['package'].update(Package=name, Provides=root + ' (= 3.3.8-2)')
            part['post'][0][0] = name
        before = copy.deepcopy((lock, doc))
        result = MODULE.populate(lock, doc)
        self.assertEqual(result['build_roots'], ['libfftw3-dev'])
        self.assertEqual(result['runtime_roots'], ['libfftw3-3'])
        self.assertEqual(result, MODULE.populate(lock, doc))
        self.assertEqual((lock, doc), before)

    def test_root_resolver_precedence_multiple_and_invalid(self):
        def row(name, provides=None):
            package = {'Package': name, 'Version': '1', 'Architecture': 'amd64'}
            if provides is not None:
                package['Provides'] = provides
            return {'package': package, 'relations': {'Provides': [[{'name': 'virtual'}]]}}
        rows = [row('z', 'virtual, virtual (= 1)'), row('b', 'virtual')]
        self.assertEqual(MODULE.resolve_roots(['virtual'], rows), ['b', 'z'])
        self.assertEqual(MODULE.resolve_roots(['virtual'], list(reversed(rows))), ['b', 'z'])
        self.assertEqual(MODULE.resolve_roots(['virtual', 'b'], rows), ['b', 'z'])
        self.assertEqual(MODULE.resolve_roots(['virtual'], rows + [row('virtual')]), ['virtual'])
        for bad in ('virtual:any', 'virtual | other', 'virtual (>= 1)', 'virtual (= !)',
                    'virtual,', '', 'virtual (= 1), bad:any', 42):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                MODULE.resolve_roots(['virtual'], [row('a', bad)])
        for roots, packages in ((['virtual'], [row('a')]), (['BAD'], rows),
                                (['virtual'], [row('BAD', 'virtual')])):
            with self.subTest(roots=roots, packages=packages), self.assertRaises(ValueError):
                MODULE.resolve_roots(roots, packages)

    def test_noninstalled_provider_cannot_resolve_root(self):
        lock, doc = fixture()
        lock['build_roots'] = ['virtual']
        part = doc['closures']['build']
        part['roots'] = ['virtual']
        provider = copy.deepcopy(part['packages'][0])
        provider['package'].update(Package='provider', Provides='virtual')
        part['packages'].append(provider)
        with self.assertRaises(ValueError):
            MODULE.populate(lock, doc)

    def test_provider_edges_and_snapshot_membership(self):
        lock, doc = fixture()
        for part in doc['closures'].values():
            item = copy.deepcopy(part['packages'][0])
            item['package'].update(Package='b', Filename='pool/b.deb')
            item['origins'][0]['filename'] = 'pool/b.deb'
            item['artifact_url'] = item['artifact_url'].replace('a.deb', 'b.deb')
            part['packages'].append(item)
            part['post'].append(['b', '1', 'amd64'])
            part['dependency_edges'].append({'source': {'name': 'a', 'version': '1', 'architecture': 'amd64'},
                 'provider': {'name': 'b', 'version': '1', 'architecture': 'amd64'},
                 'field': 'Depends', 'alternatives': [], 'choice': {}, 'via': 'b', 'provided_version': '1'})
            item = copy.deepcopy(item)
            item['package'].update(Package='c', Filename='pool/c.deb')
            item['origins'][0]['filename'] = 'pool/c.deb'
            item['artifact_url'] = item['artifact_url'].replace('b.deb', 'c.deb')
            part['packages'].append(item)
            part['post'].append(['c', '1', 'amd64'])
            edge = copy.deepcopy(part['dependency_edges'][0])
            edge['provider']['name'] = 'c'
            part['dependency_edges'].extend([edge, copy.deepcopy(edge)])
        result = MODULE.populate(lock, doc)
        for kind in ('build', 'runtime'):
            self.assertEqual(result[kind + '_packages'][0]['dependencies'], ['b', 'c'])
            self.assertEqual(result[kind + '_packages'][1]['provenance'], 'snapshot')

    def test_upgraded_base_package_is_not_marked_base(self):
        lock, doc = fixture()
        for part in doc['closures'].values():
            part['post'][0][1] = '2'
            part['packages'][0]['package']['Version'] = '2'
        result = MODULE.populate(lock, doc)
        self.assertEqual(result['build_packages'][0]['version'], '2')
        self.assertEqual(result['build_packages'][0]['provenance'], 'snapshot')
        self.assertEqual(result['runtime_packages'][0]['provenance'], 'snapshot')

    def test_equivalent_origins_do_not_claim_original_deb_origin(self):
        lock, doc = fixture()
        for part in doc['closures'].values():
            part['packages'][0]['origins'].append({'repository': 'security', 'filename': 'pool/security/a.deb'})
        result = MODULE.populate(lock, doc)
        self.assertEqual(result['build_packages'][0]['repository'], 'main')
        self.assertEqual(result['build_packages'][0]['provenance'], 'base')

    def test_duplicate_inventory_names_rejected(self):
        for field in ('base', 'pre', 'post'):
            lock, doc = fixture()
            doc['closures']['build'][field].append(['a', '2', 'amd64'])
            with self.subTest(field=field), self.assertRaises(ValueError):
                MODULE.populate(lock, doc)

    def test_edge_identity_must_match_exact_inventory(self):
        for side in ('source', 'provider'):
            for field, value in (('version', '2'), ('architecture', 'all')):
                lock, doc = fixture()
                ident = {'name': 'a', 'version': '1', 'architecture': 'amd64'}
                edge = {'source': ident.copy(), 'provider': ident.copy(), 'field': 'Depends',
                        'alternatives': [], 'choice': {}, 'via': 'a', 'provided_version': '1'}
                edge[side][field] = value
                doc['closures']['build']['dependency_edges'].append(edge)
                with self.subTest(side=side, field=field), self.assertRaises(ValueError):
                    MODULE.populate(lock, doc)

    def test_rejects_conflicting_or_incomplete_evidence(self):
        for mutate in (
            lambda l, d: d.update(schema_version=2),
            lambda l, d: d.update(base_manifest='sha256:' + 'f' * 64),
            lambda l, d: d['closures']['build']['roots'].append('missing'),
            lambda l, d: d['closures']['build']['post'].append(['missing', '1', 'amd64']),
            lambda l, d: d['closures']['build']['packages'].append(copy.deepcopy(d['closures']['build']['packages'][0])),
            lambda l, d: d['closures']['build']['packages'][0].update(artifact_url='https://evil.test/a.deb'),
            lambda l, d: d['closures']['build']['packages'][0]['origins'].clear(),
            lambda l, d: d['closures']['build']['dependency_edges'].append({'source': {'name': 'missing', 'version': '1', 'architecture': 'amd64'}, 'provider': {'name': 'a', 'version': '1', 'architecture': 'amd64'}, 'field': 'Depends', 'alternatives': [], 'choice': {}, 'via': 'a', 'provided_version': '1'}),
            lambda l, d: l.update(features=['invented']),
        ):
            lock, doc = fixture()
            mutate(lock, doc)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                MODULE.populate(lock, doc)


if __name__ == '__main__':
    unittest.main()
