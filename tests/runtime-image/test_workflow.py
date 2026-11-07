from pathlib import Path
import unittest


WORKFLOW = Path(__file__).resolve().parents[2] / '.github/workflows/runtime-inputs.yml'


class WorkflowTest(unittest.TestCase):
    def test_closure_runs_after_verified_metadata_and_before_incomplete_gate(self):
        text = WORKFLOW.read_text()
        discovery, reference = text.split('\n  reference-inventory:', 1)
        self.assertIn('timeout-minutes: 60', discovery)
        self.assertLess(discovery.index('tooling/runtime-image/closure.py'), discovery.index('tooling/runtime-image/verify_closure.py'))
        self.assertLess(discovery.index('tooling/runtime-image/verify_closure.py'), discovery.index('Upload proposed lock and verification'))
        self.assertLess(discovery.index('Upload proposed lock and verification'), discovery.index('Keep incomplete discovery fail-closed'))
        self.assertIn('verified/candidate-lock.json', discovery)
        self.assertIn('verified/verification.json', discovery)
        self.assertIn('fetch-depth: 0', discovery)
        self.assertIn('python3 -I tooling/runtime-image/closure.py', discovery)
        self.assertIn('--discoveryJSON "$RUNNER_TEMP/runtime-inputs/discovery.json"', discovery)
        self.assertIn('--output "$RUNNER_TEMP/runtime-inputs/closure.json"', discovery)
        self.assertLess(discovery.index('Upload discovery evidence'), discovery.index('tooling/runtime-image/closure.py'))
        self.assertLess(discovery.index('tooling/runtime-image/closure.py'), discovery.index('Upload closure evidence'))
        self.assertLess(discovery.index('Upload closure evidence'), discovery.index('Keep incomplete discovery fail-closed'))
        artifact = discovery[discovery.index('Upload closure evidence'):discovery.index('Keep incomplete discovery fail-closed')]
        self.assertIn('if: always()', artifact)
        self.assertIn('${{ runner.temp }}/runtime-inputs/closure.json', artifact)
        self.assertIn('retention-days: 7', artifact)
        self.assertNotIn('closure.py', reference)


if __name__ == '__main__':
    unittest.main()
