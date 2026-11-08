"""Offline contract for runtime checks against the exported OCI image."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ImageLoadWorkflowTests(unittest.TestCase):
    def test_loads_exact_oci_before_runtime_probes(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        conversion = workflow.index('image_load.py convert')
        load = workflow.index('docker load -i "$RUNNER_TEMP/docker-load.tar"')
        binding = workflow.index('image_load.py validate-loaded')
        probe = workflow.index('docker run --name "$checks"')
        self.assertLess(conversion, load)
        self.assertLess(load, binding)
        self.assertLess(binding, probe)
        self.assertIn('--oci "$RUNNER_TEMP/evidence/artifact/image.oci.tar"', workflow)
        self.assertIn('--expected "$RUNNER_TEMP/evidence/artifact/oci-load-identity.json"', workflow)
        self.assertIn('--inspect "$RUNNER_TEMP/loaded-image.json"', workflow)
        self.assertNotIn('docker load -i "$RUNNER_TEMP/evidence/artifact/image.oci.tar"', workflow)
        self.assertIn('oci-load-identity.json', workflow)


if __name__ == '__main__':
    unittest.main()
