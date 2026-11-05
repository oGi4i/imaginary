package main

/*
#cgo pkg-config: vips
#include <vips/vips.h>
*/
import "C"

import "github.com/h2non/bimg"

func init() {
	// Dependency initialization finishes before this package accepts any work.
	bimg.VipsCacheSetMax(0)
}

func nativeOperationCacheLimit() int {
	return int(C.vips_cache_get_max())
}

func nativeOperationCacheSize() int {
	return int(C.vips_cache_get_size())
}
