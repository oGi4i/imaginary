package main

import (
	"bytes"
	"context"
	"io/ioutil"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/h2non/bimg"
)

func TestImageGateReleasesNativeResourcesBeforeNextJob(t *testing.T) {
	input, err := ioutil.ReadFile("testdata/large.jpg")
	if err != nil {
		t.Fatal(err)
	}
	started := make(chan string, 2)
	block, release := admissionRelease(t)
	handler := newImageGate(time.Minute).Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Test-Request") == "second" {
			if size := nativeOperationCacheSize(); size != 0 {
				t.Errorf("next job entered with %d cached operations", size)
			}
			if memory := bimg.VipsMemory(); memory.Memory != 0 || memory.Allocations != 0 {
				t.Errorf("next job entered with native resources: %+v", memory)
			}
		}
		started <- r.Header.Get("X-Test-Request")
		_, err := bimg.Resize(input, bimg.Options{Width: 32, Type: bimg.JPEG})
		if err != nil {
			t.Errorf("resize: %v", err)
		}
		if r.Header.Get("X-Test-Request") == "first" {
			<-block
		}
	}))
	results := make(chan admissionResult, 2)
	go admissionRequest(handler, "first", "/pipeline", context.Background(), bytes.NewReader(input), results)
	if id := admissionStarted(t, started); id != "first" {
		t.Fatalf("first handler = %q", id)
	}
	go admissionRequest(handler, "second", "/pipeline", context.Background(), nil, results)
	release()
	admissionReceive(t, results)
	if id := admissionStarted(t, started); id != "second" {
		t.Fatalf("second handler = %q", id)
	}
	admissionReceive(t, results)
}

func TestImageGateDoesNotStartNativeJobAfterCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	called := false
	handler := newImageGate(time.Second).Wrap(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/pipeline", nil).WithContext(ctx))
	if called {
		t.Fatal("cancelled native job entered handler")
	}
}
