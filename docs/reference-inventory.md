# Published reference inventory

The independent `reference-inventory` job in `.github/workflows/runtime-inputs.yml`
runs on pushes to `mitigate-native-memory` that match that workflow's path
filters. It does not depend on the metadata discovery job. It tests the helper,
then collects the fixed public `linux/amd64` reference image inventory on a
GitHub-hosted runner. It does not start the Imaginary service.

The helper checks the pinned manifest reference, config digest, platform and
image volumes before running fixed package, libc, libvips, ELF and dependency
probes. Native-file probes also record SHA-256 hashes of regular files and symlink
targets beneath `/usr/local/lib`, using only utilities already present in the
image. The fixed root includes the libvips library and any installed native
modules; it does not authorize arbitrary filesystem reads. Probe containers use
a non-root UID, read-only root filesystem, no
network, dropped capabilities and no host mounts. The helper limits command
duration and output, and removes each named container. It uses an empty Docker
client configuration and does not pass credentials to containers.

The `reference-inventory` artifact contains only the bounded, parsed
`inventory.json` and is retained for seven days. If an optional tool or probe
is unavailable or its output cannot be parsed, the helper writes an incomplete
inventory and exits with status 2. The job fails but still attempts artifact
upload. Fatal identity, package, command supervision or cleanup failures fail
the job and may leave no artifact. No missing value means zero.

The image might lack the libvips CLI, ELF or file-inventory tools. Missing tools,
malformed rows, empty required library evidence or incomplete enumeration must
remain unavailable, not an empty successful inventory. Native-file rows are
bounded to 10,000 rows and 1 MiB per probe, parsed and sorted. Only validated
hashes, canonical public library paths and safe symlink targets are retained.
The `libvips.so.42` link must resolve directly to an inventoried versioned library
before both native probes can count as available. File identities can be compared with candidate
builder/runtime artifact manifests, but matching files alone do not enumerate
registered loaders or prove configuration/feature parity. Missing loaders and
config are not inferred from a shared-library version. This inventory does not prove
loader parity, publisher build provenance, production suitability or a memory
bound. Do not run the helper locally; no image publication or deployment is part
of this job.
