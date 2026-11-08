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
preload library or control-file interface. The default-off qualification
snapshot listener is read-only, loopback-only and separate from the application
port. Cgroup identity is not yet established, so its counters are unavailable;
the listener is not qualification evidence. Its bounded lifecycle records exist
only when enabled. See `docs/qualification-observation.md`.

The strict JSON-compatible YAML runtime lock and source-lock CLI validate
structure and report unresolved inputs. Schema 2 adds explicit artifact, snapshot,
keyring, package and tool identities; schema 1 remains a legacy structural format.
`source-lock --json PATH` reports fixed validity/unresolved fields, not approval.
An explicitly unqualified build may permit only the missing `features` field;
all other unresolved inputs block preparation. See `docs/runtime-lock.md`.
Neither schema validation nor JSON output authenticates the lock or builds an image. See `docs/candidate-image.md`. The separate runtime-input
metadata workflow and helpers are incomplete discovery preparation; see
`docs/runtime-inputs.md`. Its tests use mocked public metadata locally. Actual
input discovery and native reference inventory belong on GitHub-hosted runners,
not local execution. The reviewed package closure is recorded; feature evidence
remains missing. A discovery artifact is not a reproducible candidate image. The separate
reference-inventory job uses fixed read-only probes in the pinned public image;
see `docs/reference-inventory.md`. Missing native/ELF tools yield incomplete
evidence, not feature equivalence. Fatal supervision/identity/cleanup errors must
not be downgraded to optional missing tools. The closure helper resolves separate
build/runtime package states in network-disabled CI containers from verified
snapshot metadata and package payloads. Local tests mock container commands;
actual closure evidence requires CI success and is not image reproducibility.
Only disposable offline package containers add CHOWN, DAC_OVERRIDE, FOWNER, SETUID
and SETGID to the otherwise dropped capability set so APT can retain its sandbox.
Reference probes and serving workloads do not inherit those build capabilities.
Record all distinct installed providers that satisfy each dependency group, in
identity order, with the first matching alternative as a witness, not an APT
choice. Missing or invalid providers still fail. Compatibility diagnostics stay
bounded to validated public identities and must not echo raw errors. The pure lock
mapper resolves requested roots to concrete installed names using raw `Provides`;
the build preparer checks requests against source-owned roots and independently
resolves them from authenticated package rows. Neither step claims an APT choice.
The discovery verifier requires an exact pinned closure replay and authenticates
package evidence before publishing an unqualified candidate lock and identity-bound
report. Source CI 42404371864 passed; the verifier step in run 42404371869
succeeded. Reviewed proposal artifact 13586846705 supplied the manually populated
lock, SHA-256 `f6460bba74d4ed53dd002989fbd22ecee978f2da434485b9d7f2134b5977f57c`.
The verifier never automatically modifies the tracked lock. This evidence covers
only the package closure; features remain null and memory remains unqualified.

The locked offline recipe and context preparer are source-only build preparation;
see `docs/runtime-build.md`. The two-build experiment uses an approved privileged
pinned BuildKit daemon on disposable GitHub-hosted VMs and checks the daemon and
nested RUN contract before building; see `docs/runtime-image-ci.md`. The VM is
its security boundary, not the privileged daemon. Run 42427631572 passed the
builder contract and exported both OCI images, then failed because classic Docker
could not load the OCI archives. A bounded converter now preserves the exported
config bytes and layer DiffIDs in Docker load format; runtime checks must verify
the loaded config digest and ordered DiffIDs against that exact conversion.
Actual import and runtime checks remain unverified. Source tests and builder
feature evidence cannot establish runtime/reference
loader parity or qualify memory.

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
