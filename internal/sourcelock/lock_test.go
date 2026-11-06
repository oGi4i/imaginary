package sourcelock

import (
	"fmt"
	"strings"
	"testing"
)

// Values other than the reference versions and hashes are synthetic test data.
const completeLock = `{
  "schema_version": 1,
  "source": {"upstream_commit": "9357624e976790905f1b1bf4d059f8643c21e6fb", "bimg_version": "1.1.4"},
  "libvips": {"version": "8.10.0", "archive_sha256": "1299d3a98213ae477f0180817ec79afd36803a5b8434f48068e75a721d23b6f9"},
  "platform": "linux/amd64",
  "builder_image": "example.test/builder@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "runtime_image": "example.test/runtime@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "snapshot": {"url": "https://example.test/snapshot/", "signing_key_fingerprint": "0123456789abcdef0123456789abcdef01234567"},
  "build_packages": [{"name": "build-a", "version": "1", "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "depends_on": ["build-b"]}, {"name": "build-b", "version": "1", "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "depends_on": []}],
  "runtime_packages": [{"name": "runtime-a", "version": "1", "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "depends_on": []}],
  "go_toolchain": "1.20.14",
  "build_tools": [{"name": "builder", "version": "1", "sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}],
  "features": ["jpeg", "png", "webp"]
}`

func replaceOnce(t *testing.T, in, old, replacement string) string {
	t.Helper()
	if strings.Count(in, old) != 1 {
		t.Fatalf("fixture replacement %q is not unique", old)
	}
	return strings.Replace(in, old, replacement, 1)
}

func TestValidateCompleteAndUnresolved(t *testing.T) {
	missing, err := Validate(strings.NewReader(completeLock))
	if err != nil || len(missing) != 0 {
		t.Fatalf("complete synthetic structure: missing=%v err=%v", missing, err)
	}

	incomplete := completeLock
	for _, tc := range [][2]string{
		{`"platform": "linux/amd64"`, `"platform": null`},
		{`"builder_image": "example.test/builder@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"`, `"builder_image": null`},
		{`"runtime_image": "example.test/runtime@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"`, `"runtime_image": null`},
		{`"snapshot": {"url": "https://example.test/snapshot/", "signing_key_fingerprint": "0123456789abcdef0123456789abcdef01234567"}`, `"snapshot": null`},
		{`"build_packages": [{"name": "build-a", "version": "1", "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "depends_on": ["build-b"]}, {"name": "build-b", "version": "1", "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd", "depends_on": []}]`, `"build_packages": null`},
		{`"runtime_packages": [{"name": "runtime-a", "version": "1", "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "depends_on": []}]`, `"runtime_packages": null`},
		{`"go_toolchain": "1.20.14"`, `"go_toolchain": null`},
		{`"build_tools": [{"name": "builder", "version": "1", "sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}]`, `"build_tools": null`},
		{`"features": ["jpeg", "png", "webp"]`, `"features": null`},
	} {
		incomplete = replaceOnce(t, incomplete, tc[0], tc[1])
	}
	missing, err = Validate(strings.NewReader(incomplete))
	if err != nil {
		t.Fatalf("explicit unresolved locks: %v", err)
	}
	got := strings.Join(missing, "\n")
	for _, name := range []string{"platform", "builder_image", "runtime_image", "snapshot", "build_packages", "runtime_packages", "go_toolchain", "build_tools", "features"} {
		if !strings.Contains(got, name) {
			t.Errorf("missing report %q: %v", name, missing)
		}
	}
}

func TestValidateRejectsInvalidLocks(t *testing.T) {
	cases := []struct {
		name string
		old  string
		new  string
	}{
		{"unknown root", `"schema_version": 1`, `"unknown": 1, "schema_version": 1`},
		{"unknown nested", `"bimg_version": "1.1.4"`, `"unexpected": true, "bimg_version": "1.1.4"`},
		{"unknown array object", `"name": "build-a"`, `"extra": 1, "name": "build-a"`},
		{"duplicate root", `"schema_version": 1`, `"schema_version": 1, "schema_version": 1`},
		{"duplicate nested key", `"bimg_version": "1.1.4"`, `"bimg_version": "1.1.4", "bimg_version": "1.1.4"`},
		{"duplicate package key", `"name": "build-a"`, `"name": "build-a", "name": "build-a"`},
		{"missing required key", `"platform": "linux/amd64",`, ``},
		{"null source", `"source": {"upstream_commit": "9357624e976790905f1b1bf4d059f8643c21e6fb", "bimg_version": "1.1.4"}`, `"source": null`},
		{"wrong type", `"schema_version": 1`, `"schema_version": "1"`},
		{"unknown schema", `"schema_version": 1`, `"schema_version": 2`},
		{"bad commit", `9357624e976790905f1b1bf4d059f8643c21e6fb`, `not-a-commit`},
		{"different baseline", `9357624e976790905f1b1bf4d059f8643c21e6fb`, `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`},
		{"different bimg", `"bimg_version": "1.1.4"`, `"bimg_version": "1.1.5"`},
		{"different libvips", `"version": "8.10.0"`, `"version": "8.11.0"`},
		{"different archive", `1299d3a98213ae477f0180817ec79afd36803a5b8434f48068e75a721d23b6f9`, `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`},
		{"bad package hash", `cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc`, `not-a-sha256`},
		{"floating image", `example.test/builder@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`, `example.test/builder:latest`},
		{"bad platform", `linux/amd64`, `linux/`},
		{"empty reference", `"depends_on": ["build-b"]`, `"depends_on": [""]`},
		{"dangling reference", `"depends_on": ["build-b"]`, `"depends_on": ["absent"]`},
		{"absent dependency field", `, "depends_on": ["build-b"]`, ``},
		{"null dependency field", `"depends_on": ["build-b"]`, `"depends_on": null`},
		{"empty closure", `"runtime_packages": [{"name": "runtime-a", "version": "1", "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", "depends_on": []}]`, `"runtime_packages": []`},
		{"duplicate package identity", `"name": "build-b"`, `"name": "build-a"`},
		{"empty features", `"features": ["jpeg", "png", "webp"]`, `"features": []`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			input := replaceOnce(t, completeLock, tc.old, tc.new)
			if missing, err := Validate(strings.NewReader(input)); err == nil {
				t.Fatalf("accepted invalid lock, unresolved=%v", missing)
			}
		})
	}
	for _, input := range []string{"", "null", "{} {}", completeLock + ` {}`, completeLock + ` garbage`, `{"schema_version":`} {
		t.Run(fmt.Sprintf("invalid input %.30q", input), func(t *testing.T) {
			if _, err := Validate(strings.NewReader(input)); err == nil {
				t.Fatal("accepted invalid input")
			}
		})
	}
}
