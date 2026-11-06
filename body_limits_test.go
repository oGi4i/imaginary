package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// A valid single-frame PNG for requests that must reach the operation.
func bodyLimitPNG(t *testing.T) []byte {
	t.Helper()
	data, err := base64.StdEncoding.DecodeString("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=")
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func bodyLimitOptions(t *testing.T, limit int64) ServerOptions {
	t.Helper()
	return ServerOptions{MaxBodyBytes: limit}
}

func bodyLimitHandler(o ServerOptions, calls *int32) http.Handler {
	LoadSources(o)
	return ImageMiddleware(o)(func(data []byte, _ ImageOptions) (Image, error) {
		atomic.AddInt32(calls, 1)
		return Image{Body: data, Mime: "image/png"}, nil
	})
}

func bodyLimitCheck413(t *testing.T, response *httptest.ResponseRecorder) string {
	t.Helper()
	if response.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("status = %d, want 413; body %q", response.Code, response.Body.String())
	}
	if !strings.HasPrefix(response.Header().Get("Content-Type"), "application/json") {
		t.Errorf("content type = %q, want JSON", response.Header().Get("Content-Type"))
	}
	if response.Body.Len() == 0 || response.Body.Len() > 4096 || !json.Valid(response.Body.Bytes()) {
		t.Errorf("413 must contain bounded valid JSON, got %q", response.Body.String())
	}
	return response.Body.String()
}

type bodyLimitCountingReader struct {
	reader io.Reader
	bytes  int64
	reads  int32
}

func (r *bodyLimitCountingReader) Read(p []byte) (int, error) {
	atomic.AddInt32(&r.reads, 1)
	n, err := r.reader.Read(p)
	atomic.AddInt64(&r.bytes, int64(n))
	return n, err
}

func TestBodyLimitRawPOSTBoundaries(t *testing.T) {
	png := bodyLimitPNG(t)
	limit := int64(len(png))
	for _, framing := range []string{"known", "unknown", "chunked", "misleading-small"} {
		for _, delta := range []int64{-1, 0, 1, 1024} {
			t.Run(fmt.Sprintf("%s/%+d", framing, delta), func(t *testing.T) {
				var calls int32
				payload := png
				if delta > 0 {
					payload = append(append([]byte(nil), png...), bytes.Repeat([]byte{0}, int(delta))...)
				}
				capBytes := limit
				if delta == -1 {
					capBytes++
				}
				o := bodyLimitOptions(t, capBytes)
				handler := bodyLimitHandler(o, &calls)
				reader := &bodyLimitCountingReader{reader: bytes.NewReader(payload)}
				req := httptest.NewRequest(http.MethodPost, "/resize", reader)
				switch framing {
				case "known":
					req.ContentLength = int64(len(payload))
				case "chunked":
					req.ContentLength = -1
					req.TransferEncoding = []string{"chunked"}
				case "misleading-small":
					req.ContentLength = 1
				default:
					req.ContentLength = -1
				}
				response := httptest.NewRecorder()
				handler.ServeHTTP(response, req)
				if delta > 0 {
					bodyLimitCheck413(t, response)
					if calls != 0 {
						t.Errorf("oversized body reached operation %d times", calls)
					}
				} else if response.Code != http.StatusOK || calls != 1 {
					t.Errorf("status = %d, operation calls = %d; want 200 and 1", response.Code, calls)
				}
				if reader.bytes > capBytes+1 {
					t.Errorf("read %d bytes, want at most %d", reader.bytes, capBytes+1)
				}
			})
		}
	}
}

func TestBodyLimitDefaultAndPlaceholder(t *testing.T) {
	const limit = int64(16 << 20) // provisional, not a qualification result
	var calls int32
	o := ServerOptions{EnablePlaceholder: true, PlaceholderStatus: http.StatusOK}
	handler := bodyLimitHandler(o, &calls)
	var fixed string
	for _, fill := range []byte{'a', 'b'} {
		reader := &bodyLimitCountingReader{reader: io.MultiReader(bytes.NewReader(bodyLimitPNG(t)), io.LimitReader(bytes.NewReader(bytes.Repeat([]byte{fill}, int(limit+1024))), limit+1024))}
		req := httptest.NewRequest(http.MethodPost, "/resize?width=1&height=1", reader)
		req.ContentLength = -1
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, req)
		got := bodyLimitCheck413(t, response)
		if fixed != "" && got != fixed {
			t.Errorf("413 response changes with request data: %q != %q", got, fixed)
		}
		fixed = got
		if reader.bytes > limit+1 {
			t.Errorf("read %d bytes, want at most %d", reader.bytes, limit+1)
		}
	}
	if calls != 0 {
		t.Errorf("oversized body reached operation %d times", calls)
	}
}

type bodyLimitErrorReader struct {
	prefix  []byte
	err     error
	onError func()
}

func (r *bodyLimitErrorReader) Read(p []byte) (int, error) {
	if len(r.prefix) > 0 {
		n := copy(p, r.prefix)
		r.prefix = r.prefix[n:]
		return n, nil
	}
	if r.onError != nil {
		r.onError()
	}
	return 0, r.err
}

func TestBodyLimitReadErrorAndCancellation(t *testing.T) {
	for _, cancelled := range []bool{false, true} {
		t.Run(fmt.Sprintf("cancelled=%t", cancelled), func(t *testing.T) {
			var calls int32
			o := bodyLimitOptions(t, 1024)
			handler := bodyLimitHandler(o, &calls)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			failure := errors.New("read failure")
			reader := &bodyLimitErrorReader{prefix: bodyLimitPNG(t), err: failure}
			if cancelled {
				reader.err = context.Canceled
				reader.onError = cancel
			}
			req := httptest.NewRequest(http.MethodPost, "/resize", reader).WithContext(ctx)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, req)
			if !cancelled && response.Code != http.StatusBadRequest {
				t.Errorf("read error status = %d, want 400", response.Code)
			}
			if calls != 0 {
				t.Errorf("failed or cancelled read reached operation %d times", calls)
			}
			next := httptest.NewRecorder()
			handler.ServeHTTP(next, httptest.NewRequest(http.MethodPost, "/resize", bytes.NewReader(bodyLimitPNG(t))))
			if next.Code != http.StatusOK || calls != 1 {
				t.Errorf("next request status = %d, operation calls = %d; want 200 and 1", next.Code, calls)
			}
		})
	}
}

func bodyLimitMultipart(t *testing.T, png []byte) ([]byte, string) {
	t.Helper()
	var data bytes.Buffer
	writer := multipart.NewWriter(&data)
	part, err := writer.CreateFormFile("file", "image.png")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := part.Write(png); err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteField("extra", strings.Repeat("x", 512)); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return data.Bytes(), writer.FormDataContentType()
}

func TestBodyLimitMultipartWholeBodyAndCleanup(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("TMPDIR", tmp)
	payload, contentType := bodyLimitMultipart(t, bodyLimitPNG(t))
	for _, delta := range []int64{-1, 0, 1} {
		t.Run(fmt.Sprintf("%+d", delta), func(t *testing.T) {
			var calls int32
			// File fits in every case; the cap applies to boundaries and extra fields too.
			limit := int64(len(payload)) - delta
			o := bodyLimitOptions(t, limit)
			handler := bodyLimitHandler(o, &calls)
			req := httptest.NewRequest(http.MethodPost, "/resize", bytes.NewReader(payload))
			req.ContentLength = -1
			req.Header.Set("Content-Type", contentType)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, req)
			if delta > 0 {
				bodyLimitCheck413(t, response)
				if calls != 0 {
					t.Errorf("oversized multipart reached operation %d times", calls)
				}
			} else if response.Code != http.StatusOK || calls != 1 {
				t.Errorf("status = %d, operation calls = %d; want 200 and 1", response.Code, calls)
			}
			if req.MultipartForm != nil {
				t.Cleanup(func() { _ = req.MultipartForm.RemoveAll() })
			}
			entries, err := os.ReadDir(tmp)
			if err != nil {
				t.Fatal(err)
			}
			if len(entries) != 0 {
				t.Errorf("multipart temporary files remain: %v", entries)
			}
		})
	}
}

func TestBodyLimitReaderBoundariesAndFailures(t *testing.T) {
	const limit = int64(8)
	for _, size := range []int{0, 7, 8, 9, 1024} {
		t.Run(fmt.Sprintf("size=%d", size), func(t *testing.T) {
			payload := bytes.Repeat([]byte{'x'}, size)
			reader := &bodyLimitCountingReader{reader: bytes.NewReader(payload)}
			got, err := readSourceBytes(context.Background(), reader, limit)
			if size > int(limit) {
				if err != ErrMaxBodySize || got != nil {
					t.Fatalf("got %q, %v; want nil, ErrMaxBodySize", got, err)
				}
			} else if err != nil || !bytes.Equal(got, payload) {
				t.Fatalf("got %q, %v; want %q, nil", got, err, payload)
			}
			if reader.bytes > limit+1 {
				t.Fatalf("read %d bytes, want at most %d", reader.bytes, limit+1)
			}
		})
	}
	failure := errors.New("read failure")
	got, err := readSourceBytes(context.Background(), &bodyLimitErrorReader{prefix: []byte("partial"), err: failure}, limit)
	if got != nil || !errors.Is(err, failure) {
		t.Fatalf("read failure = %q, %v; want nil and original error", got, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	reader := &bodyLimitCountingReader{reader: strings.NewReader("ignored")}
	got, err = readSourceBytes(ctx, reader, limit)
	if got != nil || !errors.Is(err, context.Canceled) || reader.reads != 0 {
		t.Fatalf("cancelled read = %q, %v; reads = %d", got, err, reader.reads)
	}
	if maxBodyBytes(0) != 16<<20 || maxBodyBytes(-1) != 16<<20 {
		t.Fatal("nonpositive cap must use the provisional 16 MiB default")
	}
	got, err = readSourceBytes(context.Background(), strings.NewReader("small"), int64(1<<63-1))
	if err != nil || string(got) != "small" {
		t.Fatalf("MaxInt64 cap overflow: %q, %v", got, err)
	}
}

func TestBodyLimitDirectSources(t *testing.T) {
	for _, kind := range []string{"raw", "multipart", "http", "fs"} {
		for _, delta := range []int{-1, 0, 1, 1024} {
			t.Run(fmt.Sprintf("%s/%+d", kind, delta), func(t *testing.T) {
				payload := []byte("12345678")
				if delta > 0 {
					payload = append(payload, bytes.Repeat([]byte{'x'}, delta)...)
				}
				limit := int64(8)
				if delta < 0 {
					limit++
				}
				config := &SourceConfig{MaxBodyBytes: limit}
				var source ImageSource
				var request *http.Request
				switch kind {
				case "raw":
					source = NewBodyImageSource(config)
					request = httptest.NewRequest(http.MethodPost, "/resize", bytes.NewReader(payload))
				case "multipart":
					body, contentType := bodyLimitMultipart(t, payload)
					config.MaxBodyBytes = int64(len(body) - delta)
					source = NewBodyImageSource(config)
					request = httptest.NewRequest(http.MethodPost, "/resize", bytes.NewReader(body))
					request.Header.Set("Content-Type", contentType)
				case "http":
					server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						// Force chunked transfer instead of the automatic Content-Length.
						w.(http.Flusher).Flush()
						_, _ = w.Write(payload)
					}))
					defer server.Close()
					source = NewHTTPImageSource(config)
					request = httptest.NewRequest(http.MethodGet, "/resize?url="+url.QueryEscape(server.URL), nil)
				case "fs":
					config.MountPath = t.TempDir()
					if err := os.WriteFile(filepath.Join(config.MountPath, "image"), payload, 0600); err != nil {
						t.Fatal(err)
					}
					source = NewFileSystemImageSource(config)
					request = httptest.NewRequest(http.MethodGet, "/resize?file=image", nil)
				}
				got, err := source.GetImage(request)
				if delta > 0 {
					if err != ErrMaxBodySize || len(got) != 0 {
						t.Fatalf("oversized source = %d bytes, %v; want no bytes and ErrMaxBodySize", len(got), err)
					}
				} else if err != nil || !bytes.Equal(got, payload) {
					t.Fatalf("source = %q, %v; want %q, nil", got, err, payload)
				}
			})
		}
	}
}

func TestBodyLimitMultipartEpilogue(t *testing.T) {
	payload, contentType := bodyLimitMultipart(t, bodyLimitPNG(t))
	limit := int64(len(payload))
	payload = append(payload, bytes.Repeat([]byte{'x'}, 1024)...)
	var calls int32
	handler := bodyLimitHandler(bodyLimitOptions(t, limit), &calls)
	reader := &bodyLimitCountingReader{reader: bytes.NewReader(payload)}
	request := httptest.NewRequest(http.MethodPost, "/resize", reader)
	request.ContentLength = -1
	request.Header.Set("Content-Type", contentType)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	bodyLimitCheck413(t, response)
	if calls != 0 || reader.bytes > limit+1 {
		t.Fatalf("operation calls = %d, read bytes = %d; want 0 and at most %d", calls, reader.bytes, limit+1)
	}
}

func TestBodyLimitRemoteAndFileRejection(t *testing.T) {
	png := bodyLimitPNG(t)
	payload := append(append([]byte(nil), png...), 0)
	for _, kind := range []string{"http", "fs"} {
		t.Run(kind, func(t *testing.T) {
			var calls int32
			o := bodyLimitOptions(t, int64(len(png)))
			o.EnablePlaceholder = true
			o.PlaceholderStatus = http.StatusOK
			path := "/resize"
			if kind == "http" {
				o.EnableURLSource = true
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					_, _ = w.Write(payload)
				}))
				defer server.Close()
				path += "?url=" + url.QueryEscape(server.URL)
			} else {
				o.Mount = t.TempDir()
				if err := os.WriteFile(filepath.Join(o.Mount, "image"), payload, 0600); err != nil {
					t.Fatal(err)
				}
				path += "?file=image"
			}
			handler := bodyLimitHandler(o, &calls)
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
			bodyLimitCheck413(t, response)
			if calls != 0 {
				t.Fatalf("oversized %s source reached operation %d times", kind, calls)
			}
		})
	}
}

func TestBodyLimitMultipartTemporaryCleanup(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("TMPDIR", tmp)
	payload, contentType := bodyLimitMultipart(t, bodyLimitPNG(t))
	request := httptest.NewRequest(http.MethodPost, "/resize", bytes.NewReader(payload))
	request.Header.Set("Content-Type", contentType)
	// Force a real disk-backed form at tiny sizes before exercising source cleanup.
	if err := request.ParseMultipartForm(1); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = request.MultipartForm.RemoveAll() })
	entries, err := os.ReadDir(tmp)
	if err != nil || len(entries) == 0 {
		t.Fatalf("multipart did not create a temporary file: %v, %v", entries, err)
	}
	source := NewBodyImageSource(&SourceConfig{MaxBodyBytes: int64(len(payload))})
	if _, err := source.GetImage(request); err != nil {
		t.Fatal(err)
	}
	entries, err = os.ReadDir(tmp)
	if err != nil || len(entries) != 0 {
		t.Fatalf("multipart cleanup left files: %v, %v", entries, err)
	}
}

func TestBodyLimitQueuedBodiesRemainUnread(t *testing.T) {
	var calls int32
	o := bodyLimitOptions(t, 1024)
	LoadSources(o)
	started := make(chan struct{})
	block, release := admissionRelease(t)
	handler := ImageMiddleware(o)(func(data []byte, _ ImageOptions) (Image, error) {
		atomic.AddInt32(&calls, 1)
		close(started)
		<-block
		return Image{Body: data, Mime: "image/png"}, nil
	})
	results := make(chan admissionResult, 3)
	go admissionRequest(handler, "active", "/resize", context.Background(), bytes.NewReader(bodyLimitPNG(t)), results)
	select {
	case <-started:
	case <-time.After(admissionTestDeadline):
		t.Fatal("active operation did not start")
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	readers := []*bodyLimitCountingReader{
		{reader: bytes.NewReader(bodyLimitPNG(t))},
		{reader: bytes.NewReader(bodyLimitPNG(t))},
	}
	go admissionRequest(handler, "a", "/resize", ctx, readers[0], results)
	go admissionRequest(handler, "b", "/resize", ctx, readers[1], results)
	rejected := admissionReceive(t, results)
	admissionCheckUnavailable(t, rejected.response)
	for _, reader := range readers {
		if got := atomic.LoadInt32(&reader.reads); got != 0 {
			t.Errorf("waiting or rejected body read %d times", got)
		}
	}
	cancel()
	admissionReceive(t, results)
	release()
	admissionReceive(t, results)
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Errorf("operation calls = %d, want 1", got)
	}
}
