package main

import (
	"context"
	"net/http"
	"sync"
	"time"
)

type imageWaiter struct {
	ready    chan struct{}
	promoted bool
}

type imageGate struct {
	mu     sync.Mutex
	active bool
	waiter *imageWaiter
	wait   time.Duration
}

func newImageGate(wait time.Duration) *imageGate {
	return &imageGate{wait: wait}
}

func (g *imageGate) release() {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.waiter != nil {
		waiter := g.waiter
		g.waiter = nil
		waiter.promoted = true
		close(waiter.ready)
		return
	}
	g.active = false
}

func imageUnavailable(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Retry-After", "1")
	w.WriteHeader(http.StatusServiceUnavailable)
	_, _ = w.Write([]byte(`{"message":"Image service busy","status":503}`))
}

type imageResponseWriter struct {
	http.ResponseWriter
	ctx context.Context
}

func (w imageResponseWriter) WriteHeader(status int) {
	if w.ctx.Err() == nil {
		w.ResponseWriter.WriteHeader(status)
	}
}

func (w imageResponseWriter) Write(p []byte) (int, error) {
	if err := w.ctx.Err(); err != nil {
		return 0, err
	}
	return w.ResponseWriter.Write(p)
}

func (g *imageGate) Wrap(next http.Handler) http.Handler {
	return g.wrap(next, false)
}

func (g *imageGate) wrap(next http.Handler, observed bool) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		g.mu.Lock()
		if r.Context().Err() != nil {
			g.mu.Unlock()
			return
		}
		if !g.active {
			g.active = true
			g.mu.Unlock()
		} else {
			if g.waiter != nil {
				g.mu.Unlock()
				if observed {
					if t := qualificationTimingFrom(r); t != nil {
						t.status = 2
					}
				}
				imageUnavailable(w)
				return
			}
			waiter := &imageWaiter{ready: make(chan struct{})}
			g.waiter = waiter
			deadline := time.Now().Add(g.wait)
			g.mu.Unlock()

			timer := time.NewTimer(time.Until(deadline))
			defer timer.Stop()
			timedOut := false
			select {
			case <-waiter.ready:
			case <-r.Context().Done():
			case <-timer.C:
				timedOut = true
			}

			g.mu.Lock()
			promoted := waiter.promoted
			cancelled := r.Context().Err() != nil
			timedOut = timedOut || !time.Now().Before(deadline)
			if !promoted {
				g.waiter = nil
			}
			g.mu.Unlock()
			if cancelled || timedOut {
				if timedOut && !cancelled {
					if observed {
						if t := qualificationTimingFrom(r); t != nil {
							t.status = 2
						}
					}
					imageUnavailable(w)
				}
				if promoted {
					if observed {
						if t := qualificationTimingFrom(r); t != nil {
							t.record.QueueNS = qualificationDuration(t.entered)
							t.finish(r)
						}
					}
					g.release()
				}
				return
			}
		}

		var timing *qualificationTiming
		if observed {
			timing = qualificationTimingFrom(r)
			if timing != nil {
				timing.admitted = time.Now()
				timing.record.QueueNS = qualificationDuration(timing.entered)
			}
		}
		returned := false
		defer func() {
			defer g.release()
			if timing != nil && !returned {
				timing.status = 3
				timing.abnormal = true
			}
			var cleanup time.Time
			if timing != nil {
				cleanup = time.Now()
			}
			if r.MultipartForm != nil {
				_ = r.MultipartForm.RemoveAll()
			}
			if timing != nil {
				qualificationTimingCleanup(timing, cleanup)
				timing.finish(r)
			}
		}()
		if r.Context().Err() != nil {
			returned = true
			return
		}
		next.ServeHTTP(imageResponseWriter{ResponseWriter: w, ctx: r.Context()}, r)
		returned = true
	})
}
