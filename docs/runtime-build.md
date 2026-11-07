# Offline runtime build preparation

The recipe and preparer are under development. Do not use their output as a
candidate image until independent review and the GitHub-hosted build checks
pass. The current lock has unresolved inputs, including package closures and
keyring size. Preparation must fail until those inputs are established.

`tooling/runtime-image/build.py` accepts `--lock`, `--closure`, `--discovery`,
`--keyring`, `--source`, `--output`, `--commit`, `--tree`, `--recipe-sha256`,
`--workflow`, and `--workflow-sha256`. The source must be a clean checkout of
the supplied commit and tree. The output must be a new path outside that tree.
The helper uses the tracked commit archive, checks the recipe and workflow
hashes, verifies the official Go payload, authenticates snapshot indexes and
compares package closures with the base inventory and lock. It recomputes all
satisfying provider edges from authenticated packages and checks the complete
provider-name union against lock dependencies. It obtains modules
using the verified Go toolchain with `go.sum` verification and read-only module
files. The context uses normalized timestamps and an offline Dockerfile.

The BuildKit and frontend digests and buildx artifact hash are checked during
preparation. Preparation does not yet enforce the builder used by the caller;
do not run the recipe until the workflow enforces the verified builder and
buildx identities. Provenance must separately record the source commit, tree,
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
