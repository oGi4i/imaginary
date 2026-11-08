# Runtime image experiment CI

`.github/workflows/runtime-image.yml` defines an unqualified branch-only experiment.
The user approved two independent artifact-only builds on disposable GitHub-hosted
VMs. Each uses a privileged pinned BuildKit daemon, a named state volume and a
remote Buildx driver over Docker exec and the daemon's local Unix socket. The
daemon has no published port, host bind or Docker socket mount. The workflow checks
the actual daemon configuration and a network-disabled nested RUN before the
candidate build. A mismatch stops the job. The RUN contract permits the default
14 capabilities, UID 0, and NoNewPrivileges 0; seccomp must be active. AppArmor
state is recorded from a bounded allowlist, not assumed to be Docker's profile.
The daemon validator accepts Docker's exact privileged-container normalization
`SecurityOpt: ["label=disable"]`, as well as empty options. Extra entries and
explicit seccomp/AppArmor overrides remain rejected. This does not constrain the
privileged daemon as an ordinary unprivileged Docker container.

The disposable VM is the security boundary. A compromised build may control the
VM and its ephemeral GitHub credentials. The experiment has no private credentials,
registry publication or cluster access. It uploads one OCI artifact per build and
compares content-addressed objects in a third job. An artifact is not production
approval or a memory bound. Actual run 42427631572 passed the daemon and RUN
contract, built the runtime and exported OCI archives in both jobs. Classic Docker
then rejected the OCI layout during `docker load`; runtime checks and comparison
did not run, and no image artifacts were retained. The exported config and manifest
digests differed across jobs, so reproducibility is not established.

The import path now converts the exact validated OCI archive to Docker load
format without a second build. It preserves config bytes, streams bounded raw or
gzip layers, and verifies the ordered uncompressed DiffIDs. It does not extract
layer files or execute archive contents. After load, Docker's image config digest
and ordered layer DiffIDs must match the conversion identity before any runtime
probe. The original OCI archive remains the artifact and SBOM input; the temporary
Docker archive is not published. Run 42429314068 loaded both images and passed
those identity gates, non-root/library checks and artifact comparisons. It then
failed when the standalone PNG checker build tried to resolve application modules
with `GOPROXY=off`. The checker build now uses its single standard-library-only
source file with module and CGO modes disabled.

Run 42431858481 loaded the exact exported OCI images and passed config digest and
ordered DiffID gates, as well as native/library checks. The JPEG and PNG input
probes passed full PNG decoding and 1×1 dimension checks. The WebP input probe
returned HTTP 400. No image artifacts were retained and the independent image
comparison was skipped. Earlier exported digests differed; reproducibility remains
unproven.

Tests-only commit `00f2d28` recorded the failure in source CI 42433576835 and
pinned-image CI 42433576851. In both, `TestWebPInputToPNG` failed native resize
to 1×1 and 16×16 and HTTP resize to 1×1; native conversion to 550×368 and resize
to 300×300 passed. Both pinned-image matrix builds failed in the Dockerfile's
native Go regression tests before export; no image artifacts were retained. Pinned
bimg `resizer.go` lines 450–477 shrink WebP on load without dividing the residual
factor, unlike JPEG. This supports a likely cause, but the native error was not
captured and the diagnosis is not confirmed. The user explicitly authorized an
exact-hash bimg v1.1.4 correction for both the instrumented upstream-source baseline
and candidate, retaining libvips 8.10.0. The changed processing dependency must be
identified separately from the unchanged published reference; the memory-policy
comparison remains cache-only. The correction needs hosted native verification.
Keep the original 1×1 probe and admission controls unchanged. A shared patch helper
does not establish that the instrumented baseline image was built. First patched
run 42486160963 stopped during source-CI staging, and both jobs in 42486160994
stopped during context preparation with `module identity mismatch`; no patched
native tests or BuildKit solve ran. The verified original bimg ZIP lacks
`go.mod`, which the initial staging helper incorrectly required. The correction
adds only an exact synthetic module declaration to the local replacement.

The remaining description specifies the workflow, not a claim that every step
completed. The
lock now contains the reviewed authenticated package
closures. `features` remains null, and preparation still requires its other
inputs and checks. Discovery output alone is not approval. The workflow extracts the keyring from the digest-pinned base, resolves
closures, then passes those files to `build.py`. Preparation binds the clean
source checkout to the GitHub commit, tree, Dockerfile, and workflow hashes.
The runner downloads Buildx and Syft and checks their exact sizes and SHA-256
before execution. The Dockerfile frontend and BuildKit image use digest pins.

Each job exports an OCI archive with `SOURCE_DATE_EPOCH` and timestamp rewriting,
without BuildKit-generated attestations in the image. It retains the SBOM,
preparer provenance and context manifest, tool identities, runtime inventory,
checks and image
alongside payload sizes and hashes. Go tests, race tests and vet run in the
Dockerfile verification stage. Runtime checks include non-root identity, glibc,
linked libraries, and decoding JPEG, PNG and WebP fixtures through the service.
The runtime probe binds only a loopback port on the hosted runner.

The runtime image carries read-only builder evidence from the same build graph:
libvips version, configuration and loader inventory, plus application and native
file hashes and symlink targets. The workflow extracts that evidence from the
exported runtime and compares the hashes and symlinks against runtime files.
For JPEG, PNG and WebP inputs, it checks HTTP status and independently decodes
the complete PNG output with the verified Go toolchain. It checks 1×1 output
dimensions before recording success. The runtime image has no `vips` CLI; builder
loader output does not prove runtime loader equivalence. `features.json` marks runtime loader enumeration unavailable
and feature parity unqualified. Missing reference-feature evidence and independent
build-time closure verification remain acceptance blockers. Matching image object digests
are not an independent security disposition or evidence of deployment fitness.
Run only offline mocked Python tests locally; do not run native builds locally.
