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

## Hosted native-file result at `4467683`

Input run 42493897646 collected and uploaded reference artifact 13622692245.
The parsed inventory SHA-256 is
`1a250082d50772e91254e410bbc698141b1a7d27556ac6e12a92ac60f0d56aab`.
The fixed manifest/config/platform identities match, with 188 packages. Both new
native probes are available: six regular files and four symlinks. The libvips CLI,
loader/configuration and ELF probes remain unavailable, so collection exits 2 and
the inventory remains incomplete. Package closure collection and replay
authentication succeeded separately in the same workflow.

Compared with retained candidate evidence from build commit `5fb0679`, four
support-file hashes (`libvips.la`, `libvips-cpp.la` and both pkg-config files) and
all four symlink targets match. Both shared-library binaries differ:

| File | Published reference SHA-256 | Candidate SHA-256 |
|---|---|---|
| `libvips.so.42.12.3` | `6ba2d80df0919390a5f4bf346678ef5e5373a916941841b3ee0bb05d840c43cc` | `b5d4a2dc209423882438123d1cb6010abcdb1d2dd244c5e866738551424330de` |
| `libvips-cpp.so.42.12.3` | `dbf6a70beb8bc4abed414794d2d1761ab598aab502cfac5ad60e3cf73aa5bd22` | `e4d585fdc2ef6225f1d89d8e1e0bbbde30b6282c09fe71d7023aa0166e0bb378` |

All four symlinks select the corresponding `.so.42.12.3` file. A matching filename
or support file does not explain the binary differences, and different binary
hashes alone do not prove a loader mismatch. Registered-loader parity and publisher
source/build provenance remain unresolved. Do not mark `features` complete or
substitute the instrumented source baseline for this published reference.

Source CI 42493897663 passed at `4467683`. Runtime run 42493897661 passed both
independent builds and strict comparison. Its manifest, config, layers and DiffIDs
also equal the earlier `5fb0679` result, including manifest
`sha256:ab402cd3ed878cef6e62dffaf5dfefcc87d34a75b0f71f95f7272ddca486a2e4`.
This supports the candidate side of the file comparison without establishing
published-reference feature parity or memory qualification.
