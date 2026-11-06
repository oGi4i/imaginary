package main

import (
	"context"
	"errors"
	"io"
	"io/ioutil"
	"net/http"
)

// This default is provisional, not a qualified memory envelope.
const defaultMaxBodyBytes int64 = 16 << 20

var ErrMaxBodySize = NewError("Image source exceeds maximum body size", http.StatusRequestEntityTooLarge)

func maxBodyBytes(limit int64) int64 {
	if limit <= 0 {
		return defaultMaxBodyBytes
	}
	return limit
}

// Install this inside the image gate: waiting requests must not read their bodies.
func limitImageBody(next http.Handler, o ServerOptions) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Body != nil {
			r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes(o.MaxBodyBytes))
		}
		next.ServeHTTP(w, r)
	})
}

func bodyReadError(err error) error {
	var overflow *http.MaxBytesError
	if errors.As(err, &overflow) {
		return ErrMaxBodySize
	}
	return err
}

type sourceContextReader struct {
	ctx    context.Context
	reader io.Reader
}

func (r sourceContextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	n, err := r.reader.Read(p)
	if cancelled := r.ctx.Err(); cancelled != nil {
		return n, cancelled
	}
	return n, err
}

// MaxBytesReader probes at most one byte beyond the cap, even at MaxInt64.
// Discard partial data on every error so it cannot reach a decoder.
func readSourceBytes(ctx context.Context, reader io.Reader, limit int64) ([]byte, error) {
	bounded := http.MaxBytesReader(nil, io.NopCloser(sourceContextReader{ctx, reader}), maxBodyBytes(limit))
	buf, err := ioutil.ReadAll(bounded)
	if err != nil {
		return nil, bodyReadError(err)
	}
	return buf, nil
}
