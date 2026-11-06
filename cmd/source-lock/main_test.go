package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const incompleteLock = `{
  "schema_version": 1,
  "source": {"upstream_commit": "9357624e976790905f1b1bf4d059f8643c21e6fb", "bimg_version": "1.1.4"},
  "libvips": {"version": "8.10.0", "archive_sha256": "1299d3a98213ae477f0180817ec79afd36803a5b8434f48068e75a721d23b6f9"},
  "platform": null,
  "builder_image": null,
  "runtime_image": null,
  "snapshot": null,
  "build_packages": null,
  "runtime_packages": null,
  "go_toolchain": null,
  "build_tools": null,
  "features": null
}`

func TestRunListsUnresolvedLocks(t *testing.T) {
	path := filepath.Join(t.TempDir(), "lock.yaml")
	if err := os.WriteFile(path, []byte(incompleteLock), 0600); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	if code := run([]string{path}, &stdout, &stderr); code == 0 {
		t.Fatalf("unresolved lock succeeded: stdout=%q stderr=%q", stdout.String(), stderr.String())
	}
	output := stdout.String() + stderr.String()
	for _, want := range []string{"platform", "builder_image", "runtime_image", "snapshot", "build_packages", "runtime_packages", "go_toolchain", "build_tools", "features"} {
		if !strings.Contains(output, want) {
			t.Errorf("missing unresolved input %q in output %q", want, output)
		}
	}
	for _, forbidden := range []string{"ready", "approved", "reproducible"} {
		if strings.Contains(strings.ToLower(output), forbidden) {
			t.Errorf("output claims %q: %q", forbidden, output)
		}
	}
}

func TestRunRejectsInvalidInput(t *testing.T) {
	invalid := filepath.Join(t.TempDir(), "invalid.yaml")
	if err := os.WriteFile(invalid, []byte(`{"schema_version": 1, "schema_version": 1}`), 0600); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{nil, {"missing-lock-file.yaml"}, {invalid}, {invalid, invalid}} {
		var stdout, stderr bytes.Buffer
		if code := run(args, &stdout, &stderr); code == 0 {
			t.Errorf("run(%q) succeeded", args)
		}
		if stderr.Len() == 0 {
			t.Errorf("run(%q) did not report an error", args)
		}
	}
}
