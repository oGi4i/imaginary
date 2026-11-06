package main

import (
	"context"
	"net/http"
	"strconv"
	"sync"
	"time"
)

type qualificationTimingRecord struct {
	RequestSequence    uint64 `json:"request_sequence"`
	CompletionSequence uint64 `json:"completion_sequence"`
	Status             int    `json:"status"`
	QueueNS            *int64 `json:"queue_ns"`
	SourcePreflightNS  *int64 `json:"source_preflight_ns"`
	TransformOutputNS  *int64 `json:"transform_output_ns"`
	ResponseNS         *int64 `json:"response_ns"`
	CleanupNS          *int64 `json:"cleanup_ns"`
	ProcessingNS       *int64 `json:"processing_ns"`
}

type qualificationTimingSnapshot struct {
	RequestSequence          uint64                      `json:"request_sequence"`
	OldestCompletionSequence uint64                      `json:"oldest_completion_sequence"`
	LatestCompletionSequence uint64                      `json:"latest_completion_sequence"`
	OverwriteTotal           uint64                      `json:"overwrite_total"`
	Records                  []qualificationTimingRecord `json:"records"`
}

type qualificationTimingStore struct {
	mu         sync.Mutex
	requests   uint64
	completed  uint64
	overwrites uint64
	records    [32]qualificationTimingRecord
	count      int
}

var qualificationTimings = newQualificationTimingStore()

func newQualificationTimingStore() *qualificationTimingStore { return &qualificationTimingStore{} }

func (s *qualificationTimingStore) begin() *qualificationTiming {
	s.mu.Lock()
	s.requests++
	sequence := s.requests
	s.mu.Unlock()
	return &qualificationTiming{store: s, record: qualificationTimingRecord{RequestSequence: sequence}}
}

func (s *qualificationTimingStore) publish(r qualificationTimingRecord) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.completed++
	r.CompletionSequence = s.completed
	if s.count < len(s.records) {
		s.count++
	} else {
		s.overwrites++
	}
	s.records[(s.completed-1)%uint64(len(s.records))] = r
}

func (s *qualificationTimingStore) snapshot() qualificationTimingSnapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	result := qualificationTimingSnapshot{RequestSequence: s.requests, LatestCompletionSequence: s.completed, OverwriteTotal: s.overwrites, Records: make([]qualificationTimingRecord, 0, s.count)}
	if s.count != 0 {
		result.OldestCompletionSequence = s.completed - uint64(s.count) + 1
		for seq := result.OldestCompletionSequence; seq <= s.completed; seq++ {
			result.Records = append(result.Records, s.records[(seq-1)%uint64(len(s.records))])
		}
	}
	return result
}

type qualificationTimingKey struct{}

type qualificationTiming struct {
	store             *qualificationTimingStore
	record            qualificationTimingRecord
	queueStart        time.Time
	admitted          time.Time
	transformStart    time.Time
	transformSegments int
	status            int
	abnormal          bool
	published         bool
}

func qualificationTimingFrom(r *http.Request) *qualificationTiming {
	t, _ := r.Context().Value(qualificationTimingKey{}).(*qualificationTiming)
	return t
}

func qualificationDuration(start time.Time) *int64 {
	d := time.Since(start).Nanoseconds()
	return &d
}

func qualificationTimingSourceDone(r *http.Request) {
	if t := qualificationTimingFrom(r); t != nil && !t.admitted.IsZero() {
		duration := time.Since(t.admitted).Nanoseconds()
		if t.record.CleanupNS != nil {
			duration -= *t.record.CleanupNS
		}
		t.record.SourcePreflightNS = &duration
	}
}

func qualificationTimingTransformStart(r *http.Request) {
	if t := qualificationTimingFrom(r); t != nil {
		t.transformStart = time.Now()
	}
}

func qualificationTimingTransformDone(r *http.Request) {
	if t := qualificationTimingFrom(r); t != nil && !t.transformStart.IsZero() {
		duration := time.Since(t.transformStart).Nanoseconds()
		if t.record.TransformOutputNS != nil {
			duration += *t.record.TransformOutputNS
		}
		t.record.TransformOutputNS = &duration
		t.transformStart = time.Time{}
		t.transformSegments++
	}
}

func qualificationTimingCleanup(t *qualificationTiming, start time.Time) {
	duration := time.Since(start).Nanoseconds()
	if t.record.CleanupNS != nil {
		duration += *t.record.CleanupNS
	}
	t.record.CleanupNS = &duration
}

func qualificationTimingError(r *http.Request, _ error) {
	if t := qualificationTimingFrom(r); t != nil {
		t.status = 3
	}
}

func (t *qualificationTiming) finish(r *http.Request) {
	if t.published {
		return
	}
	if r.Context().Err() != nil && !t.abnormal {
		t.status = 4
	}
	if t.status == 0 {
		t.status = 1
	}
	t.record.Status = t.status
	if t.record.TransformOutputNS != nil && t.record.CleanupNS != nil {
		processing := *t.record.TransformOutputNS + *t.record.CleanupNS
		t.record.ProcessingNS = &processing
	}
	t.store.publish(t.record)
	t.published = true
}

type qualificationTimingWriter struct {
	http.ResponseWriter
	timing *qualificationTiming
}

func (w *qualificationTimingWriter) WriteHeader(code int) {
	if w.timing.status != 3 && w.timing.status != 2 {
		if code >= 500 {
			w.timing.status = 3
		} else if code >= 400 {
			w.timing.status = 2
		}
	}
	start := time.Now()
	w.ResponseWriter.WriteHeader(code)
	w.addResponse(start)
}

func (w *qualificationTimingWriter) Write(p []byte) (int, error) {
	start := time.Now()
	n, err := w.ResponseWriter.Write(p)
	w.addResponse(start)
	if err != nil {
		w.timing.status = 3
	}
	return n, err
}

func (w *qualificationTimingWriter) addResponse(start time.Time) {
	d := time.Since(start).Nanoseconds()
	if w.timing.record.ResponseNS != nil {
		d += *w.timing.record.ResponseNS
	}
	w.timing.record.ResponseNS = &d
}

func qualificationTimingMiddleware(enabled bool, store *qualificationTimingStore, next http.Handler) http.Handler {
	if !enabled {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t := store.begin()
		w.Header().Set("X-Imaginary-Request-Sequence", strconv.FormatUint(t.record.RequestSequence, 10))
		r = r.WithContext(context.WithValue(r.Context(), qualificationTimingKey{}, t))
		returned := false
		defer func() {
			if !returned && !t.published {
				t.status = 3
				t.abnormal = true
			}
			t.finish(r)
		}()
		next.ServeHTTP(&qualificationTimingWriter{ResponseWriter: w, timing: t}, r)
		returned = true
	})
}
