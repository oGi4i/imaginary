package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
)

func signatureTestOptions(t *testing.T) ServerOptions {
	t.Helper()
	placeholder, err := os.ReadFile("testdata/test.webp")
	if err != nil {
		t.Fatal(err)
	}
	return ServerOptions{
		EnableURLSignature: true,
		URLSignatureKey:    "test-key",
		EnablePlaceholder:  true,
		Placeholder:        "loaded-placeholder",
		PlaceholderImage:   placeholder,
		PlaceholderStatus:  http.StatusTeapot,
	}
}

func signedTestPath(path, key string) string {
	req := httptest.NewRequest(http.MethodPost, path, nil)
	h := hmac.New(sha256.New, []byte(key))
	_, _ = h.Write([]byte(req.URL.Path))
	_, _ = h.Write([]byte(req.URL.Query().Encode()))
	query := req.URL.Query()
	query.Set("sign", base64.RawURLEncoding.EncodeToString(h.Sum(nil)))
	return req.URL.Path + "?" + query.Encode()
}

func checkFixedError(t *testing.T, response *httptest.ResponseRecorder, want Error) {
	t.Helper()
	if response.Code != want.HTTPCode() {
		t.Errorf("status = %d, want %d", response.Code, want.HTTPCode())
	}
	if response.Header().Get("Content-Type") != "application/json" {
		t.Errorf("content type = %q, want JSON", response.Header().Get("Content-Type"))
	}
	var got Error
	if err := json.Unmarshal(response.Body.Bytes(), &got); err != nil || got != want {
		t.Errorf("response = %q, want %s", response.Body.String(), want.JSON())
	}
}

func TestURLSignatureRejectionsDoNotUsePlaceholder(t *testing.T) {
	o := signatureTestOptions(t)
	for _, tc := range []struct {
		name string
		path string
		want Error
	}{
		{"invalid-base64", "/resize?sign=%25&width=1&height=1&type=png", ErrInvalidURLSignature},
		{"mismatch", "/resize?sign=AA&width=1&height=1&type=png", ErrURLSignatureMismatch},
		{"missing", "/resize?width=1&height=1&type=png", ErrURLSignatureMismatch},
	} {
		for _, mode := range []string{"enabled", "path", "both"} {
			t.Run(tc.name+"/"+mode, func(t *testing.T) {
				options := o
				options.EnablePlaceholder = mode != "path"
				if mode == "enabled" {
					options.Placeholder = ""
				}
				called := false
				handler := validateURLSignature(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
					called = true
				}), options)
				response := httptest.NewRecorder()
				handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, tc.path, nil))
				if called {
					t.Error("rejected signature reached next handler")
				}
				checkFixedError(t, response, tc.want)
			})
		}
	}
}

func TestURLSignatureValidReachesNext(t *testing.T) {
	o := signatureTestOptions(t)
	called := false
	handler := validateURLSignature(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		w.WriteHeader(http.StatusNoContent)
	}), o)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, signedTestPath("/resize?width=1&height=1&type=png", o.URLSignatureKey), nil))
	if !called || response.Code != http.StatusNoContent {
		t.Fatalf("called = %t, status = %d; want next handler and 204", called, response.Code)
	}
}

func TestPublicRouteRejectionsDuringActiveImage(t *testing.T) {
	o := signatureTestOptions(t)
	o.APIKey = "test-api-key"
	LoadSources(o)
	mux := NewServerMux(o)
	block, release := admissionRelease(t)
	active := &admissionBody{read: make(chan struct{}), block: block}
	activeResults := make(chan admissionResult, 1)
	go admissionRequest(mux, "active", signedTestPath("/resize?key="+o.APIKey, o.URLSignatureKey), context.Background(), active, activeResults)
	select {
	case <-active.read:
	case <-time.After(admissionTestDeadline):
		t.Fatal("active request did not reach body read")
	}

	type result struct {
		response *httptest.ResponseRecorder
		want     Error
		body     *admissionBody
	}
	results := make(chan result, 12)
	noKeyOptions := o
	noKeyOptions.APIKey = ""
	noKeyMux := NewServerMux(noKeyOptions)
	for _, path := range []string{"/", "/form", "/health"} {
		for _, tc := range []struct {
			method  string
			key     string
			handler http.Handler
			want    Error
		}{
			{http.MethodGet, "", mux, ErrInvalidAPIKey},
			{http.MethodGet, "invalid", mux, ErrInvalidAPIKey},
			{http.MethodPut, o.APIKey, mux, ErrMethodNotAllowed},
			{http.MethodPut, "", noKeyMux, ErrMethodNotAllowed},
		} {
			body := &admissionBody{read: make(chan struct{})}
			req := httptest.NewRequest(tc.method, path+"?width=1&height=1&type=png", body)
			req.Header.Set("API-Key", tc.key)
			go func(handler http.Handler, req *http.Request, body *admissionBody, want Error) {
				response := httptest.NewRecorder()
				handler.ServeHTTP(response, req)
				results <- result{response, want, body}
			}(tc.handler, req, body, tc.want)
		}
	}
	for i := 0; i < 12; i++ {
		select {
		case got := <-results:
			checkFixedError(t, got.response, got.want)
			select {
			case <-got.body.read:
				t.Error("rejected public request body was read")
			default:
			}
		case <-time.After(admissionTestDeadline):
			t.Fatal("public route rejection blocked behind active image")
		}
	}
	health := make(chan int, 1)
	go func() {
		req := httptest.NewRequest(http.MethodGet, "/health", nil)
		req.Header.Set("API-Key", o.APIKey)
		response := httptest.NewRecorder()
		mux.ServeHTTP(response, req)
		health <- response.Code
	}()
	select {
	case status := <-health:
		if status != http.StatusOK {
			t.Errorf("authorized health status = %d, want 200", status)
		}
	case <-time.After(admissionTestDeadline):
		t.Fatal("authorized health blocked behind active image")
	}
	release()
	admissionReceive(t, activeResults)
}

func TestURLSignatureRejectionsDuringActiveImage(t *testing.T) {
	o := signatureTestOptions(t)
	LoadSources(o)
	mux := NewServerMux(o)
	block, release := admissionRelease(t)
	active := &admissionBody{read: make(chan struct{}), block: block}
	results := make(chan admissionResult, 4)
	go admissionRequest(mux, "active", signedTestPath("/resize", o.URLSignatureKey), context.Background(), active, results)
	select {
	case <-active.read:
	case <-time.After(admissionTestDeadline):
		t.Fatal("active request did not reach body read")
	}

	paths := []string{
		"/resize?sign=%25&width=1&height=1&type=png",
		"/resize?sign=AA&width=1&height=1&type=png",
		"/resize?width=1&height=1&type=png",
	}
	bodies := make([]*admissionBody, len(paths))
	for i, path := range paths {
		bodies[i] = &admissionBody{read: make(chan struct{})}
		go admissionRequest(mux, path, path, context.Background(), bodies[i], results)
	}
	for range paths {
		result := admissionReceive(t, results)
		want := ErrURLSignatureMismatch
		if strings.Contains(result.id, "sign=%25") {
			want = ErrInvalidURLSignature
		}
		checkFixedError(t, result.response, want)
	}
	for _, body := range bodies {
		select {
		case <-body.read:
			t.Error("rejected request body was read")
		default:
		}
	}
	health := make(chan int, 1)
	go func() {
		response := httptest.NewRecorder()
		mux.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/health", nil))
		health <- response.Code
	}()
	select {
	case status := <-health:
		if status != http.StatusOK {
			t.Errorf("health status = %d, want 200", status)
		}
	case <-time.After(admissionTestDeadline):
		t.Fatal("health blocked while image slot was occupied")
	}
	release()
	admissionReceive(t, results)
}
