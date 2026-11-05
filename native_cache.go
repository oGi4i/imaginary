package main

/*
#cgo pkg-config: vips
#include <vips/vips.h>
*/
import "C"

func nativeOperationCacheLimit() int {
	return int(C.vips_cache_get_max())
}

func nativeOperationCacheSize() int {
	return int(C.vips_cache_get_size())
}
