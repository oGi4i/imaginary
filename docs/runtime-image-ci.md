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
source file with module and CGO modes disabled. Codec probes, SBOM completion and
independent image comparison remain unverified; differing exported digests still
block a reproducibility claim.

The remaining description is the proposed path, not evidence of execution. The
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
