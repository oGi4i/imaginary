# Runtime image experiment CI

`.github/workflows/runtime-image.yml` is an unqualified, branch-only draft.
**Execution is blocked:** an unconditional failing step prevents Docker input
collection and image builds. The proposed `docker-container` BuildKit driver has
no rootless or capability constraint in this workflow. Its daemon privilege and
build `RUN` capabilities have not been approved; the five approved capabilities
for disposable APT closure containers do not approve this builder. A separate
privilege disposition and source review are required before removing the gate.

The draft does not push a registry image or deploy. If approved and corrected, it
is intended to use two independent hosted runners without shared caches, upload
one OCI artifact per build, and compare content-addressed objects in a third job.
An OCI artifact is not a registry image, production approval or a memory bound.

The remaining description is the proposed path, not evidence of execution or
completed review. Preparation fails until the lock contains the authenticated package closures,
build configuration, and other required inputs. Discovery output alone is not
approval. The workflow extracts the keyring from the digest-pinned base, resolves
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
and feature parity unqualified. Missing reference-feature evidence and actual
closure verification remain acceptance blockers. Matching image object digests
are not an independent security disposition or evidence of deployment fitness.
Run only offline mocked Python tests locally; do not run native builds locally.
