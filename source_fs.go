package main

import (
	"context"
	"net/http"
	"os"
	"path"
	"strings"
)

const ImageSourceTypeFileSystem ImageSourceType = "fs"

type FileSystemImageSource struct {
	Config *SourceConfig
}

func NewFileSystemImageSource(config *SourceConfig) ImageSource {
	return &FileSystemImageSource{config}
}

func (s *FileSystemImageSource) Matches(r *http.Request) bool {
	return r.Method == http.MethodGet && s.getFileParam(r) != ""
}

func (s *FileSystemImageSource) GetImage(r *http.Request) ([]byte, error) {
	file := s.getFileParam(r)
	if file == "" {
		return nil, ErrMissingParamFile
	}

	file, err := s.buildPath(file)
	if err != nil {
		return nil, err
	}

	return s.read(r.Context(), file)
}

func (s *FileSystemImageSource) buildPath(file string) (string, error) {
	file = path.Clean(path.Join(s.Config.MountPath, file))
	if !strings.HasPrefix(file, s.Config.MountPath) {
		return "", ErrInvalidFilePath
	}
	return file, nil
}

func (s *FileSystemImageSource) read(ctx context.Context, file string) ([]byte, error) {
	source, err := os.Open(file)
	if err != nil {
		return nil, ErrInvalidFilePath
	}
	defer source.Close()

	buf, err := readSourceBytes(ctx, source, s.Config.MaxBodyBytes)
	if err != nil && err != ErrMaxBodySize {
		return nil, ErrInvalidFilePath
	}
	return buf, err
}

func (s *FileSystemImageSource) getFileParam(r *http.Request) string {
	return r.URL.Query().Get("file")
}

func init() {
	RegisterSource(ImageSourceTypeFileSystem, NewFileSystemImageSource)
}
