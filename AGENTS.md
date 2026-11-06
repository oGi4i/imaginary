# Imaginary native-memory candidate

This fork develops a cache-only candidate from upstream v1.2.4, commit
`9357624e976790905f1b1bf4d059f8643c21e6fb`. Keep bimg at v1.1.4 and
libvips at 8.10.0 while comparing behavior. Upstream master is not the
reference release. Do not merge an upstream upgrade into this experiment.

## Implementation flow

Read the current instructions, clarify unclear requirements, inspect existing
symbols and all call sites, plan cross-file work, add a failing regression test,
implement the smallest change, run checks, and obtain an independent review.
Keep active image work serialized through native processing and cleanup.
A cancelled client must not release capacity while native code still runs.
Health and version requests must not wait behind image work.
`ImageMiddleware` owns the shared gate: one active request, at most one waiter,
and a 30-second queue timeout. Multipart cleanup runs before promotion.
Source admission accepts single-frame JPEG, PNG and WebP with a provisional
16,000,000-pixel default; see `docs/source-limits.md`. Image watermarks are
disabled. `-max-body-bytes` bounds actual raw/multipart bodies and HTTP/filesystem
source reads after gate admission; its 16 MiB default is provisional. Rejections
return fixed JSON 413 without a placeholder; see `docs/body-limits.md`.
Output/intermediate work limits are incomplete. Do not treat these controls as
a qualified OOM-prevention policy.

`native_cache.go` disables the operation cache with `bimg.VipsCacheSetMax(0)`
in package initialization, after bimg/libvips initialization and before requests.
Its native getters provide read-only cache assertions for tests; there is no
preload library or control-file interface.

## Verification and limits

Use `.github/workflows/native-memory.yml` for tests, race checks, vet and build.
See `docs/ci.md` for exact build inputs and verification limits. Do not execute
upstream code in a local environment without separate authorization.
CI does not qualify a production image or prove a memory bound.

Do not add allocator trimming before matched cache-only qualification shows
that it is needed. Do not publish images, enable deployment automation, add
secrets, or put private infrastructure details and inputs in this public fork.
Keep `AGENTS.md` and the fork-specific README notes current with implemented
behavior; distinguish tests from deployment and qualification evidence.
