package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
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
