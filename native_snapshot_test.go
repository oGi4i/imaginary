package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func TestQualificationSnapshotReadOnly(t *testing.T) {
	h := newQualificationHandler()
	var first qualificationSnapshot
	for i := 0; i < 2; i++ {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/snapshot", nil))
		if w.Code != http.StatusOK || w.Body.Len() > 4096 {
			t.Fatalf("snapshot response %d (%d bytes)", w.Code, w.Body.Len())
		}
		var got qualificationSnapshot
		if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
			t.Fatal(err)
		}
		if got.SchemaVersion != 2 || got.ProcessLifetime == "" || got.Sequence == 0 {
			t.Fatalf("invalid identity: %+v", got)
		}
		if i == 1 && (first.ProcessLifetime != got.ProcessLifetime || got.Sequence <= first.Sequence) {
			t.Fatalf("identity or sequence changed incorrectly: %+v %+v", first, got)
		}
		first = got
	}
	for _, tc := range []struct{ method, path string }{{"POST", "/snapshot"}, {"GET", "/snapshot?secret=private"}, {"GET", "/other"}} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest(tc.method, tc.path, nil))
		if w.Code < 400 || !json.Valid(w.Body.Bytes()) || strings.Contains(w.Body.String(), "private") {
			t.Fatalf("unsafe error for %s %s: %d %s", tc.method, tc.path, w.Code, w.Body.String())
		}
	}
}

func TestQualificationConcurrentSnapshots(t *testing.T) {
	h := newQualificationHandler()
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			w := httptest.NewRecorder()
			h.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/snapshot", nil))
			if w.Code != http.StatusOK && w.Code != http.StatusServiceUnavailable {
				t.Errorf("unexpected status: %d", w.Code)
			}
		}()
	}
	wg.Wait()
}

func TestQualificationSmapsParser(t *testing.T) {
	for _, tc := range []struct {
		input     string
		ok        bool
		rss, lazy uint64
	}{
		{"Rss: 42 kB\nLazyFree: 3 kB\n", true, 43008, 3072},
		{"Rss: 0 kB\nLazyFree: 0 kB\n", true, 0, 0},
		{"Rss: 42 kB\n", false, 0, 0},
		{"Rss: 1 kB\nRss: 2 kB\nLazyFree: 0 kB\n", false, 0, 0},
		{"Rss: 18014398509481984 kB\nLazyFree: 0 kB\n", false, 0, 0},
	} {
		rss, lazy, err := parseQualificationSmaps(strings.NewReader(tc.input))
		if (err == nil) != tc.ok || (tc.ok && (rss != tc.rss || lazy != tc.lazy)) {
			t.Errorf("input %q: %d %d %v", tc.input, rss, lazy, err)
		}
	}
}

func TestQualificationCgroupIdentityFailsClosed(t *testing.T) {
	for _, input := range []string{"", "0::/\n", "0::/kubepods.slice/pod123\n", "0::/container\n0::/other\n", "1:memory:/foo\n"} {
		if err := validateQualificationCgroup(strings.NewReader(input), strings.NewReader("")); err == nil {
			t.Errorf("ambiguous cgroup accepted: %q", input)
		}
	}
}

func TestQualificationNotOnApplicationMux(t *testing.T) {
	w := httptest.NewRecorder()
	NewServerMux(ServerOptions{}).ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/snapshot", nil))
	if w.Code == http.StatusOK {
		t.Fatal("snapshot is exposed on application mux")
	}
}
