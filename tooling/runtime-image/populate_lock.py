"""Map closure evidence into a lock; authentication is a separate build step.

This pure transformation does not read or write files or invoke commands. Base
membership describes an inventory, not the original provenance of a deb file.
"""
import copy
import re

_NAME = re.compile(r'[a-z0-9][a-z0-9+.-]{0,199}\Z')
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,199}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_PATH = re.compile(r'pool/[A-Za-z0-9+._~/\-]+\.deb\Z')
_BASE = {'main': 'debian', 'security': 'debian-security'}


def _identity(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or not all(isinstance(x, str) for x in value)
            or not _NAME.fullmatch(value[0]) or not _VERSION.fullmatch(value[1])
            or value[2] not in ('amd64', 'all')):
        raise ValueError('invalid package identity')
    return tuple(value)


def _inventory(value):
    if not isinstance(value, list) or not 0 < len(value) <= 10000:
        raise ValueError('invalid inventory')
    result = [_identity(item) for item in value]
    if len({item[0] for item in result}) != len(result):
        raise ValueError('duplicate inventory name')
    return result


def resolve_roots(roots, rows):
    """Resolve requested names against installed raw package metadata."""
    if (not isinstance(roots, list) or not roots
            or any(not isinstance(root, str) or not _NAME.fullmatch(root) for root in roots)
            or len(set(roots)) != len(roots)):
        raise ValueError('invalid roots')
    names, providers = set(), {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('package'), dict):
            raise ValueError('invalid package metadata')
        pkg = row['package']
        name, _, _ = _identity([pkg.get('Package'), pkg.get('Version'), pkg.get('Architecture')])
        if name in names:
            raise ValueError('duplicate package name')
        names.add(name)
        if 'Provides' not in pkg:
            continue
        value = pkg['Provides']
        if not isinstance(value, str):
            raise ValueError('invalid Provides')
        for atom in value.split(','):
            match = re.fullmatch(r'\s*([a-z0-9][a-z0-9+.-]{0,199})(?:\s*\(=\s*([A-Za-z0-9][A-Za-z0-9.+:~_-]{0,199})\))?\s*', atom)
            if not match:
                raise ValueError('invalid Provides')
            providers.setdefault(match[1], set()).add(name)
    resolved = set()
    for root in roots:
        if root in names:
            resolved.add(root)
        elif root in providers:
            resolved.update(providers[root])
        else:
            raise ValueError('missing root provider')
    return sorted(resolved)


def _edge_identity(value):
    if not isinstance(value, dict) or set(value) != {'name', 'version', 'architecture'}:
        raise ValueError('invalid edge identity')
    return _identity([value['name'], value['version'], value['architecture']])


def _packages(part, roots, date):
    if not isinstance(part, dict) or set(part) != {'roots', 'base', 'pre', 'post', 'solver', 'packages', 'dependency_edges'}:
        raise ValueError('invalid closure fields')
    if part['roots'] != roots:
        raise ValueError('closure roots mismatch')
    if not isinstance(part['solver'], str) or len(part['solver']) > 4 * 1024 * 1024:
        raise ValueError('invalid solver output')
    base, pre, post = (_inventory(part[k]) for k in ('base', 'pre', 'post'))
    if pre != base:
        raise ValueError('base inventory mismatch')
    rows = part['packages']
    if not isinstance(rows, list) or len(rows) != len(post):
        raise ValueError('package inventory mismatch')
    by_identity = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'repository', 'package', 'origins', 'artifact_url', 'relations'}:
            raise ValueError('invalid package row')
        pkg, repo, origins = row['package'], row['repository'], row['origins']
        if not isinstance(pkg, dict) or repo not in _BASE or not isinstance(row['relations'], dict):
            raise ValueError('invalid package metadata')
        identity = _identity([pkg.get('Package'), pkg.get('Version'), pkg.get('Architecture')])
        filename, size, sha = pkg.get('Filename'), pkg.get('Size'), pkg.get('SHA256')
        if (not isinstance(filename, str) or len(filename) > 1024 or not _PATH.fullmatch(filename)
                or any(p in ('', '.', '..') for p in filename.split('/'))
                or not isinstance(size, str) or not re.fullmatch(r'[0-9]{1,19}', size)
                or not 0 < int(size) <= 2**63 - 1 or not isinstance(sha, str) or not _SHA.fullmatch(sha)):
            raise ValueError('invalid artifact metadata')
        if not isinstance(origins, list) or not 0 < len(origins) <= 2:
            raise ValueError('invalid origins')
        seen = set()
        for origin in origins:
            if not isinstance(origin, dict) or set(origin) != {'repository', 'filename'} or origin['repository'] not in _BASE:
                raise ValueError('invalid origin')
            source = origin['filename']
            if (origin['repository'] in seen or not isinstance(source, str) or len(source) > 1024
                    or not _PATH.fullmatch(source) or any(p in ('', '.', '..') for p in source.split('/'))):
                raise ValueError('invalid origin')
            seen.add(origin['repository'])
        if repo != ('main' if 'main' in seen else 'security') or {'repository': repo, 'filename': filename} not in origins:
            raise ValueError('ambiguous canonical origin')
        url = 'https://snapshot.debian.org/archive/' + _BASE[repo] + '/' + date + '/' + filename
        if row['artifact_url'] != url or identity in by_identity:
            raise ValueError('invalid artifact location or duplicate')
        by_identity[identity] = {'name': identity[0], 'version': identity[1], 'architecture': identity[2],
                                 'repository': repo, 'url': url, 'size': int(size), 'sha256': sha,
                                 'dependencies': [], 'provenance': 'base' if identity in base else 'snapshot'}
    if set(post) != set(by_identity):
        raise ValueError('incomplete package inventory')
    resolved_roots = resolve_roots(roots, rows)
    edges = part['dependency_edges']
    if not isinstance(edges, list) or len(edges) > 100000:
        raise ValueError('invalid dependency edges')
    dependencies = {item: set() for item in by_identity}
    for edge in edges:
        if not isinstance(edge, dict) or set(edge) != {'source', 'provider', 'field', 'alternatives', 'choice', 'via', 'provided_version'}:
            raise ValueError('invalid edge')
        source, provider = _edge_identity(edge['source']), _edge_identity(edge['provider'])
        if source not in by_identity or provider not in by_identity or edge['field'] not in ('Depends', 'Pre-Depends'):
            raise ValueError('dangling edge')
        dependencies[source].add(provider[0])
    for identity, item in by_identity.items():
        item['dependencies'] = sorted(dependencies[identity])
    return sorted(by_identity.values(), key=lambda item: item['name']), resolved_roots


def populate(lock, document):
    """Return a new lock from closure evidence without authenticating it."""
    if not isinstance(lock, dict) or not isinstance(document, dict):
        raise ValueError('invalid inputs')
    if (lock.get('schema_version') != 2 or lock.get('platform') != 'linux/amd64'
            or lock.get('features') is not None or lock.get('build_packages') is not None
            or lock.get('runtime_packages') is not None):
        raise ValueError('unexpected lock state')
    if (set(document) != {'schema_version', 'platform', 'base_manifest', 'closures', 'limitations'}
            or document['schema_version'] != 1 or document['platform'] != lock['platform']
            or not isinstance(document['closures'], dict) or set(document['closures']) != {'build', 'runtime'}
            or not isinstance(document['limitations'], list)):
        raise ValueError('invalid closure document')
    manifest = document['base_manifest']
    if not isinstance(manifest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest):
        raise ValueError('invalid base manifest')
    for field in ('builder_image', 'runtime_image'):
        image = lock.get(field)
        if not isinstance(image, str) or not image.endswith('@' + manifest):
            raise ValueError('base manifest mismatch')
    snapshot = lock.get('snapshot')
    date = snapshot.get('date') if isinstance(snapshot, dict) else None
    if not isinstance(date, str) or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', date):
        raise ValueError('invalid snapshot date')
    result = copy.deepcopy(lock)
    for kind in ('build', 'runtime'):
        result[kind + '_packages'], result[kind + '_roots'] = _packages(
            document['closures'][kind], lock.get(kind + '_roots'), date)
    return result
