# Qualification snapshot (partial)

The opt-in `-qualification-snapshot` flag starts a separate read-only HTTP
listener on `127.0.0.1:9001`. It exposes only `GET /snapshot` without a query
or body. There is no application-port route. Loopback is shared with other
containers in the Pod; it is not authentication. Do not use untrusted sidecars
or expose this listener through a Service or Ingress. Access requires separate
approval for a Pod-scoped port-forward.

The listener limits connections and concurrent snapshots, uses finite deadlines,
rejects unsupported requests with fixed JSON, and emits no paths, request data,
logs, or image data. Its v3 JSON fields are:

- `schema_version`: integer 3; `build_identity`: application `Version` string,
  not a unique build digest.
- `process_lifetime`: random per-process hexadecimal token, not authentication;
  `sequence`: increasing per-process snapshot count.
- `go_heap_alloc_bytes`, `vips_tracked_bytes`: bytes;
  `vips_tracked_allocations`, `vips_cache_operations`,
  `vips_cache_limit_operations`: counts. These domains overlap and must not be
  added. Observations are not atomic across getters.
- `process_rss_bytes`, `process_lazyfree_bytes`: bytes from smaps rollup.
  `process_status` is `ok` when both fields are available; otherwise both are
  null and status is `unavailable`.
- `cgroup_current_bytes`, `cgroup_peak_bytes`, `cgroup_anon_bytes`, and
  `cgroup_file_bytes` are always null; `cgroup_status` is `identity_unproven`.
  Membership and mount information have not been bound to an independently
  established exact container identity. A private collector must reject these
  missing channels; a Pod, ancestor, helper, or namespace root is not a valid
  substitute. No cgroup metrics are captured by this slice.

The bimg v1.1.4 memory getter reads libvips tracked bytes and allocation counts.
In libvips 8.10.0, these getters use `vips_tracked_mutex`; the cache size getter
uses `vips_cache_lock`. The cache limit getter has no internal lock, but this
fork sets the limit during initialization and does not change it afterwards.
The implementations are in the pinned [libvips cache source](https://github.com/libvips/libvips/blob/v8.10.0/libvips/iofuncs/cache.c)
and [tracked-memory source](https://github.com/libvips/libvips/blob/v8.10.0/libvips/iofuncs/memory.c);
[bimg's getter](https://github.com/h2non/bimg/blob/v1.1.4/vips.go) calls them.
The listener does not take the image admission slot. Tracked bytes are not
cache bytes, and native accounting is not total process memory.

The `lifecycle` object contains `request_sequence`, `oldest_completion_sequence`,
`latest_completion_sequence`, `overwrite_total`, and `records`. The last 32
completed records are retained in completion order. Each has numeric
`request_sequence`, `completion_sequence`, `status` (1 success, 2 rejected,
3 error, 4 cancelled), and nanosecond durations `queue_ns`,
`source_preflight_ns`, `transform_output_ns`, `response_ns`, `cleanup_ns`,
`processing_ns`. Unobserved stages are null. Processing equals transform/output
plus cleanup when both exist; it excludes response transfer. Publication follows
cleanup and precedes gate promotion. Request sequence is assigned at gate entry,
and completion order can differ. `overwrite_total` counts ring turnover, not
lost collector data. A collector detects lost unread records with its completion
cursor and the oldest retained completion sequence, not request sequence gaps.
The JSON snapshot is bounded to 32 KiB by the fixed-size record ring.

Trusted container identity, collector integration, qualification and
observation-overhead measurement are not implemented here.
In particular, a disabled listener supplies no timing observations. A future
comparison must define and review its timing protocol rather than infer those
measurements. No snapshot is proof of image provenance or container identity.
