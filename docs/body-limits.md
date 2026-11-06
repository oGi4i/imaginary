# Source-byte admission

`-max-body-bytes` sets the actual source-byte cap. The default is 16 MiB
(16,777,216 bytes). This value is provisional, not a qualification result.
`ServerOptions.MaxBodyBytes` and `SourceConfig.MaxBodyBytes` are `int64` fields;
zero or negative values use the default rather than disable the cap.

## Scope

- Raw POST bodies, including `/pipeline`, must fit in the cap.
- Multipart bodies must fit as a whole: file bytes, boundaries, headers, other
  fields and any epilogue all count. A file that fits can still exceed the cap
  because of its multipart framing.
- HTTP GET source responses and filesystem source files use the same actual-read
  cap. The check does not trust a remote `Content-Length` or a file size snapshot.
- The inherited `-max-allowed-size` retains its separate HTTP HEAD precheck.
  It does not set the raw POST limit or replace the actual GET source cap.

The image gate admits a request before its body is read. A waiter and an excess
request do not pre-read bodies or allocate image buffers through this code.
Health, version and form pages stay outside the image gate.

Each source reads at most the cap plus one byte to distinguish an exact fit from
an overflow. Unknown lengths, chunked bodies and misleading request lengths do
not bypass this check. An overflow discards partial data and returns fixed JSON
413 without an image placeholder or native decoding. Other source read failures
return fixed JSON 400. Request cancellation prevents subsequent source reads and
HTTP source requests carry the incoming request context.

Multipart parsing has a bounded input and drains the bounded remainder before
native work. Temporary files are removed on success and failure; the image gate
also owns final multipart cleanup before it promotes a waiter. The cap limits
input bytes, not Go allocation overhead, multipart metadata, parser copies or
native memory. Cancellation does not guarantee that an arbitrary blocked file
read returns immediately.

## Verification limits

`body_limits_test.go` covers tiny below/exact/over-cap inputs, unknown/chunked and
misleading request lengths, the provisional default, placeholder-safe rejection,
read errors, cancellation, multipart framing and epilogues, disk-backed multipart
cleanup, unread queued requests, and HTTP/filesystem sources. The shared reader
also tests empty input and the maximum positive `int64` cap.

The pre-implementation GitHub run `42132349114` demonstrated missing
`MaxBodyBytes` and oversized raw reads with incorrect rejection status. Updated
tests, race checks, vet and builds must run through
`.github/workflows/native-memory.yml`; local upstream execution is not authorized.
Static formatting alone does not establish a passing implementation.

Source pixel/header admission is separate; see [source-limits.md](source-limits.md).
Output and intermediate work remain unbounded. These controls do not prove a
production memory bound or qualify this branch as an OOM fix.
