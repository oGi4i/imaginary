package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"hash/crc32"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"testing"
)

// These fixtures exercise container parsing, not native image decoding.
func rasterTestJPEG(width, height uint16) []byte {
	return []byte{
		0xff, 0xd8,
		0xff, 0xc0, 0, 11, 8, byte(height >> 8), byte(height), byte(width >> 8), byte(width), 1, 1, 0x11, 0,
		0xff, 0xda, 0, 8, 1, 1, 0, 0, 63, 0,
		0x12, 0xff, 0x00, 0x34, 0xff, 0xd9,
	}
}

func rasterTestPNGChunk(kind string, data []byte) []byte {
	chunk := make([]byte, 12+len(data))
	binary.BigEndian.PutUint32(chunk, uint32(len(data)))
	copy(chunk[4:8], kind)
	copy(chunk[8:], data)
	binary.BigEndian.PutUint32(chunk[8+len(data):], crc32.ChecksumIEEE(chunk[4:8+len(data)]))
	return chunk
}

func rasterTestPNG(width, height uint32, extra ...[]byte) []byte {
	buf := []byte("\x89PNG\r\n\x1a\n")
	ihdr := make([]byte, 13)
	binary.BigEndian.PutUint32(ihdr, width)
	binary.BigEndian.PutUint32(ihdr[4:], height)
	ihdr[8], ihdr[9] = 8, 2
	buf = append(buf, rasterTestPNGChunk("IHDR", ihdr)...)
	for _, chunk := range extra {
		buf = append(buf, chunk...)
	}
	buf = append(buf, rasterTestPNGChunk("IDAT", []byte{0x78, 0x9c, 0x63, 0x60, 0x60, 0x60, 0, 0, 4, 0, 1})...)
	return append(buf, rasterTestPNGChunk("IEND", nil)...)
}

func rasterTestWebPChunk(kind string, data []byte) []byte {
	chunk := make([]byte, 8+len(data)+(len(data)&1))
	copy(chunk, kind)
	binary.LittleEndian.PutUint32(chunk[4:], uint32(len(data)))
	copy(chunk[8:], data)
	return chunk
}

func rasterTestWebP(chunks ...[]byte) []byte {
	buf := []byte("RIFF\x00\x00\x00\x00WEBP")
	for _, chunk := range chunks {
		buf = append(buf, chunk...)
	}
	binary.LittleEndian.PutUint32(buf[4:], uint32(len(buf)-8))
	return buf
}

func rasterTestVP8(width, height uint16) []byte {
	return rasterTestWebPChunk("VP8 ", []byte{0x10, 0, 0, 0x9d, 0x01, 0x2a, byte(width), byte(width >> 8), byte(height), byte(height >> 8)})
}

func rasterTestVP8L(width, height uint32) []byte {
	bits := (width - 1) | ((height - 1) << 14)
	data := make([]byte, 5)
	data[0] = 0x2f
	binary.LittleEndian.PutUint32(data[1:], bits)
	return rasterTestWebPChunk("VP8L", data)
}

func rasterTestVP8X(width, height uint32, flags byte) []byte {
	data := make([]byte, 10)
	data[0] = flags
	width--
	height--
	data[4], data[5], data[6] = byte(width), byte(width>>8), byte(width>>16)
	data[7], data[8], data[9] = byte(height), byte(height>>8), byte(height>>16)
	return rasterTestWebPChunk("VP8X", data)
}

func rasterTestStatus(t *testing.T, buf []byte, max int64, status int) {
	t.Helper()
	err := validateRasterSource(buf, max)
	if status == 0 {
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		return
	}
	var imageErr Error
	if !errors.As(err, &imageErr) {
		t.Fatalf("error = %v, want Error with HTTP status %d", err, status)
	}
	if imageErr.HTTPCode() != status {
		t.Fatalf("status = %d, want %d: %v", imageErr.HTTPCode(), status, err)
	}
}

func TestRasterSourcePixelLimits(t *testing.T) {
	fixtures := map[string][]byte{
		"jpeg":                   rasterTestJPEG(4, 3),
		"png":                    rasterTestPNG(4, 3),
		"webp-lossy":             rasterTestWebP(rasterTestVP8(4, 3)),
		"webp-lossless":          rasterTestWebP(rasterTestVP8L(4, 3)),
		"webp-extended":          rasterTestWebP(rasterTestVP8X(4, 3, 0), rasterTestVP8(4, 3)),
		"webp-odd-chunk-padding": rasterTestWebP(rasterTestWebPChunk("JUNK", []byte{1}), rasterTestVP8(4, 3)),
	}
	for name, buf := range fixtures {
		t.Run(name, func(t *testing.T) {
			rasterTestStatus(t, buf, 12, 0)
			rasterTestStatus(t, buf, 13, 0)
			rasterTestStatus(t, buf, 11, http.StatusRequestEntityTooLarge)
		})
	}
	// A 32-bit multiplication must not wrap into an accepted small image.
	rasterTestStatus(t, rasterTestPNG(65536, 65536), 16000000, http.StatusRequestEntityTooLarge)
	rasterTestStatus(t, rasterTestPNG(0x7fffffff, 0x7fffffff), 16000000, http.StatusRequestEntityTooLarge)
	rasterTestStatus(t, rasterTestWebP(rasterTestVP8L(1<<14, 1<<14)), 16000000, http.StatusRequestEntityTooLarge)
}

func TestRasterSourceRealFiles(t *testing.T) {
	for _, name := range []string{"imaginary.jpg", "test.png", "test.webp"} {
		t.Run(name, func(t *testing.T) {
			buf, err := os.ReadFile(filepath.Join("testdata", name))
			if err != nil {
				t.Fatal(err)
			}
			rasterTestStatus(t, buf, 16000000, 0)
		})
	}
}

func TestRasterSourceExcludedFormatsAndAnimation(t *testing.T) {
	cases := map[string][]byte{
		"pdf":                    []byte("%PDF-1.4\n%%EOF"),
		"svg":                    []byte("<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>"),
		"tiff-le":                []byte("II\x2a\x00\x08\x00\x00\x00"),
		"tiff-be":                []byte("MM\x00\x2a\x00\x00\x00\x08"),
		"heif":                   []byte("\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"),
		"gif":                    []byte("GIF89a\x01\x00\x01\x00\x00\x00\x00\x3b"),
		"jpeg-mpf":               append([]byte{0xff, 0xd8, 0xff, 0xe2, 0, 6, 'M', 'P', 'F', 0}, rasterTestJPEG(1, 1)[2:]...),
		"png-actl":               rasterTestPNG(1, 1, rasterTestPNGChunk("acTL", make([]byte, 8))),
		"png-fctl":               rasterTestPNG(1, 1, rasterTestPNGChunk("fcTL", make([]byte, 26))),
		"png-fdat":               rasterTestPNG(1, 1, rasterTestPNGChunk("fdAT", make([]byte, 4))),
		"webp-animation-flag":    rasterTestWebP(rasterTestVP8X(1, 1, 2), rasterTestVP8(1, 1)),
		"webp-anim":              rasterTestWebP(rasterTestVP8X(1, 1, 2), rasterTestWebPChunk("ANIM", make([]byte, 6)), rasterTestVP8(1, 1)),
		"webp-anmf":              rasterTestWebP(rasterTestVP8X(1, 1, 2), rasterTestWebPChunk("ANMF", make([]byte, 16))),
		"webp-anim-without-flag": rasterTestWebP(rasterTestVP8X(1, 1, 0), rasterTestWebPChunk("ANIM", make([]byte, 6)), rasterTestVP8(1, 1)),
		"webp-anmf-without-flag": rasterTestWebP(rasterTestVP8X(1, 1, 0), rasterTestWebPChunk("ANMF", make([]byte, 16)), rasterTestVP8(1, 1)),
	}
	for name, buf := range cases {
		t.Run(name, func(t *testing.T) { rasterTestStatus(t, buf, 16000000, http.StatusNotAcceptable) })
	}
}

func TestRasterSourceMalformedContainers(t *testing.T) {
	jpeg := rasterTestJPEG(4, 3)
	png := rasterTestPNG(4, 3)
	webp := rasterTestWebP(rasterTestVP8(4, 3))
	badPNGLen := bytes.Clone(png)
	binary.BigEndian.PutUint32(badPNGLen[8:], 0xffffffff)
	badWebPLen := bytes.Clone(webp)
	binary.LittleEndian.PutUint32(badWebPLen[16:], 0xffffffff)
	badRIFFLen := bytes.Clone(webp)
	binary.LittleEndian.PutUint32(badRIFFLen[4:], 0xffffffff)
	duplicateSOF := append(bytes.Clone(jpeg[:15]), jpeg[2:]...)
	cases := map[string][]byte{
		"jpeg-duplicate-sof":   duplicateSOF,
		"jpeg-no-sof":          append([]byte{0xff, 0xd8}, jpeg[15:]...),
		"jpeg-invalid-length":  []byte{0xff, 0xd8, 0xff, 0xe0, 0, 1, 0xff, 0xd9},
		"jpeg-zero-height":     rasterTestJPEG(1, 0),
		"jpeg-trailing":        append(bytes.Clone(jpeg), 0),
		"jpeg-zero-width":      rasterTestJPEG(0, 1),
		"png-zero-width":       rasterTestPNG(0, 1),
		"png-zero-height":      rasterTestPNG(1, 0),
		"png-chunk-overflow":   badPNGLen,
		"png-duplicate-ihdr":   rasterTestPNG(1, 1, rasterTestPNGChunk("IHDR", make([]byte, 13))),
		"png-iend-before-ihdr": append([]byte("\x89PNG\r\n\x1a\n"), rasterTestPNGChunk("IEND", nil)...),
		"png-trailing":         append(bytes.Clone(png), 0),
		"webp-chunk-overflow":  badWebPLen,
		"webp-riff-overflow":   badRIFFLen,
		"webp-trailing":        append(bytes.Clone(webp), 0),
		"webp-canvas-mismatch": rasterTestWebP(rasterTestVP8X(2, 1, 0), rasterTestVP8(1, 1)),
		"webp-missing-padding": rasterTestWebP([]byte{'J', 'U', 'N', 'K', 1, 0, 0, 0, 1}),
	}
	for name, buf := range cases {
		t.Run(name, func(t *testing.T) { rasterTestStatus(t, buf, 16000000, http.StatusBadRequest) })
	}
	for name, test := range map[string]struct {
		data      []byte
		signature int
	}{
		"jpeg": {jpeg, 2}, "png": {png, 8}, "webp": {webp, 12},
	} {
		t.Run(name+"-every-truncation", func(t *testing.T) {
			for n := test.signature; n < len(test.data); n++ {
				t.Run(strconv.Itoa(n), func(t *testing.T) {
					rasterTestStatus(t, test.data[:n], 16000000, http.StatusBadRequest)
				})
			}
		})
	}
}

func TestRasterSourceRejectsWatermarkImageBeforeFetch(t *testing.T) {
	var requests int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&requests, 1)
		_, _ = w.Write(rasterTestPNG(1, 1))
	}))
	defer server.Close()

	operations, err := json.Marshal([]map[string]interface{}{{
		"operation": "watermarkImage", "ignore_failure": true,
		"params": map[string]interface{}{"image": server.URL},
	}})
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name  string
		op    Operation
		query url.Values
	}{
		{"direct", WatermarkImage, url.Values{"image": {server.URL}}},
		{"pipeline", Pipeline, url.Values{"operations": {string(operations)}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			response := httptest.NewRecorder()
			request := httptest.NewRequest(http.MethodPost, "/"+tc.name+"?"+tc.query.Encode(), nil)
			imageHandler(response, request, rasterTestPNG(1, 1), tc.op, ServerOptions{EnablePlaceholder: true, PlaceholderImage: []byte("not an image"), PlaceholderStatus: 299})
			if response.Code != http.StatusNotAcceptable || response.Header().Get("Content-Type") != "application/json" {
				t.Fatalf("status = %d, type = %q, body = %s", response.Code, response.Header().Get("Content-Type"), response.Body.String())
			}
			if got := atomic.LoadInt32(&requests); got != 0 {
				t.Fatalf("watermark server received %d requests", got)
			}
		})
	}
}

func TestRasterSourceImageHandlerRejectsBeforeNative(t *testing.T) {
	cases := map[string]struct {
		buf    []byte
		max    int64
		status int
	}{
		"admitted":           {rasterTestPNG(1, 1), 100, http.StatusOK},
		"excluded":           {[]byte("GIF89a\x01\x00\x01\x00\x00\x00\x00\x3b"), 100, http.StatusNotAcceptable},
		"animation":          {rasterTestPNG(1, 1, rasterTestPNGChunk("acTL", make([]byte, 8))), 100, http.StatusNotAcceptable},
		"malformed":          {rasterTestPNG(0, 1), 100, http.StatusBadRequest},
		"over-limit":         {rasterTestPNG(11, 10), 100, http.StatusRequestEntityTooLarge},
		"default-at-limit":   {rasterTestPNG(4000, 4000), 0, http.StatusOK},
		"default-over-limit": {rasterTestPNG(4001, 4000), 0, http.StatusRequestEntityTooLarge},
	}
	for name, tc := range cases {
		for _, placeholder := range []bool{false, true} {
			t.Run(name+"/placeholder="+map[bool]string{false: "off", true: "on"}[placeholder], func(t *testing.T) {
				calls := 0
				op := Operation(func(_ []byte, _ ImageOptions) (Image, error) {
					calls++
					return Image{Body: []byte("operation called"), Mime: "image/png"}, nil
				})
				response := httptest.NewRecorder()
				request := httptest.NewRequest(http.MethodPost, "/resize?width=1&height=1", nil)
				o := ServerOptions{MaxSourcePixels: tc.max, EnablePlaceholder: placeholder, PlaceholderImage: []byte("not a native image"), PlaceholderStatus: 299}
				imageHandler(response, request, tc.buf, op, o)
				wantCalls := 0
				if tc.status == http.StatusOK {
					wantCalls = 1
				}
				if calls != wantCalls {
					t.Fatalf("operation called %d times, want %d", calls, wantCalls)
				}
				if response.Code != tc.status {
					t.Fatalf("status = %d, want %d; body %s", response.Code, tc.status, response.Body.String())
				}
				if tc.status != http.StatusOK {
					if got := response.Header().Get("Content-Type"); got != "application/json" {
						t.Fatalf("Content-Type = %q, want application/json", got)
					}
				}
			})
		}
	}
}
