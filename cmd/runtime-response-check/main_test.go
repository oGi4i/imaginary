package main

import (
	"bytes"
	"image"
	"image/png"
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
	tests := []struct {
		name string
		data []byte
		ok   bool
	}{
		{"valid", valid.Bytes(), true},
		{"truncated", valid.Bytes()[:29], false},
		{"wrong dimensions", wrong.Bytes(), false},
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
