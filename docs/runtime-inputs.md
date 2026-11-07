# Runtime input discovery

This is an incomplete discovery stage, not an image build or reproducibility
check. The workflow deliberately fails after uploading metadata. Task 2.7
remains open, and `docs/runtime-image.lock.yaml` retains unresolved values.

The branch-only workflow reads public inputs and does not push an image. It
verifies the byte digest of the official Debian `buster-20240408-slim`
linux/amd64 platform manifest and its config before recording the base. It
reads the Go 1.20.14 linux/amd64 archive size and checksum from the official
Go JSON API, then downloads the archive from the official Google download
host and checks both. The archive is not extracted or included in the artifact.
It extracts the Debian archive keyring from the digest-pinned base without
starting a container.

For Debian main and security snapshots at `20240408T000000Z`, the helper runs
`gpgv` on each InRelease using that keyring. It requires the expected Buster
archive primary fingerprint, the pinned InRelease byte digest and the exact
signed origin, label, codename, suite and date. It then verifies the SHA-256
and size of each amd64 Packages.xz against signed metadata. Historical metadata may have expired
Valid-Until: `gpgv` verifies signatures without enforcing that field. This is
an explicit freshness exception, not permission to use unsigned metadata.
Download, signature, fingerprint, digest, or parsing failures stop discovery.
The helper permits HTTPS redirects on the same origin. It also permits the
registry's Debian blob GET to redirect to `production.cloudfront.docker.com`,
without forwarding request headers or credentials. The config digest and size
are verified after download. Redirect hops, response size and total fetch time
are bounded. `gpgv` has a process timeout and bounded output. Go payload
acquisition uses the canonical download host directly and does not add a
cross-origin redirect exception.

The artifact `runtime-input-metadata` contains `discovery.json` with `platform`,
`base`, `go`, `snapshot`, null `build_packages`, null `runtime_packages`, null
`reference_inventory`, and `limitations`. The snapshot entries include the
InRelease and Packages URLs and SHA-256 hashes, the compressed Packages size,
signer fingerprints and the freshness exception. Another helper can refetch a
Packages index and verify its hash and size against this authenticated result. The artifact expires after seven days; it is uploaded
only if the verification step writes the JSON. The Go entry records
`payload_url` and `payload_verified` after both checks pass. A successful
verification does not mean that any Debian package payload has been downloaded
or authenticated.

After the metadata artifact, the discovery job runs the closure helper against
that verified JSON. The helper independently authenticates the indexes and
resolves build and runtime transactions from the same pinned Debian base,
including base-installed packages. If it succeeds, the separate
`runtime-input-closure` artifact contains `closure.json` and expires after seven
days. If the helper fails, the workflow cannot upload closure evidence that
was not written. A reviewed CI run is required to establish whether resolution
actually succeeds; code or mocked tests alone do not establish a closure.

The separate reference job inventories the public reference image. Reference
features and candidate images remain unqualified, and the discovery job ends
with an intentional failure. Do not turn missing results into empty lists or
update the lock from unreviewed evidence. No local native execution, image
publication, or cluster action is part of this workflow.
