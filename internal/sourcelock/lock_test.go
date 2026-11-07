package sourcelock

import (
	"fmt"
	"os"
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

// V2 values are synthetic and do not constitute authenticated evidence.
const completeV2 = `{
  "schema_version": 2,
  "source": {"upstream_commit":"9357624e976790905f1b1bf4d059f8643c21e6fb","bimg_version":"1.1.4"},
  "libvips": {"version":"8.10.0","archive_url":"https://github.com/libvips/libvips/releases/download/v8.10.0/vips-8.10.0.tar.gz","archive_sha256":"1299d3a98213ae477f0180817ec79afd36803a5b8434f48068e75a721d23b6f9"},
  "platform":"linux/amd64",
  "builder_image":"example.test/base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "runtime_image":"example.test/base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "snapshot":{"date":"20240408T000000Z","main":{"inrelease":{"url":"https://example.test/release","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"packages":{"url":"https://example.test/packages","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"keyring":{"image":"example.test/base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","path":"/usr/share/keyrings/debian-archive-keyring.gpg","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"signing_key_fingerprint":"0123456789abcdef0123456789abcdef01234567","release_identity":{"origin":"Debian","label":"Debian","codename":"buster","suite":"oldoldstable","date":"Sat, 10 Jun 2023 08:53:33 UTC","valid_until":null}},"security":{"inrelease":{"url":"https://example.test/security","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"packages":{"url":"https://example.test/security-packages","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"keyring":{"image":"example.test/base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","path":"/usr/share/keyrings/debian-archive-keyring.gpg","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"signing_key_fingerprint":"0123456789abcdef0123456789abcdef01234567","release_identity":{"origin":"Debian","label":"Debian-Security","codename":"buster","suite":"oldoldstable","date":"Sun, 07 Apr 2024 12:12:17 UTC","valid_until":"Sun, 14 Apr 2024 12:12:17 UTC"}}},
  "build_packages":[{"name":"a","version":"1","architecture":"amd64","repository":"main","url":"https://example.test/a.deb","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","dependencies":[],"provenance":"base"}],
  "runtime_packages":[{"name":"a","version":"1","architecture":"amd64","repository":"main","url":"https://example.test/a.deb","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","dependencies":[],"provenance":"snapshot"}],
  "build_roots":["a"],"runtime_roots":["a"],
  "go_toolchain":{"version":"1.20.14","url":"https://example.test/go.tar.gz","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "build_tools":{"buildkit_image":"example.test/buildkit@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","frontend_image":"example.test/frontend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","buildx":{"url":"https://example.test/buildx","size":1,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},"actions":[{"name":"checkout","repository":"https://example.test/checkout","commit":"0123456789abcdef0123456789abcdef01234567"}]},
  "build":{"go_mod_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","go_sum_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_date_epoch":1,"configure":["--disable-debug"],"cflags":[],"cxxflags":[],"go_flags":[],"module_mode":"readonly"},
  "features":["jpeg"]
}`

func TestCommittedV2LockIsIncomplete(t *testing.T) {
	f, err := os.Open("../../docs/runtime-image.lock.yaml")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	missing, err := Validate(f)
	if err != nil {
		t.Fatalf("committed lock is invalid: %v", err)
	}
	for _, name := range []string{"snapshot.main.keyring.size", "snapshot.security.keyring.size", "build_packages", "runtime_packages", "build_roots", "runtime_roots", "build_tools", "build", "features"} {
		found := false
		for _, got := range missing {
			if got == name {
				found = true
			}
		}
		if !found {
			t.Errorf("missing unresolved path %q", name)
		}
	}
}

func TestValidateV2(t *testing.T) {
	if missing, err := Validate(strings.NewReader(completeV2)); err != nil || len(missing) != 0 {
		t.Fatalf("complete v2: missing=%v err=%v", missing, err)
	}
	incomplete := replaceOnce(t, completeV2, `"features":["jpeg"]`, `"features":null`)
	if missing, err := Validate(strings.NewReader(incomplete)); err != nil || strings.Join(missing, ",") != "features" {
		t.Fatalf("incomplete v2: missing=%v err=%v", missing, err)
	}
	for _, tc := range [][2]string{
		{`https://github.com/libvips/libvips/releases/download/v8.10.0/vips-8.10.0.tar.gz`, `https://example.test/other.tar.gz`},
		{`20240408T000000Z`, `20249999T999999Z`},
		{`example.test/base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`, `debian/bookworm@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`},
		{`"size":1`, `"size":0`},
		{`"architecture":"amd64"`, `"architecture":"arm64"`},
		{`"dependencies":[]`, `"dependencies":["absent"]`},
		{`"build_roots":["a"]`, `"build_roots":["missing"]`},
		{`"module_mode":"readonly"`, `"module_mode":"mod"`},
		{`"source_date_epoch":1`, `"source_date_epoch":-1`},
		{`"signing_key_fingerprint"`, `"Signing_key_fingerprint"`},
		{`"buildkit_image"`, `"secret\nmarker":1,"buildkit_image"`},
	} {
		input := strings.Replace(completeV2, tc[0], tc[1], 1)
		if input == completeV2 {
			t.Fatalf("fixture not found: %s", tc[0])
		}
		if _, err := Validate(strings.NewReader(input)); err == nil || strings.Contains(err.Error(), "secret") || strings.Contains(err.Error(), "\n") {
			t.Fatalf("invalid v2 accepted or reflected: %v", err)
		}
	}
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

func TestValidateDoesNotReflectUnknownOrDuplicateKeys(t *testing.T) {
	const private = "private-marker"
	for _, input := range []string{
		replaceOnce(t, completeLock, `"schema_version": 1`, `"private-marker\nlog-injection": true, "schema_version": 1`),
		replaceOnce(t, completeLock, `"schema_version": 1`, `"private-marker\nlog-injection": true, "private-marker\nlog-injection": true, "schema_version": 1`),
	} {
		_, err := Validate(strings.NewReader(input))
		if err == nil {
			t.Fatal("accepted unknown or duplicate key")
		}
		if strings.Contains(err.Error(), private) || strings.Contains(err.Error(), "log-injection") || strings.Contains(err.Error(), "\n") {
			t.Fatalf("error reflects input: %q", err)
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
		{"case variant key", `"bimg_version": "1.1.4"`, `"BIMG_VERSION": "1.1.4"`},
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
		{"image empty component", `example.test/builder@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`, `example.test//builder@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`},
		{"snapshot without hostname", `https://example.test/snapshot/`, `https://:443/`},
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
