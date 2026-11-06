package main

import (
	"io"
	"net/http"
	"strings"
)

const formFieldName = "file"
const maxMemory int64 = 1024 * 1024 * 64

const ImageSourceTypeBody ImageSourceType = "payload"

type BodyImageSource struct {
	Config *SourceConfig
}

func NewBodyImageSource(config *SourceConfig) ImageSource {
	return &BodyImageSource{config}
}

func (s *BodyImageSource) Matches(r *http.Request) bool {
	return r.Method == http.MethodPost || r.Method == http.MethodPut
}

func (s *BodyImageSource) GetImage(r *http.Request) ([]byte, error) {
	if r.Body == nil {
		return nil, ErrEmptyBody
	}
	limit := maxBodyBytes(s.Config.MaxBodyBytes)
	// Keep direct source callers bounded too, not just ImageMiddleware callers.
	r.Body = http.MaxBytesReader(nil, r.Body, limit)
	if isFormBody(r) {
		return readFormBody(r, limit)
	}
	return readRawBody(r, limit)
}

func isFormBody(r *http.Request) bool {
	return strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/")
}

func readFormBody(r *http.Request, limit int64) ([]byte, error) {
	defer func() {
		if r.MultipartForm != nil {
			_ = r.MultipartForm.RemoveAll()
		}
	}()
	memory := maxMemory
	if limit < memory {
		memory = limit
	}
	if err := r.ParseMultipartForm(memory); err != nil {
		return nil, bodyReadError(err)
	}
	// A multipart parser can stop at the final boundary before the HTTP body ends.
	// Check the bounded remainder too, so epilogues cannot bypass the total cap.
	if _, err := io.Copy(io.Discard, sourceContextReader{r.Context(), r.Body}); err != nil {
		return nil, bodyReadError(err)
	}

	file, _, err := r.FormFile(formFieldName)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	buf, err := readSourceBytes(r.Context(), file, limit)
	if err == nil && len(buf) == 0 {
		err = ErrEmptyBody
	}
	return buf, err
}

func readRawBody(r *http.Request, limit int64) ([]byte, error) {
	return readSourceBytes(r.Context(), r.Body, limit)
}

func init() {
	RegisterSource(ImageSourceTypeBody, NewBodyImageSource)
}
