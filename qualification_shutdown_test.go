package main

import (
	"context"
	"errors"
	"testing"
	"time"
)

type shutdownFunc func(context.Context) error

func (f shutdownFunc) Shutdown(ctx context.Context) error { return f(ctx) }

func TestQualificationShutdownDoesNotDelayApplicationDrain(t *testing.T) {
	observerStarted := make(chan struct{})
	applicationStarted := make(chan struct{})
	releaseObserver := make(chan struct{})
	observer := shutdownFunc(func(ctx context.Context) error {
		close(observerStarted)
		select {
		case <-releaseObserver:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	})
	application := shutdownFunc(func(ctx context.Context) error {
		close(applicationStarted)
		return ctx.Err()
	})
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- shutdownServers(ctx, application, observer) }()
	select {
	case <-observerStarted:
	case <-time.After(time.Second):
		t.Fatal("observer shutdown did not start")
	}
	select {
	case <-applicationStarted:
	case <-time.After(time.Second):
		close(releaseObserver)
		t.Fatal("application drain waited for observer shutdown")
	}
	close(releaseObserver)
	if err := <-done; err != nil {
		t.Fatalf("shutdown failed: %v", err)
	}
}

func TestQualificationShutdownBoundedAndReportsApplicationError(t *testing.T) {
	want := errors.New("application shutdown failed")
	application := shutdownFunc(func(context.Context) error { return want })
	observer := shutdownFunc(func(ctx context.Context) error {
		<-ctx.Done()
		return ctx.Err()
	})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if err := shutdownServers(ctx, application, observer); !errors.Is(err, want) {
		t.Fatalf("application error lost: %v", err)
	}
}

func TestQualificationShutdownWithoutObserver(t *testing.T) {
	called := false
	application := shutdownFunc(func(context.Context) error { called = true; return nil })
	if err := shutdownServers(context.Background(), application, nil); err != nil || !called {
		t.Fatalf("application shutdown: called=%t err=%v", called, err)
	}
}
