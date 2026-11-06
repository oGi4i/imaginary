package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"image"
	_ "image/jpeg"
	"io/ioutil"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/h2non/bimg"
)

func TestNativeOperationCacheDisabledAtStartup(t *testing.T) {
	if got := nativeOperationCacheLimit(); got != 0 {
		t.Fatalf("operation cache limit = %d, want 0 before requests", got)
	}
}

func TestNativeOperationCacheRemainsEmpty(t *testing.T) {
	input, err := ioutil.ReadFile("testdata/large.jpg")
	if err != nil {
		t.Fatal(err)
	}
	options := ServerOptions{HTTPReadTimeout: 60, HTTPWriteTimeout: 60, HTTPCacheTTL: -1}
	LoadSources(options)
	handler := NewServerMux(options)
	for i := 0; i < 20; i++ {
		operations := []map[string]interface{}{
			{"operation": "autorotate"},
			{"operation": "fit", "params": map[string]interface{}{"width": 128, "height": 128}},
			{"operation": "convert", "params": map[string]interface{}{"type": "jpeg"}},
		}
		encoded, err := json.Marshal(operations)
		if err != nil {
			t.Fatal(err)
		}
		req := httptest.NewRequest(http.MethodPost, "/pipeline", bytes.NewReader(input))
		query := req.URL.Query()
		query.Set("operations", string(encoded))
		req.URL.RawQuery = query.Encode()
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, req)
		if response.Code != http.StatusOK {
			t.Fatalf("request %d: status %d: %s", i, response.Code, response.Body.String())
		}
		output, format, err := image.DecodeConfig(bytes.NewReader(response.Body.Bytes()))
		if err != nil || format != "jpeg" || output.Width > 128 || output.Height > 128 {
			t.Fatalf("request %d: invalid output %s %dx%d: %v", i, format, output.Width, output.Height, err)
		}
		if got := nativeOperationCacheSize(); got != 0 {
			t.Fatalf("request %d retained %d cached operations", i, got)
		}
		if memory := bimg.VipsMemory(); memory.Memory != 0 || memory.Allocations != 0 {
			t.Fatalf("request %d retained tracked native resources: %+v", i, memory)
		}
	}
}

func TestNativeCacheErrorRecovery(t *testing.T) {
	input, err := ioutil.ReadFile("testdata/large.jpg")
	if err != nil {
		t.Fatal(err)
	}
	options := ServerOptions{HTTPCacheTTL: -1}
	LoadSources(options)
	handler := NewServerMux(options)
	for _, test := range []struct {
		name string
		body []byte
		path string
	}{
		// Header admission accepts this JPEG, but its missing tables must fail native decoding.
		{"decoder", rasterTestJPEG(1, 1), "/resize?width=32"},
		{"operation", input, "/extract?top=999999&left=999999&areawidth=10&areaheight=10"},
	} {
		t.Run(test.name, func(t *testing.T) {
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, test.path, bytes.NewReader(test.body)))
			var failure Error
			if err := json.Unmarshal(response.Body.Bytes(), &failure); err != nil {
				t.Fatalf("invalid error response: %v", err)
			}
			if response.Code != http.StatusBadRequest || !strings.HasPrefix(failure.Message, "Error while processing the image:") {
				t.Fatalf("expected native operation error, got %d: %s", response.Code, response.Body.String())
			}
			assertNativeRecovery(t, handler, input)
		})
	}
	failedWriter := &failedImageWriter{header: make(http.Header)}
	handler.ServeHTTP(failedWriter, httptest.NewRequest(http.MethodPost, "/resize?width=32", bytes.NewReader(input)))
	if !failedWriter.written || failedWriter.header.Get("Content-Type") != "image/jpeg" {
		t.Fatal("output failure did not reach the image response write")
	}
	assertNativeRecovery(t, handler, input)
}

func assertNativeRecovery(t *testing.T, handler http.Handler, input []byte) {
	t.Helper()
	if got := nativeOperationCacheSize(); got != 0 {
		t.Fatalf("error path retained %d cached operations", got)
	}
	if memory := bimg.VipsMemory(); memory.Memory != 0 || memory.Allocations != 0 {
		t.Fatalf("error path retained tracked native resources: %+v", memory)
	}
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/resize?width=32", bytes.NewReader(input)))
	if response.Code != http.StatusOK {
		t.Fatalf("recovery status %d: %s", response.Code, response.Body.String())
	}
	if got := nativeOperationCacheSize(); got != 0 {
		t.Fatalf("recovery retained %d cached operations", got)
	}
}

type failedImageWriter struct {
	header  http.Header
	written bool
}

func (w *failedImageWriter) Header() http.Header { return w.header }
func (w *failedImageWriter) WriteHeader(int)     {}
func (w *failedImageWriter) Write([]byte) (int, error) {
	w.written = true
	return 0, errors.New("client disconnected")
}
