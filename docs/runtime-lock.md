# Runtime lock schema

`runtime-image.lock.yaml` uses JSON syntax, a subset of YAML. Schema 2 is a
structural description of inputs, not authentication, build authorization,
feature equivalence or evidence of reproducibility. The source-lock parser also
accepts schema 1 for compatibility; schema 1 is not a build contract.
Source, tree, recipe and workflow provenance belongs in external evidence, not
in the lock that those objects contain.

All keys are required. Unknown or duplicate keys, case variants, trailing data,
invalid UTF-8 and inputs over 1 MiB are rejected. A top-level nullable input is
unresolved when null. Snapshot repositories and snapshot artifact fields may
also be null; their unresolved paths are reported. `valid_until: null` instead
means the authenticated Release has no Valid-Until field. A complete structure is
not necessarily authentic. The standard `source-lock PATH` command exits nonzero
for any unresolved field, including `features`. `source-lock --json PATH`
prints `{"valid":true,"unresolved":[...]}` for structurally valid inputs, or
`{"valid":false,"unresolved":[]}` for invalid or unreadable input. Exit status
is 0 only for a complete structure, 1 for incomplete or invalid input, and 2
for invalid invocation. The reported paths come from the schema, not the input.

Schema 2 fields:

- `source`: fixed upstream commit and bimg version. `libvips`: fixed version,
  archive URL and SHA-256.
- `platform`, `builder_image`, `runtime_image`: target platform and immutable OCI
  references. OCI references have a registry and `@sha256:` digest.
- `snapshot`: timestamp `date` and `main` and `security` repositories. Each has
  `inrelease` and `packages` artifacts, each with `url`, `size`, and `sha256`;
  `keyring` with pinned OCI `image`, fixed absolute `path`, `size`, and `sha256`;
  unknown keyring size is null and blocks structural completeness;
  `signing_key_fingerprint`; and `release_identity` with `origin`,
  `label`, `codename`, `suite`, `date`, and nullable `valid_until`. Signatures,
  identity and index digests must be authenticated separately. Historical
  snapshot expiry handling must be explicit in the build verifier.
- `build_packages`, `runtime_packages`: complete nonempty package arrays. Each
  entry has `name`, `version`, `architecture`, `repository` (`main` or
  `security`), `url`, `size`, `sha256`, `dependencies` and `provenance` (`base`
  or `snapshot`). Dependencies are exact package-name references within the
  same closure. `build_roots` and `runtime_roots` are nonempty arrays whose names
  must exist in the corresponding closure. Base provenance identifies packages
  already present in the pinned image; it does not replace archive verification.
- `go_toolchain`: `version`, `url`, `size`, `sha256`.
- `build_tools`: digest-pinned `buildkit_image` and `frontend_image`, `buildx`
  and `syft` artifacts (each with `url`, `size`, `sha256`), and nonempty `actions` entries with `name`,
  HTTPS `repository`, and full lowercase 40-hex `commit`.
- `build`: `go_mod_sha256`, `go_sum_sha256`, positive `source_date_epoch`, arrays
  `configure`, `cflags`, `cxxflags`, `go_flags`, and `module_mode: "readonly"`.
  Arrays preserve argument order. The consumer must compare the hashes against
  the actual module files and enforce readonly module mode.
- `features`: nonempty feature-name array or null. Null is a feature evidence
  gap. A CI consumer may explicitly permit only this unresolved path for an
  unqualified build; it must reject any other unresolved path.

The stored base digest, Go payload and signed snapshot index identities came
from discovery artifact 13573423316. The InRelease sizes came from official
snapshot downloads whose hashes match that artifact. Discovery artifact
13574856370 records the keyring size and hash. Tool identities come from public
image and release metadata. The buildx and Syft payload bytes have not yet been
downloaded and checked against their published checksums, and the tools have not
been executed in the approved CI build environment. These are separate pending
checks.
Package roots and build flags record the recipe intent, not a resolved closure.
The module hashes identify the tracked files, and `source_date_epoch` is the
upstream baseline commit timestamp. Package closures and features remain
unresolved. Discovery evidence is not itself a package closure or qualification.
