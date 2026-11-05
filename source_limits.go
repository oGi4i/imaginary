package main

import (
	"bytes"
	"encoding/binary"
	"net/http"
)

// defaultMaxSourcePixels is provisional and has not been qualified for deployment.
const defaultMaxSourcePixels int64 = 16000000

var (
	errMalformedRaster   = NewError("Malformed or unrecognized image source", http.StatusBadRequest)
	errExcludedRaster    = NewError("Unsupported image source or animation", http.StatusNotAcceptable)
	errSourcePixels      = NewError("Image source exceeds pixel limit", http.StatusRequestEntityTooLarge)
	errExcludedWatermark = NewError("Image watermark sources are disabled", http.StatusNotAcceptable)
)

// validateRasterSource inspects container headers and boundaries without native decoding.
func validateRasterSource(buf []byte, maxPixels int64) error {
	if maxPixels <= 0 {
		maxPixels = defaultMaxSourcePixels
	}
	if len(buf) >= 2 && buf[0] == 0xff && buf[1] == 0xd8 {
		return validateRasterJPEG(buf, maxPixels)
	}
	if bytes.HasPrefix(buf, []byte("\x89PNG\r\n\x1a\n")) {
		return validateRasterPNG(buf, maxPixels)
	}
	if len(buf) >= 12 && string(buf[:4]) == "RIFF" && string(buf[8:12]) == "WEBP" {
		return validateRasterWebP(buf, maxPixels)
	}
	trimmed := bytes.TrimSpace(buf)
	if bytes.HasPrefix(buf, []byte("GIF87a")) || bytes.HasPrefix(buf, []byte("GIF89a")) ||
		bytes.HasPrefix(buf, []byte("II\x2a\x00")) || bytes.HasPrefix(buf, []byte("MM\x00\x2a")) ||
		bytes.HasPrefix(buf, []byte("%PDF-")) || bytes.HasPrefix(trimmed, []byte("<svg")) ||
		bytes.HasPrefix(trimmed, []byte("<?xml")) || (len(buf) >= 12 && string(buf[4:8]) == "ftyp") {
		return errExcludedRaster
	}
	return errMalformedRaster
}

func rasterPixels(width, height uint64, max int64) error {
	if width == 0 || height == 0 {
		return errMalformedRaster
	}
	if width > uint64(max)/height {
		return errSourcePixels
	}
	return nil
}

func validateRasterJPEG(buf []byte, max int64) error {
	pos := 2
	seenSOF, seenSOS, inScan := false, false, false
	var pixels error
	for pos < len(buf) {
		if inScan {
			for pos < len(buf) && buf[pos] != 0xff {
				pos++
			}
			if pos == len(buf) {
				return errMalformedRaster
			}
		}
		if buf[pos] != 0xff {
			return errMalformedRaster
		}
		for pos < len(buf) && buf[pos] == 0xff {
			pos++
		}
		if pos == len(buf) {
			return errMalformedRaster
		}
		marker := buf[pos]
		pos++
		if inScan && (marker == 0 || (marker >= 0xd0 && marker <= 0xd7)) {
			continue
		}
		inScan = false
		if marker == 0xd9 {
			if !seenSOF || !seenSOS || pos != len(buf) {
				return errMalformedRaster
			}
			return pixels
		}
		if marker == 0 || marker == 0xd8 || marker == 0x01 || (marker >= 0xd0 && marker <= 0xd7) || pos+2 > len(buf) {
			return errMalformedRaster
		}
		length := int(binary.BigEndian.Uint16(buf[pos:]))
		if length < 2 || length > len(buf)-pos {
			return errMalformedRaster
		}
		segment := buf[pos+2 : pos+length]
		pos += length
		if marker == 0xe2 && bytes.HasPrefix(segment, []byte("MPF\x00")) {
			return errExcludedRaster
		}
		sof := marker >= 0xc0 && marker <= 0xcf && marker != 0xc4 && marker != 0xc8 && marker != 0xcc
		if sof {
			if seenSOF || len(segment) < 6 || len(segment) != 6+int(segment[5])*3 || segment[5] == 0 {
				return errMalformedRaster
			}
			seenSOF = true
			pixels = rasterPixels(uint64(binary.BigEndian.Uint16(segment[3:5])), uint64(binary.BigEndian.Uint16(segment[1:3])), max)
			if pixels == errMalformedRaster {
				return pixels
			}
		}
		if marker == 0xda {
			if !seenSOF || len(segment) < 4 || segment[0] == 0 || len(segment) != 1+int(segment[0])*2+3 {
				return errMalformedRaster
			}
			seenSOS, inScan = true, true
		}
	}
	return errMalformedRaster
}

func validateRasterPNG(buf []byte, max int64) error {
	pos := 8
	seenIHDR, seenIDAT := false, false
	var pixels error
	for pos < len(buf) {
		if len(buf)-pos < 12 {
			return errMalformedRaster
		}
		n := uint64(binary.BigEndian.Uint32(buf[pos:]))
		if n > uint64(len(buf)-pos-12) {
			return errMalformedRaster
		}
		kind := string(buf[pos+4 : pos+8])
		data := buf[pos+8 : pos+8+int(n)]
		pos += 12 + int(n)
		if kind == "acTL" || kind == "fcTL" || kind == "fdAT" {
			return errExcludedRaster
		}
		if !seenIHDR && kind != "IHDR" {
			return errMalformedRaster
		}
		switch kind {
		case "IHDR":
			if seenIHDR || len(data) != 13 {
				return errMalformedRaster
			}
			seenIHDR = true
			pixels = rasterPixels(uint64(binary.BigEndian.Uint32(data)), uint64(binary.BigEndian.Uint32(data[4:])), max)
			if pixels == errMalformedRaster {
				return pixels
			}
		case "IDAT":
			seenIDAT = true
		case "IEND":
			if len(data) != 0 || !seenIDAT || pos != len(buf) {
				return errMalformedRaster
			}
			return pixels
		}
	}
	return errMalformedRaster
}

func validateRasterWebP(buf []byte, max int64) error {
	if uint64(binary.LittleEndian.Uint32(buf[4:8]))+8 != uint64(len(buf)) {
		return errMalformedRaster
	}
	pos := 12
	var canvasW, canvasH, imageW, imageH uint64
	seenX, seenImage := false, false
	for pos < len(buf) {
		if len(buf)-pos < 8 {
			return errMalformedRaster
		}
		n := uint64(binary.LittleEndian.Uint32(buf[pos+4:]))
		if n+(n&1) > uint64(len(buf)-pos-8) {
			return errMalformedRaster
		}
		kind := string(buf[pos : pos+4])
		data := buf[pos+8 : pos+8+int(n)]
		pos += 8 + int(n+(n&1))
		switch kind {
		case "ANIM", "ANMF":
			return errExcludedRaster
		case "VP8X":
			if seenX || seenImage || len(data) != 10 {
				return errMalformedRaster
			}
			if data[0]&2 != 0 {
				return errExcludedRaster
			}
			seenX = true
			canvasW = 1 + uint64(data[4]) + uint64(data[5])<<8 + uint64(data[6])<<16
			canvasH = 1 + uint64(data[7]) + uint64(data[8])<<8 + uint64(data[9])<<16
		case "VP8 ":
			if seenImage || len(data) < 10 || data[0]&1 != 0 || !bytes.Equal(data[3:6], []byte{0x9d, 0x01, 0x2a}) {
				return errMalformedRaster
			}
			seenImage = true
			imageW = uint64(binary.LittleEndian.Uint16(data[6:8]) & 0x3fff)
			imageH = uint64(binary.LittleEndian.Uint16(data[8:10]) & 0x3fff)
		case "VP8L":
			if seenImage || len(data) < 5 || data[0] != 0x2f {
				return errMalformedRaster
			}
			seenImage = true
			bits := binary.LittleEndian.Uint32(data[1:5])
			if bits>>29 != 0 {
				return errMalformedRaster
			}
			imageW, imageH = uint64(bits&0x3fff)+1, uint64((bits>>14)&0x3fff)+1
		}
	}
	if !seenImage || (seenX && (canvasW != imageW || canvasH != imageH)) {
		return errMalformedRaster
	}
	return rasterPixels(imageW, imageH, max)
}
