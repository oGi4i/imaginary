# Candidate image build design

This is a **partially implemented build design**, not a reproducible image. No candidate image has been built or qualified. The runtime-input workflow now verifies public base/toolchain metadata and signed snapshot indexes; see [runtime inputs](runtime-inputs.md). The separate [reference inventory](reference-inventory.md) remains incomplete where the published image lacks tools. Complete package closures and the locked recipe are still required. Image publication and deployment remain separate from CI input/build execution. Native execution is not approved locally; the existing Ubuntu source tests do not validate the serving ABI.

## Reference and fixed inputs

The comparison target is Imaginary 1.2.4, bimg 1.1.4, libvips 8.10.0 and glibc 2.28. The fork is based on upstream Imaginary commit `9357624e976790905f1b1bf4d059f8643c21e6fb`. Its `go.mod` fixes bimg at v1.1.4. The libvips 8.10.0 release archive is recorded in `docs/ci.md` with SHA-256 `1299d3a98213ae477f0180817ec79afd36803a5b8434f48068e75a721d23b6f9`. Verify that hash before extraction; a version tag alone is not a content lock.

The existing Dockerfile uses a Go 1.14 builder and `debian:buster-slim` runtime, downloads the libvips release, and installs native build and runtime packages. Its image tags, package repositories and package versions are mutable. It is not a reproducible recipe. Its package list is a starting point, not proof of the installed reference-image versions, enabled loaders, or complete transitive dependency closure.

## Input lock validation

`docs/runtime-image.lock.yaml` is the single input lock. It uses strict JSON, a
YAML subset; YAML-only syntax is not accepted. Unknown inputs are explicitly
`null`, not floating values. The known source fields identify the comparison
baseline, not the fork commit or build recipe. Record the actual source and
recipe identities externally in each build's provenance to avoid a
self-referential lock.

The standard-library-only `cmd/source-lock` validates the lock structure and
lists unresolved fields. In an authorized Go environment, invoke it as
`go run ./cmd/source-lock docs/runtime-image.lock.yaml`. It exits nonzero for
invalid or unresolved inputs. The parser rejects duplicate and unknown keys,
invalid identities and dangling package dependencies. The package arrays
represent separate build and runtime closures; each package names its direct
dependencies within the same closure. An empty dependency array means a package
has no dependencies; a missing dependency list is invalid. Unknown closures
remain `null`, not empty arrays. The JSON input limit is 1 MiB.

Structural completeness does not authenticate any input, establish a complete
closure, verify features, prove reproducibility, or authorize execution. Those
checks and approval gates are separate and not implemented by this validator.

## Proposed build sequence

Once the missing inputs and execution are approved, implement one multi-stage image build on GitHub-hosted runners. Do not execute it locally. Use the same target architecture, Debian Buster snapshot and locked native dependency versions in both stages. Pin the actual platform-specific image manifests, not only multi-platform tags. A Go builder must support the chosen source and target glibc 2.28; verify this rather than assuming that an arbitrary Go image has the correct Debian release.

1. Check out one immutable fork commit and its `go.mod` and `go.sum`. Record the source tree, workflow and build recipe digests. Pin the GitHub Actions and build tools used by the workflow to reviewed immutable revisions.
2. Start from a verified official Go builder manifest digest. Configure only the approved dated Debian snapshot and its archive signing keys. Install the complete locked build package closure with exact versions, including the compiler, linker, `pkg-config`, GLib and the image-format development libraries. Verify package metadata and signatures. Resolve expired snapshot metadata explicitly without disabling signature or package-hash verification.
3. Download the libvips 8.10.0 release archive from its approved source, check the SHA-256 above, and build it with the upstream Dockerfile's `CFLAGS`, `CXXFLAGS` and configure options as the initial proposal. Lock the full configure command and resulting feature summary after comparing them with the reference. Preserve the reference's supported loaders and native features; do not copy the Ubuntu test workflow's disabled SVG setting without evidence that it matches the reference.
4. Acquire the Go modules identified by `go.mod` and `go.sum` from approved sources. Enforce read-only module files, verify module checksums and run `go mod verify`; fail if resolution changes either file. Pin the Go toolchain and module acquisition policy, including whether a module proxy or checksum service is used. Build the application with CGO enabled against the locked libvips and native libraries. Record the complete build flags, environment and version metadata.
5. Start the runtime stage from the verified old Debian runtime manifest digest. Install the exact locked runtime package closure from the same dated snapshot, including all transitive libraries required by libvips, its loaders and the application. Copy only the application and required libvips artifacts from the builder. Retain the upstream non-root entrypoint behavior. Do not add a diagnostic preload or control-file interface.
6. Check the resulting image without publishing it: inspect its architecture, glibc version, dynamic dependencies and resolved libraries; compare the installed package manifest, libvips feature summary and loaders with the reference. Run the approved functional, race/concurrency and image-format tests in the authorized CI environment. Record any unsupported features or missing comparison evidence as failures, not as equivalence.

For reproducibility, also lock the image builder and Dockerfile frontend, target platform, locale, timezone, build paths, timestamps and other relevant build environment. Build independently twice from the same locked inputs and compare the resulting image and artifact digests. Investigate differences rather than calling a version-pinned build reproducible. A matching rebuild does not establish memory behavior or production suitability.

## Missing locks and approvals

These values are **not yet established** and must not be replaced with floating tags or guessed hashes:

- Official Go builder image, toolchain version and platform-specific manifest digest compatible with Buster and the target architecture.
- Old Debian runtime image and platform-specific manifest digest; exact target architecture and reference-image digest.
- A dated Debian snapshot, signing keys and exact versions and checksums for the complete build and runtime package closures. Capture the reference's installed package manifest, dynamic-library closure, libvips configure/features and loader inventory first. A matching glibc version alone is insufficient to preserve native behavior.
- Exact fork commit, clean source tree, build flags and environment; pinned workflow actions, Dockerfile frontend and image builder. Record the libvips archive URL and verified checksum in the lock alongside the module files and acquisition policy.

The current continuation covers official Go/Debian/build-tool input verification, the public reference inventory and GitHub-hosted candidate image builds retained as CI artifacts. Reference inventory uses fixed read-only commands in the exact pinned image under isolated, non-root execution; it does not start the service. These permissions do not extend to registry publication, local native execution, cluster actions or production changes. Verified metadata is recorded by the discovery workflow, but missing artifact hashes, package closures and features remain blockers rather than guessed values.

## Provenance and release gate

For each approved build, retain the source commit and tree digest, build recipe and workflow revisions, all image manifest digests, snapshot date, signed package manifests and checksums, libvips archive checksum and feature summary, Go toolchain and module verification results, compiler/linker and flags, target architecture, reproducibility comparison, test results, image digest, SBOM and provenance attestation. Include the runtime library and loader inventory. Verify the eventual published manifest digest independently before any release; publication requires its own approval.

Task 2.7 remains incomplete until a reproducible cache-only image exists and the required checks and source review pass. A successful build or matching digest alone does not qualify the image, prove a memory bound, or authorize deployment.
