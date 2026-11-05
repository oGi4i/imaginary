package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

const admissionTestDeadline = 5 * time.Second

type admissionResult struct {
	id       string
	response *httptest.ResponseRecorder
}

func admissionRequest(h http.Handler, id, path string, ctx context.Context, body io.Reader, results chan<- admissionResult) {
	req := httptest.NewRequest(http.MethodPost, path, body).WithContext(ctx)
	req.Header.Set("X-Test-Request", id)
	response := httptest.NewRecorder()
	h.ServeHTTP(response, req)
	results <- admissionResult{id: id, response: response}
}

func admissionReceive(t *testing.T, ch <-chan admissionResult) admissionResult {
	t.Helper()
	select {
	case result := <-ch:
		return result
	case <-time.After(admissionTestDeadline):
		t.Fatal("request did not finish")
		return admissionResult{}
	}
}

func admissionStarted(t *testing.T, ch <-chan string) string {
	t.Helper()
	select {
	case id := <-ch:
		return id
	case <-time.After(admissionTestDeadline):
		t.Fatal("handler did not start")
		return ""
	}
}

func admissionCheckUnavailable(t *testing.T, response *httptest.ResponseRecorder) {
	t.Helper()
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", response.Code)
	}
	if !strings.HasPrefix(response.Header().Get("Content-Type"), "application/json") {
		t.Errorf("content type = %q, want JSON", response.Header().Get("Content-Type"))
	}
	if response.Body.Len() == 0 || response.Body.Len() > 4096 || !json.Valid(response.Body.Bytes()) {
		t.Errorf("503 response must contain bounded valid JSON, got %q", response.Body.String())
	}
	retry, err := strconv.Atoi(response.Header().Get("Retry-After"))
	if err != nil || retry < 1 {
		t.Errorf("Retry-After = %q, want positive seconds", response.Header().Get("Retry-After"))
	}
}

func admissionRelease(t *testing.T) (chan struct{}, func()) {
	t.Helper()
	ch := make(chan struct{})
	var once sync.Once
	release := func() { once.Do(func() { close(ch) }) }
	t.Cleanup(release)
	return ch, release
}

func TestImageGateOneActiveOneWaiting(t *testing.T) {
	started := make(chan string, 3)
	block, release := admissionRelease(t)
	handler := newImageGate(time.Minute).Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started <- r.Header.Get("X-Test-Request")
		<-block
		w.WriteHeader(http.StatusNoContent)
	}))
	results := make(chan admissionResult, 3)
	go admissionRequest(handler, "active", "/resize", context.Background(), nil, results)
	if id := admissionStarted(t, started); id != "active" {
		t.Fatalf("first handler = %q", id)
	}
	go admissionRequest(handler, "candidate-a", "/fit", context.Background(), nil, results)
	go admissionRequest(handler, "candidate-b", "/resize", context.Background(), nil, results)

	rejected := admissionReceive(t, results)
	admissionCheckUnavailable(t, rejected.response)
	select {
	case id := <-started:
		t.Fatalf("handler %q started while active handler held the slot", id)
	default:
	}
	release()
	waiting := admissionStarted(t, started)
	if waiting == rejected.id || waiting == "active" {
		t.Fatalf("unexpected waiting handler %q; rejected %q", waiting, rejected.id)
	}
	for i := 0; i < 2; i++ {
		result := admissionReceive(t, results)
		if result.response.Code != http.StatusNoContent {
			t.Errorf("%s status = %d, want 204", result.id, result.response.Code)
		}
	}
}

func TestImageGateCancelledWaiterNeverStarts(t *testing.T) {
	started := make(chan string, 3)
	block, release := admissionRelease(t)
	handler := newImageGate(time.Minute).Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started <- r.Header.Get("X-Test-Request")
		if r.Header.Get("X-Test-Request") == "active" {
			<-block
		}
	}))
	results := make(chan admissionResult, 4)
	go admissionRequest(handler, "active", "/resize", context.Background(), nil, results)
	admissionStarted(t, started)
	ctxA, cancelA := context.WithCancel(context.Background())
	ctxB, cancelB := context.WithCancel(context.Background())
	defer cancelA()
	defer cancelB()
	go admissionRequest(handler, "a", "/resize", ctxA, nil, results)
	go admissionRequest(handler, "b", "/fit", ctxB, nil, results)
	rejected := admissionReceive(t, results)
	admissionCheckUnavailable(t, rejected.response)
	cancelA()
	cancelB()
	cancelled := admissionReceive(t, results)
	if cancelled.id == rejected.id || cancelled.id == "active" {
		t.Fatalf("unexpected completed request %q", cancelled.id)
	}
	release()
	admissionReceive(t, results)
	go admissionRequest(handler, "next", "/resize", context.Background(), nil, results)
	if id := admissionStarted(t, started); id != "next" {
		t.Fatalf("cancelled waiter entered handler: %q", id)
	}
	admissionReceive(t, results)
}

func TestImageGateWaitTimeout(t *testing.T) {
	started := make(chan string, 2)
	block, release := admissionRelease(t)
	handler := newImageGate(50 * time.Millisecond).Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started <- r.Header.Get("X-Test-Request")
		if r.Header.Get("X-Test-Request") == "active" {
			<-block
		}
	}))
	results := make(chan admissionResult, 2)
	go admissionRequest(handler, "active", "/resize", context.Background(), nil, results)
	admissionStarted(t, started)
	go admissionRequest(handler, "waiting", "/resize", context.Background(), nil, results)
	timedOut := admissionReceive(t, results)
	if timedOut.id != "waiting" {
		t.Fatalf("completed %q, want waiting", timedOut.id)
	}
	admissionCheckUnavailable(t, timedOut.response)
	release()
	admissionReceive(t, results)
	select {
	case id := <-started:
		t.Fatalf("timed-out handler %q started", id)
	default:
	}
}

func TestImageGateDiscardsCancelledResponse(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	started := make(chan struct{})
	continueWrite := make(chan struct{})
	finished := make(chan struct{})
	var writeErr error
	handler := newImageGate(time.Second).Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-continueWrite
		w.WriteHeader(http.StatusCreated)
		_, writeErr = w.Write([]byte("abandoned"))
	}))
	response := httptest.NewRecorder()
	go func() {
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodPost, "/resize", nil).WithContext(ctx))
		close(finished)
	}()
	select {
	case <-started:
	case <-time.After(admissionTestDeadline):
		t.Fatal("handler did not start")
	}
	cancel()
	close(continueWrite)
	select {
	case <-finished:
	case <-time.After(admissionTestDeadline):
		t.Fatal("handler did not finish")
	}
	if writeErr != context.Canceled {
		t.Errorf("write error = %v, want context.Canceled", writeErr)
	}
	if response.Code == http.StatusCreated || response.Body.Len() != 0 {
		t.Errorf("cancelled response reached writer: status %d, body %q", response.Code, response.Body.String())
	}
}

func TestImageGateCleansMultipartAfterHandler(t *testing.T) {
	t.Setenv("TMPDIR", t.TempDir())
	var payload bytes.Buffer
	writer := multipart.NewWriter(&payload)
	part, err := writer.CreateFormFile("file", "image")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := part.Write([]byte("image")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/resize", &payload)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	if err := request.ParseMultipartForm(0); err != nil {
		t.Fatal(err)
	}
	files, err := os.ReadDir(os.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	var multipartFiles []string
	for _, file := range files {
		if strings.HasPrefix(file.Name(), "multipart-") {
			multipartFiles = append(multipartFiles, file.Name())
		}
	}
	t.Cleanup(func() { _ = request.MultipartForm.RemoveAll() })
	if len(multipartFiles) == 0 {
		t.Fatal("multipart request did not create a temporary file")
	}
	gate := newImageGate(time.Second)
	gate.Wrap(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {})).ServeHTTP(httptest.NewRecorder(), request)
	for _, name := range multipartFiles {
		if _, err := os.Stat(os.TempDir() + string(os.PathSeparator) + name); !os.IsNotExist(err) {
			t.Errorf("multipart temporary file %q remains after handler: %v", name, err)
		}
	}
}

func TestImageGateCancelledContextNeverStarts(t *testing.T) {
	for _, tc := range []struct {
		name    string
		context func() (context.Context, context.CancelFunc)
	}{
		{"cancelled", func() (context.Context, context.CancelFunc) {
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			return ctx, cancel
		}},
		{"expired", func() (context.Context, context.CancelFunc) {
			return context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := tc.context()
			defer cancel()
			called := false
			handler := newImageGate(time.Second).Wrap(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
				called = true
			}))
			results := make(chan admissionResult, 1)
			admissionRequest(handler, "cancelled", "/resize", ctx, nil, results)
			if called {
				t.Fatal("handler started with a finished context")
			}
		})
	}
}

func TestImageGateActiveCancellationKeepsSlot(t *testing.T) {
	started := make(chan string, 3)
	block, release := admissionRelease(t)
	handler := newImageGate(time.Minute).Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started <- r.Header.Get("X-Test-Request")
		if r.Header.Get("X-Test-Request") == "active" {
			<-block
		}
	}))
	results := make(chan admissionResult, 3)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go admissionRequest(handler, "active", "/resize", ctx, nil, results)
	admissionStarted(t, started)
	cancel()
	go admissionRequest(handler, "a", "/resize", context.Background(), nil, results)
	go admissionRequest(handler, "b", "/fit", context.Background(), nil, results)
	rejected := admissionReceive(t, results)
	admissionCheckUnavailable(t, rejected.response)
	select {
	case id := <-started:
		t.Fatalf("handler %q started before cancelled owner returned", id)
	default:
	}
	release()
	if id := admissionStarted(t, started); id == rejected.id || id == "active" {
		t.Fatalf("unexpected waiting handler %q", id)
	}
	admissionReceive(t, results)
	admissionReceive(t, results)
}

type admissionBody struct {
	read  chan struct{}
	block <-chan struct{}
	once  sync.Once
}

func (b *admissionBody) Read(p []byte) (int, error) {
	b.once.Do(func() { close(b.read) })
	if b.block != nil {
		<-b.block
	}
	return 0, io.EOF
}

func (b *admissionBody) Close() error { return nil }

func TestImageAdmissionBeforeBodyReadAndPublicRoutes(t *testing.T) {
	block, release := admissionRelease(t)
	LoadSources(ServerOptions{})
	mux := NewServerMux(ServerOptions{})
	active := &admissionBody{read: make(chan struct{}), block: block}
	bodyA := &admissionBody{read: make(chan struct{})}
	bodyB := &admissionBody{read: make(chan struct{})}
	results := make(chan admissionResult, 3)
	go admissionRequest(mux, "active", "/resize", context.Background(), active, results)
	select {
	case <-active.read:
	case <-time.After(admissionTestDeadline):
		t.Fatal("active image body was not read")
	}
	go admissionRequest(mux, "a", "/fit", context.Background(), bodyA, results)
	go admissionRequest(mux, "b", "/resize", context.Background(), bodyB, results)
	rejected := admissionReceive(t, results)
	admissionCheckUnavailable(t, rejected.response)
	for _, body := range []*admissionBody{bodyA, bodyB} {
		select {
		case <-body.read:
			t.Fatal("waiting or rejected image body was read before admission")
		default:
		}
	}
	for _, path := range []string{"/", "/health"} {
		publicResult := make(chan int, 1)
		go func(path string) {
			response := httptest.NewRecorder()
			mux.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
			publicResult <- response.Code
		}(path)
		select {
		case status := <-publicResult:
			if status != http.StatusOK {
				t.Errorf("%s status = %d, want 200 while image slot is occupied", path, status)
			}
		case <-time.After(admissionTestDeadline):
			t.Fatalf("%s blocked while image slot was occupied", path)
		}
	}
	release()
	admissionReceive(t, results)
	admissionReceive(t, results)
}
