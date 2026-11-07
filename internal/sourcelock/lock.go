// Package sourcelock validates the structure of the candidate image input lock.
// It does not authenticate inputs or determine whether a build is authorized.
package sourcelock

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

const maxLockBytes = 1 << 20

var (
	sha256Pattern         = regexp.MustCompile(`^[0-9a-f]{64}$`)
	keyPattern            = regexp.MustCompile(`^([0-9a-f]{40}|[0-9a-f]{64})$`)
	namePattern           = regexp.MustCompile(`^[a-z0-9][a-z0-9+._-]*$`)
	versionPattern        = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.+:~_-]*$`)
	platformPattern       = regexp.MustCompile(`^[a-z0-9]+/[a-z0-9]+(/[a-z0-9]+)?$`)
	imageComponentPattern = regexp.MustCompile(`^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$`)
	registryPattern       = regexp.MustCompile(`^[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?$`)
)

// Validate returns unresolved input names, or an error for an invalid lock.
// An empty unresolved list is not evidence of authenticity or build approval.
func Validate(r io.Reader) ([]string, error) {
	data, err := io.ReadAll(io.LimitReader(r, maxLockBytes+1))
	if err != nil {
		return nil, fmt.Errorf("cannot read lock")
	}
	if len(data) > maxLockBytes {
		return nil, fmt.Errorf("lock exceeds %d bytes", maxLockBytes)
	}
	if !utf8.Valid(data) {
		return nil, fmt.Errorf("lock is not UTF-8")
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	value, err := readValue(d, 0)
	if err != nil {
		return nil, err
	}
	if _, err := d.Token(); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("trailing JSON value")
		}
		return nil, fmt.Errorf("invalid trailing data")
	}
	if root, ok := value.(map[string]interface{}); ok && root["schema_version"] == json.Number("2") {
		return validateV2(value)
	}
	root, err := object(value, "lock", "schema_version", "source", "libvips", "platform", "builder_image", "runtime_image", "snapshot", "build_packages", "runtime_packages", "go_toolchain", "build_tools", "features")
	if err != nil {
		return nil, err
	}
	if root["schema_version"] != json.Number("1") {
		return nil, fmt.Errorf("invalid schema_version")
	}
	source, err := object(root["source"], "source", "upstream_commit", "bimg_version")
	if err != nil {
		return nil, err
	}
	if source["upstream_commit"] != "9357624e976790905f1b1bf4d059f8643c21e6fb" || source["bimg_version"] != "1.1.4" {
		return nil, fmt.Errorf("source identity does not match baseline")
	}
	vips, err := object(root["libvips"], "libvips", "version", "archive_sha256")
	if err != nil {
		return nil, err
	}
	if vips["version"] != "8.10.0" || vips["archive_sha256"] != "1299d3a98213ae477f0180817ec79afd36803a5b8434f48068e75a721d23b6f9" {
		return nil, fmt.Errorf("libvips identity does not match baseline")
	}
	var missing []string
	for _, field := range []string{"platform", "builder_image", "runtime_image", "snapshot", "build_packages", "runtime_packages", "go_toolchain", "build_tools", "features"} {
		v := root[field]
		if v == nil {
			missing = append(missing, field)
			continue
		}
		switch field {
		case "platform":
			err = match(v, field, platformPattern)
		case "builder_image", "runtime_image":
			err = validateImage(v, field)
		case "go_toolchain":
			err = match(v, field, versionPattern)
		case "snapshot":
			err = validateSnapshot(v)
		case "build_packages", "runtime_packages":
			err = validatePackages(v, field)
		case "build_tools":
			err = validateTools(v)
		case "features":
			err = validateNames(v, field)
		}
		if err != nil {
			return nil, err
		}
	}
	return missing, nil
}

// readValue rejects duplicate keys before a map can hide them.
func readValue(d *json.Decoder, depth int) (interface{}, error) {
	if depth > 32 {
		return nil, fmt.Errorf("JSON nesting too deep")
	}
	t, err := d.Token()
	if err != nil {
		return nil, fmt.Errorf("invalid JSON")
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return t, nil
	}
	switch delim {
	case '{':
		m := make(map[string]interface{})
		for d.More() {
			key, err := d.Token()
			if err != nil {
				return nil, fmt.Errorf("invalid JSON object key")
			}
			k, ok := key.(string)
			if !ok {
				return nil, fmt.Errorf("invalid object key")
			}
			if _, exists := m[k]; exists {
				return nil, fmt.Errorf("duplicate object key")
			}
			v, err := readValue(d, depth+1)
			if err != nil {
				return nil, err
			}
			m[k] = v
		}
		end, err := d.Token()
		if err != nil || end != json.Delim('}') {
			return nil, fmt.Errorf("invalid object")
		}
		return m, nil
	case '[':
		a := make([]interface{}, 0)
		for d.More() {
			v, err := readValue(d, depth+1)
			if err != nil {
				return nil, err
			}
			a = append(a, v)
		}
		end, err := d.Token()
		if err != nil || end != json.Delim(']') {
			return nil, fmt.Errorf("invalid array")
		}
		return a, nil
	default:
		return nil, fmt.Errorf("unexpected delimiter")
	}
}

func object(v interface{}, path string, fields ...string) (map[string]interface{}, error) {
	m, ok := v.(map[string]interface{})
	if !ok {
		return nil, fmt.Errorf("%s must be an object", path)
	}
	allowed := make(map[string]bool, len(fields))
	for _, field := range fields {
		allowed[field] = true
		if _, ok := m[field]; !ok {
			return nil, fmt.Errorf("%s.%s is missing", path, field)
		}
	}
	for k := range m {
		if !allowed[k] {
			return nil, fmt.Errorf("unknown key in %s", path)
		}
	}
	return m, nil
}

func match(v interface{}, path string, pattern *regexp.Regexp) error {
	s, ok := v.(string)
	if !ok || !pattern.MatchString(s) {
		return fmt.Errorf("invalid %s", path)
	}
	return nil
}

// validateImage accepts a conservative OCI repository name pinned by sha256.
func validateImage(v interface{}, path string) error {
	s, ok := v.(string)
	if !ok || len(s) > 512 {
		return fmt.Errorf("invalid %s", path)
	}
	parts := strings.Split(s, "@sha256:")
	if len(parts) != 2 || len(parts[0]) > 255 || !sha256Pattern.MatchString(parts[1]) {
		return fmt.Errorf("invalid %s", path)
	}
	components := strings.Split(parts[0], "/")
	if len(components) < 2 || !registryPattern.MatchString(components[0]) {
		return fmt.Errorf("invalid %s", path)
	}
	if i := strings.LastIndexByte(components[0], ':'); i >= 0 {
		port, err := strconv.Atoi(components[0][i+1:])
		if err != nil || port < 1 || port > 65535 {
			return fmt.Errorf("invalid %s", path)
		}
	}
	for _, component := range components[1:] {
		if !imageComponentPattern.MatchString(component) {
			return fmt.Errorf("invalid %s", path)
		}
	}
	return nil
}

func validateV2Image(v interface{}, path string) error {
	if err := validateImage(v, path); err != nil {
		return err
	}
	registry := strings.SplitN(v.(string), "/", 2)[0]
	if registry != "localhost" && !strings.ContainsAny(registry, ".:") {
		return fmt.Errorf("invalid %s", path)
	}
	return nil
}

func validateSnapshot(v interface{}) error {
	m, err := object(v, "snapshot", "url", "signing_key_fingerprint")
	if err != nil {
		return err
	}
	s, ok := m["url"].(string)
	if !ok {
		return fmt.Errorf("invalid snapshot.url")
	}
	u, err := url.Parse(s)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" {
		return fmt.Errorf("invalid snapshot.url")
	}
	if strings.HasSuffix(u.Host, ":") {
		return fmt.Errorf("invalid snapshot.url")
	}
	if p := u.Port(); p != "" {
		port, err := strconv.Atoi(p)
		if err != nil || port < 1 || port > 65535 {
			return fmt.Errorf("invalid snapshot.url")
		}
	}
	return match(m["signing_key_fingerprint"], "snapshot.signing_key_fingerprint", keyPattern)
}

func array(v interface{}, path string) ([]interface{}, error) {
	a, ok := v.([]interface{})
	if !ok || len(a) == 0 {
		return nil, fmt.Errorf("%s must be a nonempty array", path)
	}
	return a, nil
}

func validatePackages(v interface{}, path string) error {
	a, err := array(v, path)
	if err != nil {
		return err
	}
	names := map[string]bool{}
	var refs []string
	for _, item := range a {
		m, err := object(item, path, "name", "version", "sha256", "depends_on")
		if err != nil {
			return err
		}
		if err = match(m["name"], path+".name", namePattern); err != nil {
			return err
		}
		name := m["name"].(string)
		if names[name] {
			return fmt.Errorf("duplicate %s package %s", path, name)
		}
		names[name] = true
		if err = match(m["version"], path+".version", versionPattern); err != nil {
			return err
		}
		if err = match(m["sha256"], path+".sha256", sha256Pattern); err != nil {
			return err
		}
		deps, ok := m["depends_on"].([]interface{})
		if !ok {
			return fmt.Errorf("%s.depends_on must be an array", path)
		}
		for _, dep := range deps {
			if err = match(dep, path+".depends_on", namePattern); err != nil {
				return err
			}
			refs = append(refs, dep.(string))
		}
	}
	for _, ref := range refs {
		if !names[ref] {
			return fmt.Errorf("%s dependency %q is absent", path, ref)
		}
	}
	return nil
}

func validateTools(v interface{}) error {
	a, err := array(v, "build_tools")
	if err != nil {
		return err
	}
	names := map[string]bool{}
	for _, item := range a {
		m, err := object(item, "build_tools", "name", "version", "sha256")
		if err != nil {
			return err
		}
		if err = match(m["name"], "build_tools.name", namePattern); err != nil {
			return err
		}
		name := m["name"].(string)
		if names[name] {
			return fmt.Errorf("duplicate build tool %s", name)
		}
		names[name] = true
		if err = match(m["version"], "build_tools.version", versionPattern); err != nil {
			return err
		}
		if err = match(m["sha256"], "build_tools.sha256", sha256Pattern); err != nil {
			return err
		}
	}
	return nil
}

// validateV2 describes inputs; it does not authenticate them or approve a build.
func validateV2(v interface{}) ([]string, error) {
	root, err := object(v, "lock", "schema_version", "source", "libvips", "platform", "builder_image", "runtime_image", "snapshot", "build_packages", "runtime_packages", "build_roots", "runtime_roots", "go_toolchain", "build_tools", "build", "features")
	if err != nil {
		return nil, err
	}
	source, err := object(root["source"], "source", "upstream_commit", "bimg_version")
	if err != nil {
		return nil, err
	}
	if source["upstream_commit"] != "9357624e976790905f1b1bf4d059f8643c21e6fb" || source["bimg_version"] != "1.1.4" {
		return nil, fmt.Errorf("source identity does not match baseline")
	}
	vips, err := object(root["libvips"], "libvips", "version", "archive_url", "archive_sha256")
	if err != nil {
		return nil, err
	}
	if vips["version"] != "8.10.0" || vips["archive_sha256"] != "1299d3a98213ae477f0180817ec79afd36803a5b8434f48068e75a721d23b6f9" {
		return nil, fmt.Errorf("libvips identity does not match baseline")
	}
	if vips["archive_url"] != "https://github.com/libvips/libvips/releases/download/v8.10.0/vips-8.10.0.tar.gz" {
		return nil, fmt.Errorf("invalid libvips.archive_url")
	}
	var missing []string
	for _, field := range []string{"platform", "builder_image", "runtime_image", "snapshot", "build_packages", "runtime_packages", "build_roots", "runtime_roots", "go_toolchain", "build_tools", "build", "features"} {
		x := root[field]
		if x == nil {
			missing = append(missing, field)
			continue
		}
		switch field {
		case "platform":
			err = match(x, field, platformPattern)
		case "builder_image", "runtime_image":
			err = validateV2Image(x, field)
		case "snapshot":
			err = validateV2Snapshot(x, &missing)
		case "build_packages", "runtime_packages":
			err = validateV2Packages(x, field)
		case "build_roots", "runtime_roots", "features":
			err = validateNames(x, field)
		case "go_toolchain":
			var m map[string]interface{}
			m, err = object(x, field, "version", "url", "size", "sha256")
			if err == nil {
				err = match(m["version"], field+".version", versionPattern)
			}
			if err == nil {
				err = validateArtifactFields(m, field)
			}
		case "build_tools":
			err = validateV2Tools(x)
		case "build":
			err = validateV2Build(x)
		}
		if err != nil {
			return nil, err
		}
	}
	for _, pair := range [][2]string{{"build_roots", "build_packages"}, {"runtime_roots", "runtime_packages"}} {
		if root[pair[0]] == nil || root[pair[1]] == nil {
			continue
		}
		names := map[string]bool{}
		for _, item := range root[pair[1]].([]interface{}) {
			names[item.(map[string]interface{})["name"].(string)] = true
		}
		for _, item := range root[pair[0]].([]interface{}) {
			if !names[item.(string)] {
				return nil, fmt.Errorf("%s reference is absent", pair[0])
			}
		}
	}
	return missing, nil
}

func validateURL(v interface{}, path string) error {
	s, ok := v.(string)
	if !ok || len(s) > 2048 {
		return fmt.Errorf("invalid %s", path)
	}
	u, err := url.Parse(s)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || strings.HasSuffix(u.Host, ":") {
		return fmt.Errorf("invalid %s", path)
	}
	if p := u.Port(); p != "" {
		n, e := strconv.Atoi(p)
		if e != nil || n < 1 || n > 65535 {
			return fmt.Errorf("invalid %s", path)
		}
	}
	return nil
}

func positiveInteger(v interface{}, path string) error {
	n, ok := v.(json.Number)
	if !ok {
		return fmt.Errorf("invalid %s", path)
	}
	i, err := n.Int64()
	if err != nil || i <= 0 {
		return fmt.Errorf("invalid %s", path)
	}
	return nil
}

func validateArtifactFields(m map[string]interface{}, path string) error {
	if err := validateURL(m["url"], path+".url"); err != nil {
		return err
	}
	if err := positiveInteger(m["size"], path+".size"); err != nil {
		return err
	}
	return match(m["sha256"], path+".sha256", sha256Pattern)
}

func validateArtifact(v interface{}, path string) error {
	m, err := object(v, path, "url", "size", "sha256")
	if err != nil {
		return err
	}
	return validateArtifactFields(m, path)
}

func validateV2Snapshot(v interface{}, missing *[]string) error {
	m, err := object(v, "snapshot", "date", "main", "security")
	if err != nil {
		return err
	}
	date, ok := m["date"].(string)
	if !ok {
		return fmt.Errorf("invalid snapshot.date")
	}
	parsed, err := time.Parse("20060102T150405Z", date)
	if err != nil || parsed.Format("20060102T150405Z") != date {
		return fmt.Errorf("invalid snapshot.date")
	}
	for _, name := range []string{"main", "security"} {
		path := "snapshot." + name
		if m[name] == nil {
			*missing = append(*missing, path)
			continue
		}
		a, e := object(m[name], path, "inrelease", "packages", "keyring", "signing_key_fingerprint", "release_identity")
		if e != nil {
			return e
		}
		for _, field := range []string{"inrelease", "packages", "keyring", "signing_key_fingerprint", "release_identity"} {
			p := path + "." + field
			if a[field] == nil {
				*missing = append(*missing, p)
				continue
			}
			switch field {
			case "keyring":
				var keyring map[string]interface{}
				keyring, e = object(a[field], p, "image", "path", "size", "sha256")
				if e == nil {
					e = validateV2Image(keyring["image"], p+".image")
				}
				if e == nil && keyring["path"] != "/usr/share/keyrings/debian-archive-keyring.gpg" {
					e = fmt.Errorf("invalid %s.path", p)
				}
				if e == nil {
					if keyring["size"] == nil {
						*missing = append(*missing, p+".size")
					} else {
						e = positiveInteger(keyring["size"], p+".size")
					}
				}
				if e == nil {
					e = match(keyring["sha256"], p+".sha256", sha256Pattern)
				}
			case "inrelease", "packages":
				var artifact map[string]interface{}
				artifact, e = object(a[field], p, "url", "size", "sha256")
				if e == nil {
					for _, k := range []string{"url", "size", "sha256"} {
						q := p + "." + k
						if artifact[k] == nil {
							*missing = append(*missing, q)
							continue
						}
						switch k {
						case "url":
							e = validateURL(artifact[k], q)
						case "size":
							e = positiveInteger(artifact[k], q)
						case "sha256":
							e = match(artifact[k], q, sha256Pattern)
						}
						if e != nil {
							break
						}
					}
				}
			case "signing_key_fingerprint":
				e = match(a[field], p, keyPattern)
			case "release_identity":
				var identity map[string]interface{}
				identity, e = object(a[field], p, "origin", "label", "codename", "suite", "date", "valid_until")
				if e == nil {
					for _, k := range []string{"origin", "label", "codename", "suite", "date", "valid_until"} {
						if k == "valid_until" && identity[k] == nil {
							continue
						}
						if e = safeString(identity[k], p+"."+k); e != nil {
							break
						}
					}
				}
			}
			if e != nil {
				return e
			}
		}
	}
	return nil
}

func validateV2Packages(v interface{}, path string) error {
	a, err := array(v, path)
	if err != nil {
		return err
	}
	names := map[string]bool{}
	var refs []string
	for _, item := range a {
		m, e := object(item, path, "name", "version", "architecture", "repository", "url", "size", "sha256", "dependencies", "provenance")
		if e != nil {
			return e
		}
		if e = match(m["name"], path+".name", namePattern); e != nil {
			return e
		}
		name := m["name"].(string)
		if names[name] {
			return fmt.Errorf("duplicate %s package", path)
		}
		names[name] = true
		if e = match(m["version"], path+".version", versionPattern); e != nil {
			return e
		}
		if m["architecture"] != "amd64" && m["architecture"] != "all" {
			return fmt.Errorf("invalid %s.architecture", path)
		}
		if m["repository"] != "main" && m["repository"] != "security" {
			return fmt.Errorf("invalid %s.repository", path)
		}
		if m["provenance"] != "base" && m["provenance"] != "snapshot" {
			return fmt.Errorf("invalid %s.provenance", path)
		}
		if e = validateArtifactFields(m, path); e != nil {
			return e
		}
		deps, ok := m["dependencies"].([]interface{})
		if !ok {
			return fmt.Errorf("invalid %s.dependencies", path)
		}
		seen := map[string]bool{}
		for _, d := range deps {
			if e = match(d, path+".dependencies", namePattern); e != nil {
				return e
			}
			s := d.(string)
			if seen[s] {
				return fmt.Errorf("duplicate dependency in %s", path)
			}
			seen[s] = true
			refs = append(refs, s)
		}
	}
	for _, r := range refs {
		if !names[r] {
			return fmt.Errorf("%s dependency is absent", path)
		}
	}
	return nil
}

func safeString(v interface{}, path string) error {
	s, ok := v.(string)
	if !ok || len(s) == 0 || len(s) > 2048 || strings.ContainsAny(s, "\r\n\x00") {
		return fmt.Errorf("invalid %s", path)
	}
	return nil
}

func stringArray(v interface{}, path string) error {
	a, ok := v.([]interface{})
	if !ok {
		return fmt.Errorf("invalid %s", path)
	}
	for _, x := range a {
		if err := safeString(x, path); err != nil {
			return err
		}
	}
	return nil
}

func validateV2Tools(v interface{}) error {
	m, err := object(v, "build_tools", "buildkit_image", "frontend_image", "buildx", "actions")
	if err != nil {
		return err
	}
	for _, k := range []string{"buildkit_image", "frontend_image"} {
		if err = validateV2Image(m[k], "build_tools."+k); err != nil {
			return err
		}
	}
	if err = validateArtifact(m["buildx"], "build_tools.buildx"); err != nil {
		return err
	}
	actions, err := array(m["actions"], "build_tools.actions")
	if err != nil {
		return err
	}
	names := map[string]bool{}
	for _, item := range actions {
		a, e := object(item, "build_tools.actions", "name", "repository", "commit")
		if e != nil {
			return e
		}
		if e = match(a["name"], "build_tools.actions.name", namePattern); e != nil {
			return e
		}
		name := a["name"].(string)
		if names[name] {
			return fmt.Errorf("duplicate action")
		}
		names[name] = true
		if e = validateURL(a["repository"], "build_tools.actions.repository"); e != nil {
			return e
		}
		if e = match(a["commit"], "build_tools.actions.commit", regexp.MustCompile(`^[0-9a-f]{40}$`)); e != nil {
			return e
		}
	}
	return nil
}

func validateV2Build(v interface{}) error {
	m, err := object(v, "build", "go_mod_sha256", "go_sum_sha256", "source_date_epoch", "configure", "cflags", "cxxflags", "go_flags", "module_mode")
	if err != nil {
		return err
	}
	for _, k := range []string{"go_mod_sha256", "go_sum_sha256"} {
		if err = match(m[k], "build."+k, sha256Pattern); err != nil {
			return err
		}
	}
	if err = positiveInteger(m["source_date_epoch"], "build.source_date_epoch"); err != nil {
		return err
	}
	for _, k := range []string{"configure", "cflags", "cxxflags", "go_flags"} {
		if err = stringArray(m[k], "build."+k); err != nil {
			return err
		}
	}
	if m["module_mode"] != "readonly" {
		return fmt.Errorf("invalid build.module_mode")
	}
	return nil
}

func validateNames(v interface{}, path string) error {
	a, err := array(v, path)
	if err != nil {
		return err
	}
	seen := map[string]bool{}
	for _, item := range a {
		if err := match(item, path, namePattern); err != nil {
			return err
		}
		s := item.(string)
		if seen[s] {
			return fmt.Errorf("duplicate %s entry %s", path, s)
		}
		seen[s] = true
	}
	return nil
}
