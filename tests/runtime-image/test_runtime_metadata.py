import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / 'Dockerfile'
FONT_DIRS = ('/usr/local/share/fonts', '/usr/share/fonts',
             '/usr/share/fonts/truetype', '/usr/share/fonts/truetype/dejavu')
UUIDS = tuple(f'00000000-0000-4000-8000-{number:012d}' for number in range(1, 5))
LOGS = ('/var/log/alternatives.log', '/var/log/dpkg.log',
        '/var/log/apt/history.log', '/var/log/apt/term.log')
AUX = '/var/cache/ldconfig/aux-cache'


def runtime_runs():
    runtime = DOCKERFILE.read_text().split(' AS runtime\n', 1)[1]
    instructions = runtime.replace('\\\n', '').splitlines()
    return [line.removeprefix('RUN --network=none ') for line in instructions
            if line.startswith('RUN --network=none ')]


class RuntimeMetadataTests(unittest.TestCase):
    def run_install(self, *, missing_uuid=False, symlink_uuid=False, missing_cache=False,
                    empty_cache=False, invalid_cache=False, install_fails=False):
        script = runtime_runs()[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for directory, uuid in zip(FONT_DIRS, UUIDS):
                path = root / directory.lstrip('/') / '.uuid'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('random')
            if missing_uuid:
                (root / FONT_DIRS[0].lstrip('/') / '.uuid').unlink()
            if symlink_uuid:
                path = root / FONT_DIRS[0].lstrip('/') / '.uuid'
                path.unlink()
                path.symlink_to(root / FONT_DIRS[1].lstrip('/') / '.uuid')
            cache = root / 'var/cache/fontconfig'
            cache.mkdir(parents=True)
            (cache / 'old-le64.cache-7').write_text('old')
            (cache / 'keep-other.cache').write_text('keep')
            for name in (*LOGS, AUX, '/var/log/keep.log', '/etc/ld.so.cache'):
                path = root / name.lstrip('/')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('keep')
            (root / 'install.sh').write_text('exit ' + ('1' if install_fails else '0') + '\n')
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            fc = bin_dir / 'fc-cache'
            dirs = [root / directory.lstrip('/') for directory in FONT_DIRS]
            fc.write_text('#!/bin/sh\n[ "$*" = "-fs" ] || exit 9\n'
                          + '\n'.join(f'[ "$(stat -c %Y {directory})" = "$SOURCE_DATE_EPOCH" ] || exit 8'
                                      for directory in dirs) + '\n'
                          + '\n'.join(f"printf cache > '{cache}/{uuid}-le64.cache-7'"
                                      for uuid in UUIDS[:3 if missing_cache else 4])
                          + (f"\n: > '{cache}/{UUIDS[3]}-le64.cache-7'" if empty_cache else '') + '\n')
            fc.chmod(0o755)
            cat = bin_dir / 'fc-cat'
            cat.write_text('#!/bin/sh\nexit ' + ('1' if invalid_cache else '0') + '\n')
            cat.chmod(0o755)
            prefixes = ('/usr/local/share/fonts', '/usr/share/fonts', '/var/cache/fontconfig',
                        '/var/cache/ldconfig', '/var/log', '/var/lib/apt/lists', '/mirror', '/install.sh')
            script = re.sub('|'.join(map(re.escape, prefixes)),
                            lambda match: str(root) + match.group(), script)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                       SOURCE_DATE_EPOCH='946684800')
            result = subprocess.run(['/bin/sh', '-c', script], env=env, capture_output=True)
            contents = {directory: (root / directory.lstrip('/') / '.uuid').read_bytes()
                        for directory in FONT_DIRS if (root / directory.lstrip('/') / '.uuid').exists()}
            files = {name: (root / name.lstrip('/')).exists() for name in (*LOGS, AUX, '/var/log/keep.log', '/etc/ld.so.cache')}
            return result.returncode, contents, files, sorted(p.name for p in cache.iterdir())

    def test_install_normalizes_uuid_caches_and_only_approved_metadata(self):
        code, contents, files, caches = self.run_install()
        self.assertEqual(code, 0)
        self.assertEqual([contents[d] for d in FONT_DIRS], [u.encode() for u in UUIDS])
        self.assertEqual(caches, sorted(['keep-other.cache'] + [u + '-le64.cache-7' for u in UUIDS]))
        self.assertTrue(all(not files[name] for name in (*LOGS, AUX)))
        self.assertTrue(files['/var/log/keep.log'])
        self.assertTrue(files['/etc/ld.so.cache'])

    def test_missing_or_symlink_uuid_fails_closed(self):
        for option in ('missing_uuid', 'symlink_uuid'):
            with self.subTest(option=option):
                self.assertNotEqual(self.run_install(**{option: True})[0], 0)

    def test_missing_or_empty_rebuilt_cache_fails_closed(self):
        for option in ('missing_cache', 'empty_cache'):
            with self.subTest(option=option):
                self.assertNotEqual(self.run_install(**{option: True})[0], 0)

    def test_install_failure_stops_processing(self):
        self.assertNotEqual(self.run_install(install_fails=True)[0], 0)

    def test_invalid_cache_fails_closed(self):
        self.assertNotEqual(self.run_install(invalid_cache=True)[0], 0)

    def test_directory_timestamps_are_set_before_cache_build(self):
        script = runtime_runs()[0]
        self.assertIn('touch -d "@$SOURCE_DATE_EPOCH"', script)
        self.assertLess(script.index('touch -d'), script.index('fc-cache -fs'))
        self.assertLess(script.index('fc-cache -fs'), script.index('fc-cat'))

    def test_final_ldconfig_removes_only_aux_cache_in_same_run(self):
        self.assertEqual(runtime_runs()[1], 'ldconfig && rm -f /var/cache/ldconfig/aux-cache')

    def test_source_date_epoch_kept_in_both_stages(self):
        text = DOCKERFILE.read_text()
        self.assertEqual(text.count('SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}'), 2)


if __name__ == '__main__':
    unittest.main()
