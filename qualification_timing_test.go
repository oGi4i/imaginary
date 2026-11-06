package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"io/ioutil"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func TestQualificationLifecycleDisabled(t *testing.T) {
	store := newQualificationTimingStore()
	h := qualificationTimingMiddleware(false, store, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil))
	if got := store.snapshot(); got.RequestSequence != 0 || len(got.Records) != 0 {
		t.Fatalf("disabled timing recorded work: %+v", got)
	}
}

func TestQualificationLifecycleSuccessAndTransfer(t *testing.T) {
	store := newQualificationTimingStore()
	gate := newImageGate(time.Second)
	started := make(chan struct{})
	release := make(chan struct{})
	h := qualificationTimingMiddleware(true, store, gate.wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		qualificationTimingSourceDone(r)
		qualificationTimingTransformStart(r)
		qualificationTimingTransformDone(r)
		_, _ = w.Write([]byte("image"))
	}), true))
	w := &qualificationSlowWriter{header: make(http.Header), started: started, release: release}
	done := make(chan struct{})
	go func() {
		h.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/resize?private=value", nil))
		close(done)
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("response did not start")
	}
	if got := store.snapshot(); len(got.Records) != 0 {
		t.Fatal("record published before transfer and cleanup")
	}
	close(release)
	<-done
	got := store.snapshot()
	if len(got.Records) != 1 {
		t.Fatalf("records: %+v", got)
	}
	record := got.Records[0]
	if record.Status != 1 || record.SourcePreflightNS == nil || record.TransformOutputNS == nil || record.ResponseNS == nil || record.CleanupNS == nil || record.ProcessingNS == nil || *record.ProcessingNS != *record.TransformOutputNS+*record.CleanupNS {
		t.Fatalf("invalid success record: %+v", record)
	}
	encoded, err := json.Marshal(got)
	if err != nil || bytes.Contains(encoded, []byte("private")) || bytes.Contains(encoded, []byte("resize")) {
		t.Fatalf("unsafe timing payload: %s %v", encoded, err)
	}
}

type qualificationSlowWriter struct {
	header  http.Header
	started chan struct{}
	release chan struct{}
}

func (w *qualificationSlowWriter) Header() http.Header { return w.header }
func (w *qualificationSlowWriter) WriteHeader(int)     {}
func (w *qualificationSlowWriter) Write(p []byte) (int, error) {
	close(w.started)
	<-w.release
	return len(p), nil
}

func TestQualificationLifecycleCompletionBeforePromotion(t *testing.T) {
	store := newQualificationTimingStore()
	gate := newImageGate(time.Second)
	firstStarted := make(chan struct{})
	releaseFirst := make(chan struct{})
	secondSaw := make(chan bool, 1)
	var calls int
	h := qualificationTimingMiddleware(true, store, gate.wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			close(firstStarted)
			<-releaseFirst
			return
		}
		got := store.snapshot()
		secondSaw <- len(got.Records) == 1 && got.Records[0].CleanupNS != nil
	}), true))
	done := make(chan struct{}, 2)
	for i := 0; i < 2; i++ {
		go func() {
			h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil))
			done <- struct{}{}
		}()
		if i == 0 {
			<-firstStarted
		}
	}
	close(releaseFirst)
	<-done
	<-done
	if !<-secondSaw {
		t.Fatal("next admission preceded cleanup publication")
	}
}

func TestQualificationLifecycleStatuses(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		cancel bool
		want   int
	}{
		{"rejected", http.StatusBadRequest, false, 2},
		{"error", http.StatusInternalServerError, false, 3},
		{"cancelled", http.StatusOK, true, 4},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := newQualificationTimingStore()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			h := qualificationTimingMiddleware(true, store, newImageGate(time.Second).wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if tc.cancel {
					cancel()
				}
				w.WriteHeader(tc.status)
			}), true))
			h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil).WithContext(ctx))
			got := store.snapshot()
			if len(got.Records) != 1 || got.Records[0].Status != tc.want {
				t.Fatalf("wrong outcome: %+v", got)
			}
		})
	}
}

func TestQualificationLifecycleRetentionAndConcurrency(t *testing.T) {
	store := newQualificationTimingStore()
	h := qualificationTimingMiddleware(true, store, newImageGate(time.Second).wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}), true))
	var wg sync.WaitGroup
	for i := 0; i < 64; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil))
			_ = store.snapshot()
		}()
	}
	wg.Wait()
	got := store.snapshot()
	if got.RequestSequence != 64 || len(got.Records) != 32 || got.OverwriteTotal != 32 || got.OldestCompletionSequence != 33 || got.LatestCompletionSequence != 64 {
		t.Fatalf("invalid bounded retention: %+v", got)
	}
	for i, record := range got.Records {
		if record.CompletionSequence != uint64(i+33) {
			t.Fatalf("completion ordering: %+v", got.Records)
		}
	}
	encoded, err := json.Marshal(got)
	if err != nil || len(encoded) > 32768 {
		t.Fatalf("unbounded timing payload: %d, %v", len(encoded), err)
	}
}

func TestQualificationLifecycleQueueTimeoutIsRejection(t *testing.T) {
	store := newQualificationTimingStore()
	gate := newImageGate(20 * time.Millisecond)
	started, release := make(chan struct{}), make(chan struct{})
	h := qualificationTimingMiddleware(true, store, gate.wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-release
	}), true))
	done := make(chan struct{})
	go func() {
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil))
		close(done)
	}()
	<-started
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/resize", nil))
	got := store.snapshot()
	close(release)
	<-done
	if w.Code != http.StatusServiceUnavailable || len(got.Records) != 1 || got.Records[0].Status != 2 || got.Records[0].CleanupNS != nil {
		t.Fatalf("queue timeout must be rejection without cleanup: status=%d records=%+v", w.Code, got.Records)
	}
}

func TestQualificationLifecyclePanicIsError(t *testing.T) {
	store := newQualificationTimingStore()
	h := qualificationTimingMiddleware(true, store, newImageGate(time.Second).wrap(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		panic("private")
	}), true))
	func() {
		defer func() {
			if recover() == nil {
				t.Error("panic did not propagate")
			}
		}()
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil))
	}()
	got := store.snapshot()
	if len(got.Records) != 1 || got.Records[0].Status != 3 || got.Records[0].CleanupNS == nil {
		t.Fatalf("panic must publish error after cleanup: %+v", got)
	}
}

func TestQualificationLifecycleMultipartCleanupAccounting(t *testing.T) {
	store := newQualificationTimingStore()
	h := qualificationTimingMiddleware(true, store, newImageGate(time.Second).wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body bytes.Buffer
		mw := multipart.NewWriter(&body)
		part, err := mw.CreateFormFile("file", "image.png")
		if err != nil {
			t.Fatal(err)
		}
		_, _ = part.Write([]byte("small"))
		_ = mw.Close()
		r.Body = io.NopCloser(bytes.NewReader(body.Bytes()))
		r.Header.Set("Content-Type", mw.FormDataContentType())
		if err := r.ParseMultipartForm(0); err != nil {
			t.Fatal(err)
		}
		before := time.Now()
		if _, err := readFormBody(r, 4096); err != nil {
			t.Fatal(err)
		}
		after := time.Now()
		qualificationTimingSourceDone(r)
		qualificationTimingTransformStart(r)
		qualificationTimingTransformDone(r)
		timing := qualificationTimingFrom(r)
		if timing.record.CleanupNS == nil || *timing.record.CleanupNS <= 0 || *timing.record.CleanupNS > after.Sub(before).Nanoseconds() {
			t.Error("actual source cleanup was not measured")
		}
	}), true))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil))
	got := store.snapshot()
	if len(got.Records) != 1 || got.Records[0].CleanupNS == nil || got.Records[0].SourcePreflightNS == nil || *got.Records[0].CleanupNS <= 0 {
		t.Fatalf("multipart cleanup must be timed: %+v", got)
	}
}

func TestQualificationLifecycleTransformSegmentsAccumulate(t *testing.T) {
	store := newQualificationTimingStore()
	h := qualificationTimingMiddleware(true, store, newImageGate(time.Second).wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		qualificationTimingSourceDone(r)
		qualificationTimingTransformStart(r)
		time.Sleep(10 * time.Millisecond)
		qualificationTimingTransformDone(r)
		qualificationTimingTransformStart(r)
		time.Sleep(10 * time.Millisecond)
		qualificationTimingTransformDone(r)
	}), true))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil))
	got := store.snapshot()
	if len(got.Records) != 1 || got.Records[0].TransformOutputNS == nil || *got.Records[0].TransformOutputNS < int64(18*time.Millisecond) {
		t.Fatalf("transform segments must accumulate: %+v", got)
	}
}

func TestQualificationLifecycleNativePlaceholderIsTimed(t *testing.T) {
	placeholder, err := ioutil.ReadFile("testdata/large.jpg")
	if err != nil {
		t.Fatal(err)
	}
	store := newQualificationTimingStore()
	var originalTransform int64
	var segments int
	o := ServerOptions{QualSnapshot: true, EnablePlaceholder: true, PlaceholderImage: placeholder, PlaceholderStatus: http.StatusTeapot}
	operation := Operation(func([]byte, ImageOptions) (Image, error) {
		return Image{}, errors.New("private")
	})
	h := qualificationTimingMiddleware(true, store, newImageGate(time.Second).wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		imageHandler(w, r, rasterTestPNG(1, 1), operation, o)
		timing := qualificationTimingFrom(r)
		originalTransform = *timing.record.TransformOutputNS
		segments = timing.transformSegments
	}), true))
	response := httptest.NewRecorder()
	h.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/resize?width=64&height=64", nil))
	got := store.snapshot()
	if response.Code != http.StatusTeapot || response.Body.Len() == 0 || len(got.Records) != 1 || got.Records[0].Status != 3 {
		t.Fatalf("native placeholder response changed: status=%d records=%+v", response.Code, got.Records)
	}
	if segments != 2 || got.Records[0].TransformOutputNS == nil || *got.Records[0].TransformOutputNS != originalTransform || originalTransform <= 0 {
		t.Fatalf("invalid native timing: %+v", got.Records[0])
	}
}

func TestQualificationLifecycleAuthPlaceholderIsRejection(t *testing.T) {
	placeholder, err := ioutil.ReadFile("testdata/large.jpg")
	if err != nil {
		t.Fatal(err)
	}
	store := newQualificationTimingStore()
	o := ServerOptions{QualSnapshot: true, APIKey: "required", EnablePlaceholder: true, PlaceholderImage: placeholder, PlaceholderStatus: http.StatusOK}
	auth := authorizeClient(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("unauthorized request reached handler")
	}), o)
	h := qualificationTimingMiddleware(true, store, newImageGate(time.Second).wrap(auth, true))
	response := httptest.NewRecorder()
	h.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/resize?width=64&height=64", nil))
	got := store.snapshot()
	if response.Code != http.StatusOK || response.Body.Len() == 0 {
		t.Fatalf("placeholder response changed: status=%d body=%d", response.Code, response.Body.Len())
	}
	if len(got.Records) != 1 || got.Records[0].Status != 2 || got.Records[0].TransformOutputNS == nil || *got.Records[0].TransformOutputNS <= 0 {
		t.Fatalf("auth rejection recorded incorrectly: %+v", got)
	}
}

func TestQualificationLifecycleURLSignatureRejection(t *testing.T) {
	placeholder, err := ioutil.ReadFile("testdata/large.jpg")
	if err != nil {
		t.Fatal(err)
	}
	before := qualificationTimings.snapshot().LatestCompletionSequence
	o := ServerOptions{QualSnapshot: true, EnableURLSignature: true, URLSignatureKey: "secret", EnablePlaceholder: true, PlaceholderImage: placeholder, PlaceholderStatus: http.StatusOK}
	h := ImageMiddleware(o)(func([]byte, ImageOptions) (Image, error) {
		t.Fatal("invalid signature reached operation")
		return Image{}, nil
	})
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/resize?width=64&height=64&sign=invalid", nil))
	got := qualificationTimings.snapshot()
	if w.Code != http.StatusOK || w.Body.Len() == 0 || got.LatestCompletionSequence != before+1 {
		t.Fatalf("missing signature rejection record: status=%d snapshot=%+v", w.Code, got)
	}
	record := got.Records[len(got.Records)-1]
	if record.Status != 2 || record.CleanupNS != nil {
		t.Fatalf("invalid signature outcome: %+v", record)
	}
}

func TestQualificationLifecycleOutsideGatePanic(t *testing.T) {
	store := newQualificationTimingStore()
	h := qualificationTimingMiddleware(true, store, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		panic(nil)
	}))
	returned := false
	func() {
		defer func() { _ = recover() }()
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil))
		returned = true
	}()
	got := store.snapshot()
	if returned || len(got.Records) != 1 || got.Records[0].Status != 3 {
		t.Fatalf("panic must propagate and record error: returned=%t records=%+v", returned, got)
	}
}

func TestQualificationLifecycleBrokenAuthPlaceholderIsError(t *testing.T) {
	store := newQualificationTimingStore()
	o := ServerOptions{QualSnapshot: true, APIKey: "required", EnablePlaceholder: true, PlaceholderImage: []byte("not an image"), PlaceholderStatus: http.StatusOK}
	auth := authorizeClient(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("unauthorized request reached handler")
	}), o)
	h := qualificationTimingMiddleware(true, store, newImageGate(time.Second).wrap(auth, true))
	response := httptest.NewRecorder()
	h.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/resize?width=64&height=64", nil))
	got := store.snapshot()
	if response.Code != http.StatusBadRequest || len(got.Records) != 1 || got.Records[0].Status != 3 {
		t.Fatalf("broken native placeholder must be an error: status=%d record=%+v", response.Code, got)
	}
}

func TestQualificationLifecycleErrorMarker(t *testing.T) {
	store := newQualificationTimingStore()
	h := qualificationTimingMiddleware(true, store, newImageGate(time.Second).wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		qualificationTimingError(r, errors.New("secret path"))
	}), true))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/resize", nil))
	got := store.snapshot()
	if len(got.Records) != 1 || got.Records[0].Status != 3 {
		t.Fatalf("error not recorded: %+v", got)
	}
}
