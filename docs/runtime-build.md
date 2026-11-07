# Offline runtime build preparation

The recipe and preparer are under development. Do not use their output as a
candidate image until independent review and the GitHub-hosted build checks
pass. The lock now records reviewed authenticated package closures and keyring
size; `features` remains null. Source CI 42404371864 passed and the authentication
step in run 42404371869 succeeded. Reviewed proposal artifact 13586846705 supplied
candidate SHA-256 `f6460bba74d4ed53dd002989fbd22ecee978f2da434485b9d7f2134b5977f57c`.
The verifier never automatically modifies the lock; these bytes were copied manually.
This evidence covers only the package closure, not reference parity, image
reproducibility or memory qualification. The BuildKit privilege gate remains closed.

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
