package main

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"io/ioutil"
	"math"
	"net"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/h2non/bimg"
)

const qualificationAddress = "127.0.0.1:9001"

var qualificationLifetime = newQualificationLifetime()
var qualificationSequence uint64

func newQualificationLifetime() string {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return ""
	}
	return hex.EncodeToString(id[:])
}

type qualificationSnapshot struct {
	SchemaVersion            int                         `json:"schema_version"`
	BuildIdentity            string                      `json:"build_identity"`
	ProcessLifetime          string                      `json:"process_lifetime"`
	Sequence                 uint64                      `json:"sequence"`
	GoHeapAllocBytes         uint64                      `json:"go_heap_alloc_bytes"`
	VipsTrackedBytes         int64                       `json:"vips_tracked_bytes"`
	VipsTrackedAllocations   int64                       `json:"vips_tracked_allocations"`
	VipsCacheOperations      int                         `json:"vips_cache_operations"`
	VipsCacheLimitOperations int                         `json:"vips_cache_limit_operations"`
	ProcessRSSBytes          *uint64                     `json:"process_rss_bytes"`
	ProcessLazyFreeBytes     *uint64                     `json:"process_lazyfree_bytes"`
	ProcessStatus            string                      `json:"process_status"`
	CgroupStatus             string                      `json:"cgroup_status"`
	CgroupCurrentBytes       *uint64                     `json:"cgroup_current_bytes"`
	CgroupPeakBytes          *uint64                     `json:"cgroup_peak_bytes"`
	CgroupAnonBytes          *uint64                     `json:"cgroup_anon_bytes"`
	CgroupFileBytes          *uint64                     `json:"cgroup_file_bytes"`
	Lifecycle                qualificationTimingSnapshot `json:"lifecycle"`
}

func newQualificationHandler() http.Handler {
	return qualificationHandler(qualificationProcessMemory)
}

func qualificationProcessMemory() (*uint64, *uint64, string) {
	f, err := os.Open("/proc/self/smaps_rollup")
	if err != nil {
		return nil, nil, "unavailable"
	}
	defer f.Close()
	rss, lazy, err := parseQualificationSmaps(f)
	if err != nil {
		return nil, nil, "unavailable"
	}
	return &rss, &lazy, "ok"
}

func qualificationHandler(processMemory func() (*uint64, *uint64, string)) http.Handler {
	semaphore := make(chan struct{}, 1)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Connection", "close")
		if r.Method != http.MethodGet || r.URL.Path != "/snapshot" || r.URL.RawQuery != "" || r.ContentLength != 0 || len(r.TransferEncoding) != 0 {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = io.WriteString(w, "{\"error\":\"invalid_request\"}\n")
			return
		}
		select {
		case semaphore <- struct{}{}:
			defer func() { <-semaphore }()
		default:
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = io.WriteString(w, "{\"error\":\"busy\"}\n")
			return
		}
		if qualificationLifetime == "" {
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = io.WriteString(w, "{\"error\":\"identity_unavailable\"}\n")
			return
		}
		var mem runtime.MemStats
		runtime.ReadMemStats(&mem)
		native := bimg.VipsMemory()
		snapshot := qualificationSnapshot{
			SchemaVersion: 3, BuildIdentity: Version, ProcessLifetime: qualificationLifetime,
			Sequence: atomic.AddUint64(&qualificationSequence, 1), GoHeapAllocBytes: mem.HeapAlloc,
			VipsTrackedBytes: native.Memory, VipsTrackedAllocations: native.Allocations,
			VipsCacheOperations: nativeOperationCacheSize(), VipsCacheLimitOperations: nativeOperationCacheLimit(),
			ProcessStatus: "unavailable", CgroupStatus: "identity_unproven",
		}
		snapshot.ProcessRSSBytes, snapshot.ProcessLazyFreeBytes, snapshot.ProcessStatus = processMemory()
		snapshot.Lifecycle = qualificationTimings.snapshot()
		_ = json.NewEncoder(w).Encode(snapshot)
	})
}

func parseQualificationSmaps(r io.Reader) (uint64, uint64, error) {
	data, err := ioutil.ReadAll(io.LimitReader(r, 65537))
	if err != nil || len(data) > 65536 {
		return 0, 0, errors.New("invalid smaps")
	}
	var rss, lazy uint64
	seen := map[string]bool{}
	scanner := bufio.NewScanner(strings.NewReader(string(data)))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) == 0 || (fields[0] != "Rss:" && fields[0] != "LazyFree:") {
			continue
		}
		if len(fields) != 3 || fields[2] != "kB" || seen[fields[0]] {
			return 0, 0, errors.New("invalid smaps")
		}
		value, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil || value > math.MaxUint64/1024 {
			return 0, 0, errors.New("invalid smaps")
		}
		seen[fields[0]] = true
		if fields[0] == "Rss:" {
			rss = value * 1024
		} else {
			lazy = value * 1024
		}
	}
	if scanner.Err() != nil || !seen["Rss:"] || !seen["LazyFree:"] {
		return 0, 0, errors.New("invalid smaps")
	}
	return rss, lazy, nil
}

type qualificationListener struct {
	net.Listener
	slots chan struct{}
}

type qualificationConn struct {
	net.Conn
	slots  chan struct{}
	closed uint32
}

func (c *qualificationConn) Close() error {
	err := c.Conn.Close()
	if atomic.CompareAndSwapUint32(&c.closed, 0, 1) {
		<-c.slots
	}
	return err
}

func (l *qualificationListener) Accept() (net.Conn, error) {
	for {
		c, err := l.Listener.Accept()
		if err != nil {
			return nil, err
		}
		select {
		case l.slots <- struct{}{}:
			_ = c.SetDeadline(time.Now().Add(5 * time.Second))
			return &qualificationConn{Conn: c, slots: l.slots}, nil
		default:
			_ = c.Close()
		}
	}
}

func startQualificationServer() (*http.Server, error) {
	l, err := net.Listen("tcp4", qualificationAddress)
	if err != nil {
		return nil, err
	}
	s := &http.Server{Handler: newQualificationHandler(), ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second, MaxHeaderBytes: 4096}
	s.SetKeepAlivesEnabled(false)
	go func() {
		if err := s.Serve(&qualificationListener{Listener: l, slots: make(chan struct{}, 8)}); err != nil && err != http.ErrServerClosed {
			_ = s.Close()
		}
	}()
	return s, nil
}
