# Authorized bimg v1.1.4 WebP correction

`resizer.go.txt` replaces only `resizer.go` in a staged copy of the verified
`github.com/h2non/bimg` v1.1.4 module. The `.txt` suffix prevents application
`go test ./...` from treating this incomplete source copy as another package.
The original MIT license is retained in `LICENSE`.

`identity.json` records the original source URL, module version, exact byte sizes
and SHA-256 values. The original source is 14,111 bytes with SHA-256
`4897d4105655308bb8235132f057753b718facd1368b11c867ee28d14c16802f`.
The replacement is 14,861 bytes with SHA-256
`6dd0a8e838850b54500fad5fd48f4fdd1b014b566093523e2a9a02e342ae4bef`.
These are source identities, not proof of runtime correctness or memory behavior.

## Scope

The original WebP shrink-on-load path returns the unreduced resize factor, unlike
the JPEG path. Hosted source run 42433576835 and pinned-runtime run 42433576851
failed 1×1 and 16×16 WebP resizing while conversion and 300×300 resizing passed.
The native error text was not captured; this is a source-supported diagnosis.

The correction applies only to WebP:

- Bound preload shrink by the source axes and libvips' 1024 limit; skip values
  below two and divide the remaining resize factor by the actual preload factor.
- Bound subsequent integral shrink by the loaded axes and adjust its residual.
- Keep inferred output dimensions and per-axis residual sizes at least one pixel.

Other formats retain the original path. Crop/embed and operation order remain in
place. Native rounding, crop, zoom, rotation and output behavior still require
hosted verification; arithmetic checks alone do not establish equivalence.

## Build contract

Keep the tracked `go.mod` and `go.sum` unchanged and verify original modules
before staging. The shared runtime-image patch helper must reject identity or
source mismatches, replace only this file, and bind the staged module and temporary
module file into build evidence. Every test, race, vet and binary-build command
must select the same approved replacement. Module-cache verification does not
verify a local replacement; the staged-file checks are separate.

Apply this exact correction to both the instrumented upstream-source baseline and
candidate. No instrumented baseline image is established by these source files.
Both patched roles remain distinct from the unchanged published reference.
libvips remains 8.10.0. Do not weaken source admission or replace the original 1×1
runtime probe to pass the image gate. No memory qualification or deployment
approval follows from this correction.
