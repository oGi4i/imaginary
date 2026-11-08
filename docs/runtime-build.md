# Offline runtime build preparation

The recipe and preparer are under development. Do not use their output as a
candidate image until independent review and the GitHub-hosted build checks
pass. The lock now records reviewed authenticated package closures and keyring
size; `features` remains null. Source CI 42404371864 passed and the authentication
step in run 42404371869 succeeded. Reviewed proposal artifact 13586846705 supplied
candidate SHA-256 `f6460bba74d4ed53dd002989fbd22ecee978f2da434485b9d7f2134b5977f57c`.
That historical digest predates the authorized bimg patch metadata. The verifier
never automatically modifies the lock; the authenticated package bytes were
copied manually.
This evidence covers only the package closure, not reference parity, image
reproducibility or memory qualification. The user approved the exact hosted
BuildKit daemon and nested RUN contract. After the WebP correction, both image
jobs in run 42487208065 passed native and runtime checks and retained artifacts;
that comparison failed on differing provenance. Construction fixes at `5fb0679`
then passed both independent builds and strict provenance/OCI comparison in run
42490597131, including all eight layers. See [runtime image CI](runtime-image-ci.md)
for exact identities and remaining qualification limits.

`tooling/runtime-image/build.py` accepts `--lock`, `--closure`, `--discovery`,
`--keyring`, `--source`, `--output`, `--commit`, `--tree`, `--recipe-sha256`,
`--workflow`, and `--workflow-sha256`. The source must be a clean checkout of
the supplied commit and tree. The output must be a new path outside that tree.
The helper uses the tracked commit archive, checks the recipe and workflow
hashes, verifies the official Go payload, authenticates snapshot indexes and
compares package closures with the base inventory and lock. It checks requested
roots against the source-owned root list, resolves them from authenticated package
metadata, and compares the concrete roots with the lock. It recomputes all
satisfying provider edges from authenticated packages and checks the complete
provider-name union against lock dependencies. It obtains modules
using the verified Go toolchain with `go.sum` verification and read-only module
files. The authorized WebP correction uses a staged copy of the verified bimg
v1.1.4 module. Only existing file `resizer.go` changes; its original and replacement
bytes must match the exact patch identity. The original module ZIP has no
`go.mod`, so the staged local replacement also needs one fixed, synthetic module
declaration. No other added or changed module files are permitted. The tracked
application `go.mod` and `go.sum` stay unchanged.
A temporary module file permits only the approved local replacement, and tests,
race checks, vet and compilation must all consume it. `go mod verify` checks the
original module cache, not a local replacement; staged-file checks provide the
separate binding. The context uses normalized timestamps and an offline Dockerfile.

After module verification, the staged cache excludes only
`cache/download/sumdb`, whose mutable lookup/tile files caused different context
manifests. Extracted modules and downloaded module artifacts remain available for
offline verification. The original cache is not changed, and the strict context
comparison is not filtered.

The same source-owned patch helper and identity are required for any instrumented
upstream-source baseline. No instrumented baseline image builder or successful
patched image is established by this preparation. libvips remains 8.10.0, and
neither patched role substitutes for the unchanged published reference.

The BuildKit and frontend digests and buildx artifact hash are checked during
preparation. The approved workflow also enforces the actual daemon configuration
and nested RUN contract before building. Preparation alone does not enforce the
caller's builder; do not bypass those workflow checks. Provenance must separately record the source commit, tree,
recipe and workflow hashes and the actual tool identities. The build must run
only on the approved GitHub-hosted CI jobs and must not publish an image.

The helper requires the GitHub branch ref, repository and commit environment to
match its inputs. This is a consistency check, not proof of authorization. The
workflow must bind those values to its reviewed trigger and source identity.
The context contains `provenance.json` and `context-manifest.json` with the
source, recipe, consumed lock and prepared input identities. The manifest lists
files before these two evidence files are added, avoiding a self-reference. The
workflow must retain both files outside the built image. Hashes identify bytes;
they do not authenticate the lock or establish qualification.

The verification stage copies read-only builder feature and artifact manifests
into `/usr/local/share/imaginary-build/`. The workflow must extract them from
the exported image and compare the recorded file hashes and symlink paths with
the exported runtime. Builder feature output is not a runtime loader inventory
or reference parity evidence.

A null `features` value may permit an unqualified test build; it is not
proof that the reference loaders or features match. A build does not establish
reproducibility, a memory bound, security suitability or deployment approval.
