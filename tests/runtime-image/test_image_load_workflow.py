"""Offline contract for runtime checks against the exported OCI image."""
from pathlib import Path
import subprocess
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

    def test_codec_status_diagnostic_is_bounded(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        marker = '          case "$status" in\n'
        self.assertIn(marker, workflow[:workflow.index('"$RUNNER_TEMP/runtime-response-check" "$status"')])
        start = workflow.index(marker)
        end = workflow.index('          esac', start) + len('          esac')
        diagnostic = workflow[start:end]
        for status, expected, ok in [('200', '', True), ('400', 'codec probe failed input=jpeg http_status=400\n', False),
                                     ('500', 'codec probe failed input=jpeg http_status=500\n', False),
                                     ('000', 'codec probe failed input=jpeg reason=invalid_status\n', False),
                                     ('500\nprivate/path', 'codec probe failed input=jpeg reason=invalid_status\n', False)]:
            result = subprocess.run(['bash', '-c', diagnostic], text=True, capture_output=True,
                                    env={'status': status, 'kind': 'jpeg'})
            with self.subTest(status=status):
                self.assertEqual(result.returncode == 0, ok)
                self.assertEqual(result.stderr, expected)
                self.assertEqual(result.stdout, '')

    def test_response_checker_build_is_standard_library_only(self):
        workflow = (ROOT / '.github/workflows/runtime-image.yml').read_text()
        self.assertIn('GO111MODULE=off CGO_ENABLED=0', workflow)
        self.assertIn('"$GOROOT/bin/go" build -trimpath -o "$RUNNER_TEMP/runtime-response-check" ./cmd/runtime-response-check/main.go', workflow)
        self.assertNotIn('"$GOROOT/bin/go" build -o "$RUNNER_TEMP/runtime-response-check" ./cmd/runtime-response-check\n', workflow)
        source = (ROOT / 'cmd/runtime-response-check/main.go').read_text()
        self.assertNotIn('github.com/', source)


if __name__ == '__main__':
    unittest.main()
