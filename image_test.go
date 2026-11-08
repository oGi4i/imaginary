package main

import (
	"bytes"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"io/ioutil"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

func TestWebPInputToPNG(t *testing.T) {
	buf, err := os.ReadFile("testdata/test.webp")
	if err != nil {
		t.Fatal("fixture read failed")
	}
	if err := validateRasterSource(buf, defaultMaxSourcePixels); err != nil {
		t.Fatal("source admission failed")
	}

	checkPNG := func(t *testing.T, body []byte, width, height int) {
		t.Helper()
		config, err := png.DecodeConfig(bytes.NewReader(body))
		if err != nil {
			t.Fatal("PNG header decode failed")
		}
		if config.Width != width || config.Height != height {
			t.Fatalf("PNG dimensions = %dx%d, want %dx%d", config.Width, config.Height, width, height)
		}
		decoded, err := png.Decode(bytes.NewReader(body))
		if err != nil {
			t.Fatal("PNG full decode failed")
		}
		if decoded.Bounds().Dx() != width || decoded.Bounds().Dy() != height {
			t.Fatal("PNG decoded dimensions mismatch")
		}
	}

	// Conversion without resizing distinguishes input decoding from resize failures.
	t.Run("native-convert", func(t *testing.T) {
		img, err := Convert(buf, ImageOptions{Type: "png"})
		if err != nil {
			t.Fatal("native convert failed")
		}
		checkPNG(t, img.Body, 550, 368)
	})

	for _, tc := range []struct {
		name string
		size int
	}{
		{"native-resize-1x1", 1},
		{"native-resize-16x16", 16},
		{"native-resize-300x300", 300},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// Match the HTTP request: force and nocrop are not specified.
			img, err := Resize(buf, ImageOptions{Width: tc.size, Height: tc.size, Type: "png"})
			if err != nil {
				t.Fatal("native resize failed")
			}
			checkPNG(t, img.Body, tc.size, tc.size)
		})
	}

	t.Run("http-resize-1x1", func(t *testing.T) {
		o := ServerOptions{}
		LoadSources(o)
		req := httptest.NewRequest(http.MethodPost, "/resize?width=1&height=1&type=png", bytes.NewReader(buf))
		req.Header.Set("Content-Type", "application/octet-stream")
		response := httptest.NewRecorder()
		imageController(o, Resize)(response, req)
		if response.Code != http.StatusOK {
			t.Fatalf("HTTP resize status = %d, want 200", response.Code)
		}
		if response.Header().Get("Content-Type") != "image/png" {
			t.Fatal("HTTP resize content type is not PNG")
		}
		checkPNG(t, response.Body.Bytes(), 1, 1)
	})
}

func syntheticWebP(t *testing.T, width, height int) []byte {
	t.Helper()
	var buf bytes.Buffer
	source := image.NewRGBA(image.Rect(0, 0, width, height))
	draw.Draw(source, source.Bounds(), &image.Uniform{C: color.RGBA{R: 240, G: 220, B: 30, A: 255}}, image.Point{}, draw.Src)
	draw.Draw(source, image.Rect(0, 0, (width+1)/2, (height+1)/2), &image.Uniform{C: color.RGBA{R: 20, G: 40, B: 220, A: 255}}, image.Point{}, draw.Src)
	if err := png.Encode(&buf, source); err != nil {
		t.Fatal("synthetic PNG encode failed")
	}
	if err := validateRasterSource(buf.Bytes(), defaultMaxSourcePixels); err != nil {
		t.Fatal("synthetic PNG admission failed")
	}
	img, err := Convert(buf.Bytes(), ImageOptions{Type: "webp"})
	if err != nil {
		t.Fatal("synthetic WebP conversion failed")
	}
	if err := validateRasterSource(img.Body, defaultMaxSourcePixels); err != nil {
		t.Fatal("synthetic WebP admission failed")
	}
	return img.Body
}

func checkWebPResizePNG(t *testing.T, buf []byte, width, height int) {
	t.Helper()
	// Resize sets Embed=true; Force remains false, as with default HTTP options.
	img, err := Resize(buf, ImageOptions{Width: width, Height: height, Type: "png"})
	if err != nil {
		t.Fatal("native WebP resize failed")
	}
	decoded, err := png.Decode(bytes.NewReader(img.Body))
	if err != nil {
		t.Fatal("PNG full decode failed")
	}
	if decoded.Bounds().Dx() != width || decoded.Bounds().Dy() != height {
		t.Fatalf("PNG dimensions = %dx%d, want %dx%d", decoded.Bounds().Dx(), decoded.Bounds().Dy(), width, height)
	}
}

func TestSyntheticWebPResizePreservesContent(t *testing.T) {
	buf := syntheticWebP(t, 512, 512)
	img, err := Resize(buf, ImageOptions{Width: 16, Height: 16, Type: "png"})
	if err != nil {
		t.Fatal("native WebP resize failed")
	}
	decoded, err := png.Decode(bytes.NewReader(img.Body))
	if err != nil {
		t.Fatal("PNG full decode failed")
	}
	if decoded.Bounds().Dx() != 16 || decoded.Bounds().Dy() != 16 {
		t.Fatal("PNG decoded dimensions mismatch")
	}
	blueR, _, blueB, _ := decoded.At(2, 2).RGBA()
	yellowR, _, yellowB, _ := decoded.At(13, 13).RGBA()
	if blueB <= blueR+10000 || yellowR <= yellowB+10000 {
		t.Fatal("PNG content markers missing")
	}
}

func TestSyntheticWebPResizeToPNG(t *testing.T) {
	for _, tc := range []struct {
		name                      string
		sourceWidth, sourceHeight int
		width, height             int
	}{
		{"portrait-small", 513, 1025, 1, 1},
		{"portrait-rounded", 513, 1025, 17, 17},
		{"thin-vertical", 1, 256, 1, 16},
		{"thin-horizontal", 256, 1, 16, 1},
		{"thin-vertical-max-webp", 1, 16383, 1, 16},
		{"thin-horizontal-max-webp", 16383, 1, 16, 1},
		{"rounding-below", 257, 129, 15, 15},
		{"rounding-above", 257, 129, 16, 16},
		// For bicubic, floor(shrink * 3/4) crosses the 1024 preload limit.
		{"preload-1023", 1364, 1364, 1, 1},
		{"preload-1024", 1366, 1366, 1, 1},
		{"preload-1025", 1367, 1367, 1, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			buf := syntheticWebP(t, tc.sourceWidth, tc.sourceHeight)
			checkWebPResizePNG(t, buf, tc.width, tc.height)
		})
	}
}

func TestMalformedWebPThenResize(t *testing.T) {
	buf := syntheticWebP(t, 128, 128)
	malformed := buf[:12]
	if err := validateRasterSource(malformed, defaultMaxSourcePixels); err == nil {
		t.Fatal("malformed WebP passed admission")
	}
	if _, err := Resize(malformed, ImageOptions{Width: 16, Height: 16, Type: "png"}); err == nil {
		t.Fatal("malformed WebP resize succeeded")
	}
	checkWebPResizePNG(t, buf, 16, 16)
}

func TestImageResize(t *testing.T) {
	t.Run("Width and Height defined", func(t *testing.T) {
		opts := ImageOptions{Width: 300, Height: 300}
		buf, _ := ioutil.ReadAll(readFile("imaginary.jpg"))

		img, err := Resize(buf, opts)
		if err != nil {
			t.Errorf("Cannot process image: %s", err)
		}
		if img.Mime != "image/jpeg" {
			t.Error("Invalid image MIME type")
		}
		if assertSize(img.Body, opts.Width, opts.Height) != nil {
			t.Errorf("Invalid image size, expected: %dx%d", opts.Width, opts.Height)
		}
	})

	t.Run("Width defined", func(t *testing.T) {
		opts := ImageOptions{Width: 300}
		buf, _ := ioutil.ReadAll(readFile("imaginary.jpg"))

		img, err := Resize(buf, opts)
		if err != nil {
			t.Errorf("Cannot process image: %s", err)
		}
		if img.Mime != "image/jpeg" {
			t.Error("Invalid image MIME type")
		}
		if err := assertSize(img.Body, 300, 404); err != nil {
			t.Error(err)
		}
	})

	t.Run("Width defined with NoCrop=false", func(t *testing.T) {
		opts := ImageOptions{Width: 300, NoCrop: false, IsDefinedField: IsDefinedField{NoCrop: true}}
		buf, _ := ioutil.ReadAll(readFile("imaginary.jpg"))

		img, err := Resize(buf, opts)
		if err != nil {
			t.Errorf("Cannot process image: %s", err)
		}
		if img.Mime != "image/jpeg" {
			t.Error("Invalid image MIME type")
		}

		// The original image is 550x740
		if err := assertSize(img.Body, 300, 740); err != nil {
			t.Error(err)
		}
	})

	t.Run("Width defined with NoCrop=true", func(t *testing.T) {
		opts := ImageOptions{Width: 300, NoCrop: true, IsDefinedField: IsDefinedField{NoCrop: true}}
		buf, _ := ioutil.ReadAll(readFile("imaginary.jpg"))

		img, err := Resize(buf, opts)
		if err != nil {
			t.Errorf("Cannot process image: %s", err)
		}
		if img.Mime != "image/jpeg" {
			t.Error("Invalid image MIME type")
		}

		// The original image is 550x740
		if err := assertSize(img.Body, 300, 404); err != nil {
			t.Error(err)
		}
	})

}

func TestImageFit(t *testing.T) {
	opts := ImageOptions{Width: 300, Height: 300}
	buf, _ := ioutil.ReadAll(readFile("imaginary.jpg"))

	img, err := Fit(buf, opts)
	if err != nil {
		t.Errorf("Cannot process image: %s", err)
	}
	if img.Mime != "image/jpeg" {
		t.Error("Invalid image MIME type")
	}
	// 550x740 -> 222.9x300
	if assertSize(img.Body, 223, 300) != nil {
		t.Errorf("Invalid image size, expected: %dx%d", opts.Width, opts.Height)
	}
}

func TestImageAutoRotate(t *testing.T) {
	buf, _ := ioutil.ReadAll(readFile("imaginary.jpg"))
	img, err := AutoRotate(buf, ImageOptions{})
	if err != nil {
		t.Errorf("Cannot process image: %s", err)
	}
	if img.Mime != "image/jpeg" {
		t.Error("Invalid image MIME type")
	}
	if assertSize(img.Body, 550, 740) != nil {
		t.Errorf("Invalid image size, expected: %dx%d", 550, 740)
	}
}

func TestImagePipelineOperations(t *testing.T) {
	width, height := 300, 260

	operations := PipelineOperations{
		PipelineOperation{
			Name: "crop",
			Params: map[string]interface{}{
				"width":  width,
				"height": height,
			},
		},
		PipelineOperation{
			Name: "convert",
			Params: map[string]interface{}{
				"type": "webp",
			},
		},
	}

	opts := ImageOptions{Operations: operations}
	buf, _ := ioutil.ReadAll(readFile("imaginary.jpg"))

	img, err := Pipeline(buf, opts)
	if err != nil {
		t.Errorf("Cannot process image: %s", err)
	}
	if img.Mime != "image/webp" {
		t.Error("Invalid image MIME type")
	}
	if assertSize(img.Body, width, height) != nil {
		t.Errorf("Invalid image size, expected: %dx%d", width, height)
	}
}

func TestCalculateDestinationFitDimension(t *testing.T) {
	cases := []struct {
		// Image
		imageWidth  int
		imageHeight int

		// User parameter
		optionWidth  int
		optionHeight int

		// Expect
		fitWidth  int
		fitHeight int
	}{

		// Leading Width
		{1280, 1000, 710, 9999, 710, 555},
		{1279, 1000, 710, 9999, 710, 555},
		{900, 500, 312, 312, 312, 173}, // rounding down
		{900, 500, 313, 313, 313, 174}, // rounding up

		// Leading height
		{1299, 2000, 710, 999, 649, 999},
		{1500, 2000, 710, 999, 710, 947},
	}

	for _, tc := range cases {
		fitWidth, fitHeight := calculateDestinationFitDimension(tc.imageWidth, tc.imageHeight, tc.optionWidth, tc.optionHeight)
		if fitWidth != tc.fitWidth || fitHeight != tc.fitHeight {
			t.Errorf(
				"Fit dimensions calculation failure\nExpected : %d/%d (width/height)\nActual   : %d/%d (width/height)\n%+v",
				tc.fitWidth, tc.fitHeight, fitWidth, fitHeight, tc,
			)
		}
	}

}
