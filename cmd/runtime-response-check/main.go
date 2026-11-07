// Command runtime-response-check verifies the CI raster response as a full PNG.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"image/png"
	"io"
	"os"
)

const maxResponse = 1024 * 1024

func checkPNG(r io.Reader) error {
	data, err := io.ReadAll(io.LimitReader(r, maxResponse+1))
	if err != nil {
		return errors.New("cannot read response")
	}
	if len(data) > maxResponse {
		return errors.New("response exceeds size limit")
	}
	config, err := png.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return errors.New("invalid PNG response")
	}
	if config.Width != 1 || config.Height != 1 {
		return errors.New("unexpected image bounds")
	}
	img, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		return errors.New("invalid PNG response")
	}
	if bounds := img.Bounds(); bounds.Dx() != 1 || bounds.Dy() != 1 {
		return errors.New("unexpected image bounds")
	}
	return nil
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) != 3 || args[0] != "200" {
		return errors.New("expected HTTP 200, input format and response path")
	}
	if args[1] != "jpeg" && args[1] != "png" && args[1] != "webp" {
		return errors.New("unexpected input format")
	}
	f, err := os.Open(args[2])
	if err != nil {
		return errors.New("cannot open response")
	}
	defer f.Close()
	if err := checkPNG(f); err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(map[string]interface{}{
		"input": args[1], "status": 200, "output": "png", "width": 1, "height": 1,
	})
}
