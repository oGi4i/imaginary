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
	"unicode/utf8"
)

const maxLockBytes = 1 << 20

var (
	sha256Pattern   = regexp.MustCompile(`^[0-9a-f]{64}$`)
	keyPattern      = regexp.MustCompile(`^([0-9a-f]{40}|[0-9a-f]{64})$`)
	namePattern     = regexp.MustCompile(`^[a-z0-9][a-z0-9+._-]*$`)
	versionPattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.+:~_-]*$`)
	platformPattern = regexp.MustCompile(`^[a-z0-9]+/[a-z0-9]+(/[a-z0-9]+)?$`)
	imagePattern    = regexp.MustCompile(`^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$`)
)

// Validate returns unresolved input names, or an error for an invalid lock.
// An empty unresolved list is not evidence of authenticity or build approval.
func Validate(r io.Reader) ([]string, error) {
	data, err := io.ReadAll(io.LimitReader(r, maxLockBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read lock: %w", err)
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
		return nil, fmt.Errorf("trailing data: %w", err)
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
			err = match(v, field, imagePattern)
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
		return nil, fmt.Errorf("invalid JSON: %w", err)
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
				return nil, err
			}
			k, ok := key.(string)
			if !ok {
				return nil, fmt.Errorf("invalid object key")
			}
			if _, exists := m[k]; exists {
				return nil, fmt.Errorf("duplicate key %q", k)
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
			return nil, fmt.Errorf("unknown key %s.%s", path, k)
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
	if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.Fragment != "" {
		return fmt.Errorf("invalid snapshot.url")
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
