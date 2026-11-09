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

## Separately authorized registered-type probe

The user explicitly authorized executing the pinned published image's libvips and
the verified candidate's libvips through the same source-owned, hash-bound probe
on disposable GitHub-hosted runners. This is separate from the stock inventory
job above. One probe executable is compiled in the locked Buster builder, retained
with source/binary hashes and linked-library evidence, then copied from the
identity-checked candidate export. Both probe containers receive that identical
file as their only read-only mount. The reference image is not modified, no tools
are installed there and Imaginary does not start.

Probe containers use UID/GID 65534, no network, read-only root, dropped capabilities,
no-new-privileges, one CPU, 512 MiB memory, 128 PIDs, a 45-second command deadline
and 1 MiB output bound. The environment excludes loader overrides and has no
writable home/cache/temp location. Named containers are identity-checked and
removed even after a failure. Local native execution remains unauthorized.

The C probe initializes libvips for the normal application prefix and recursively
enumerates concrete registered foreign loader and saver classes. A bounded
protocol carries the actual runtime version and sorted unique class names.
libvips 8.10.0 defines `VipsForeignPrintMatrix` beneath `VipsForeignSaveMatrix`,
so this exact name is accepted only as a saver despite its different prefix.
Other prefix violations still fail. Warnings, invalid output, identity changes
and cleanup errors fail closed. The comparison report binds image and probe identities and records exact set
differences. A matching registered-type set does not prove every codec behavior,
full build-configuration parity, publisher provenance or production suitability.
`features` stays null until the complete required evidence is resolved. Probe
source and integration alone do not establish a successful hosted run.

## Hosted registered-type result at `d653973`

Runtime run 42548917990 passed both independent builds, both loader-comparison
gates and strict cross-build comparison. Source CI 42548917947 also passed.
Candidate and pinned published reference each report libvips 8.10.0, 42 concrete
loaders and 33 concrete savers, including `VipsForeignPrintMatrix`. Both difference
sets are empty; the two `loader-inventory.json` files match exactly.

- Candidate manifest: `sha256:d79028c3f225ab6fe137b27220d4b5cd6b4b1e7f8a41b32ca9ca4d67d625e509`.
- Candidate config: `sha256:ddbe88828274fcbe68f5ac821deef8d8ce3a3b3dfcf2d62ff4fe155c61fa1e7a`.
- Published reference manifest: `docker.io/h2non/imaginary@sha256:7facb4221047a5e79b9e902f380247f4e5bf4376400d0badbeb738d3e1c2f654`.
- Published reference config: `sha256:ccc2df4fa965458678f796ef7d2af7ac2752a5b9d564bbab9d7abadac5eb7804`.
- Probe source SHA-256: `e6b5a52a9a2c9216f9ec47c1b4008b3a750bda670bf7ebc857060df27f22dc39`.
- Probe binary SHA-256: `877cbba8e7adf7a855b431e3c35ccea1405333834153357173c312fbbc9409d1`.
- Loader-report SHA-256: `36a7738788fb734ea22142281eaf86a4b4cb96d796f708001b8c29466af80a4a`.
- Provenance SHA-256: `7edca9d3e0c45687aab7043479b55813b993fb66d836ea7af33770b731f4947f`.

Retained image artifacts 13645340045 and 13644902864 have ZIP digests
`cca640b2dda5f4ef1d120ff2d5a6b13bc6320037066361094a02f904a75a84f2` and
`f3577561e9c66565228216bf451ec3f222aa43331cb84cdca6157db396273987`.
Offline data checks verified those downloads, exact loader/probe/provenance/context
report equality, and source/tree/recipe/lock/workflow bindings. Hosted strict
comparison verified matching image manifest, config, layers and ordered DiffIDs.
Evidence archives are not byte-identical. No downloaded code or image layers ran
locally.

Earlier probe runs failed because the name-prefix policy rejected the legitimate
matrix-print saver descendant. libvips v8.10.0 `foreign/matrixsave.c` defines it
under `VipsForeignSaveMatrix`; recursive concrete-class enumeration includes it.
The narrow saver-only correction preserves identifier/count/output bounds,
duplicate rejection, image identities, isolation and cleanup requirements.

This resolves registered-type enumeration only. It does not resolve full
configuration/codec-behavior parity, publisher source-to-image provenance, security
suitability or memory qualification. The report keeps `features_complete: false`,
and the lock keeps `features: null`. The stock inventory still reports missing
CLI/ELF/configuration tools independently.

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
hashes alone do not prove a loader mismatch. Registered types were unresolved at
this historical checkpoint; the `d653973` result above supplies that evidence.
Publisher source/build provenance remains unresolved. Do not mark `features`
complete or substitute the instrumented source baseline for this published reference.

Source CI 42493897663 passed at `4467683`. Runtime run 42493897661 passed both
independent builds and strict comparison. Its manifest, config, layers and DiffIDs
also equal the earlier `5fb0679` result, including manifest
`sha256:ab402cd3ed878cef6e62dffaf5dfefcc87d34a75b0f71f95f7272ddca486a2e4`.
This supports the candidate side of the file comparison without establishing
published-reference feature parity or memory qualification.
