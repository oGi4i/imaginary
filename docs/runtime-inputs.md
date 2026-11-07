# Runtime input discovery

This is an incomplete discovery stage, not an image build or reproducibility
check. The workflow deliberately fails after the evidence steps. Task 2.7
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
signer fingerprints, keyring SHA-256 and byte size, and the freshness exception.
The keyring read is capped at 8 MiB. Both signatures use a private temporary
copy of those exact bytes. Another helper can refetch a
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
The verifier runs in the same job after a fresh collector replay. It
requires the source-pinned closure digest and length, reauthenticates the base,
keyring, signed indexes and package evidence, measures the base independently,
and verifies all final payload hashes and sizes. It recomputes dependency edges
with Debian version comparison before mapping the lock. Its 20-minute deadline
reserves cleanup time. Only after cleanup does it publish the exact
`candidate-lock.json` and `verification.json` files for seven days. The report
binds source, workflow, run, input and candidate hashes; it is not an external
signature. Source CI run 42404371864 passed. The authentication step in run
42404371869 succeeded, and the reviewed proposal artifact 13586846705 supplied
exact candidate bytes manually copied into the tracked lock. Their SHA-256 is
`f6460bba74d4ed53dd002989fbd22ecee978f2da434485b9d7f2134b5977f57c`.
The verifier never automatically modifies the tracked lock. This evidence covers
only the package closure; features remain null and the result is unqualified.

For failed offline APT configuration, CI may retain at most eight sanitized
`E:`/`W:` lines, each capped at 160 characters. URLs, paths, controls and
credential-like values are redacted before publication. The bounded log stays
inside the disposable container. Signature checks and APT sandboxing stay enabled.
The network-disabled package containers retain `cap-drop=ALL` and add only CHOWN,
DAC_OVERRIDE, FOWNER, SETUID and SETGID for package ownership and APT privilege
separation. They have no host mounts or credentials; reference probes and serving
workload security settings remain unchanged.

For each dependency group, closure evidence records every distinct installed
provider that satisfies an alternative, sorted by package identity. Multiple paths
to one provider produce one edge with the first matching alternative as its witness.
This evidence does not claim which provider APT selected. Missing providers and
invalid relations, versions or architecture constraints still fail. Signed-index
rematching establishes the metadata source; string validation alone does not
authenticate it. Compatibility `graph_conflict` diagnostics retain at most eight
alternatives/providers and a 4 KiB total limit. They contain only validated public
identities and exclude paths, URLs, credentials and raw errors.

The separate reference job inventories the public reference image. Reference
features and candidate images remain unqualified, and the discovery job ends
with an intentional failure. Do not turn missing results into empty lists or
update the lock from unreviewed evidence. No local native execution, image
publication, or cluster action is part of this workflow.
