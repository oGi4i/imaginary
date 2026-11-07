# Published reference inventory

The independent `reference-inventory` job in `.github/workflows/runtime-inputs.yml`
runs on pushes to `mitigate-native-memory` that match that workflow's path
filters. It does not depend on the metadata discovery job. It tests the helper,
then collects the fixed public `linux/amd64` reference image inventory on a
GitHub-hosted runner. It does not start the Imaginary service.

The helper checks the pinned manifest reference, config digest, platform and
image volumes before running fixed package, libc, libvips, ELF and dependency
probes. Probe containers use a non-root UID, read-only root filesystem, no
network, dropped capabilities and no host mounts. The helper limits command
duration and output, and removes each named container. It uses an empty Docker
client configuration and does not pass credentials to containers.

The `reference-inventory` artifact contains only the bounded, parsed
`inventory.json` and is retained for seven days. If an optional tool or probe
is unavailable or its output cannot be parsed, the helper writes an incomplete
inventory and exits with status 2. The job fails but still attempts artifact
upload. Fatal identity, package, command supervision or cleanup failures fail
the job and may leave no artifact. No missing value means zero.

The image might lack the libvips CLI or ELF tools. Missing loaders and config
are not inferred from a shared-library version. This inventory does not prove
loader parity, publisher build provenance, production suitability or a memory
bound. Do not run the helper locally; no image publication or deployment is part
of this job.
