package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"image"
	"image/png"
	"io"
	"strings"
	"testing"
)

func TestCheckPNG(t *testing.T) {
	var valid, wrong bytes.Buffer
	if err := png.Encode(&valid, image.NewRGBA(image.Rect(0, 0, 1, 1))); err != nil {
		t.Fatal(err)
	}
	if err := png.Encode(&wrong, image.NewRGBA(image.Rect(0, 0, 2, 1))); err != nil {
		t.Fatal(err)
	}
	huge := append([]byte(nil), valid.Bytes()...)
	binary.BigEndian.PutUint32(huge[16:20], 100000)
	binary.BigEndian.PutUint32(huge[20:24], 100000)
	binary.BigEndian.PutUint32(huge[29:33], crc32.ChecksumIEEE(huge[12:29]))
	tests := []struct {
		name string
		data []byte
		ok   bool
	}{
		{"valid", valid.Bytes(), true},
		{"truncated", valid.Bytes()[:29], false},
		{"wrong dimensions", wrong.Bytes(), false},
		{"huge dimensions", huge, false},
		{"oversized", bytes.Repeat([]byte{0}, maxResponse+1), false},
		{"invalid", []byte("not an image"), false},
		{"empty", nil, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if err := checkPNG(bytes.NewReader(tt.data)); (err == nil) != tt.ok {
				t.Fatalf("checkPNG() error = %v, want success %v", err, tt.ok)
			}
		})
	}
}

type failingReader struct{}

func (failingReader) Read([]byte) (int, error) {
	return 0, errors.New("private reader detail")
}

func TestErrorsDoNotExposePrivateDetails(t *testing.T) {
	if err := checkPNG(failingReader{}); err == nil || strings.Contains(err.Error(), "private") {
		t.Fatalf("unexpected read error: %v", err)
	}
	if err := run([]string{"200", "png", "/private/nonexistent/response.png"}); err == nil || strings.Contains(err.Error(), "/private/") {
		t.Fatalf("unexpected open error: %v", err)
	}
	if err := checkPNG(io.LimitReader(bytes.NewReader([]byte("not a PNG")), 8)); err == nil || err.Error() != "invalid PNG response" {
		t.Fatalf("unexpected decode error: %v", err)
	}
}
