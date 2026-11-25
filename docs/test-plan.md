# Single-cluster release validation

The default command targets an already deployed MinIO service, including Ubuntu 26.04. Every case sets up its own required state. Teardown runs after success or failure. No old release, pre-upgrade phase, application bucket or second cluster is required.

| # | Default check | Main assertions |
| --- | --- | --- |
| 1 | Cluster health and identity | Live/ready/read/write quorum probes; all nodes and drives online; consistent build |
| 2 | Endpoint and test buckets | Unique private buckets, ownership tags, authenticated write/read, versioning setup |
| 3 | Object operations | 0 B, 1 KiB, 1 MiB, 64 MiB, 1 GiB; original SHA-256, GET/HEAD/COPY, metadata, tags, ranges, delete |
| 4 | Overwrite and batch delete | 100 overwrite cycles and exact batch-delete results |
| 5 | Pagination and key names | 2,105 keys, page boundaries, prefixes, delimiters and special characters |
| 6 | Multipart uploads | Two 1 GiB uploads, part replacement, restricted uploader, abort, byte integrity |
| 7 | Access restrictions | Restricted STS session, invalid credentials and anonymous denial |
| 8 | Presigned URLs | PUT/GET, tampering and expiration |
| 9 | Conditional requests | ETag GET/HEAD and copy preconditions, exact HTTP 206 range, invalid range rejection |
| 10 | Bucket configuration | Location/tags; CORS round trip or explicit unsupported capability result |
| 11 | Bucket policy | Exactly one synthetic object public temporarily; sibling read/list/write denied; policy removal |
| 12 | Encryption behavior | HTTP SSE-C rejected without storing an object; HTTPS verifies encrypted reads and key rejection |
| 13 | Object lock configuration | Empty locked bucket and automatic versioning; no retained object |
| 14 | Versioning | Exact versions and delete markers; older data readable |
| 15 | Null versions and restoration | Null/suspended versions; removing latest versions/markers restores correct bytes |
| 16 | Concurrent operations | 8 clients × 12 rounds; shared versioned PUT/DELETE and independent overwrites |
| 17 | Lifecycle configuration | Rules round trip unchanged |
| 18 | Lifecycle actions | Real expiry of backdated current/null objects; sole marker removal; protected/fresh/history data survive |
| 19 | Final data integrity | Retained bytes, metadata, history, deletion, lifecycle and health rechecked |
| 20 | Remove test buckets | Ownership checks, all versions/markers/uploads removed, buckets absent, local payloads removed |

CORS HTTP 501 is reported explicitly as an unsupported bucket API, never as a successful CORS round trip. HTTP cannot perform an SSE-C encrypted round trip: its rejection is the expected check. Active governance retention and legal hold are only in the explicit lab group because they require retention/hold removal permissions. COMPLIANCE retention is never used.

| Original runner coverage | Consolidated location |
| --- | --- |
| Health, CRUD, metadata, tags, ranges, copy, multipart, versioning, presigned URLs | Default cases 1–8, 14–15, 19 |
| Conditional requests, bucket location/tags/CORS, anonymous policy, SSE-C | Default cases 9–12; stricter payload and denial assertions |
| Object-lock configuration | Default case 13, without retained objects |
| Legal hold and governance retention | Explicit lab concurrency group |
| Locust long-run traffic | `./run.sh soak URL`; bounded objects, versions and uploads; original checksum oracle |
| Local source build / launched-server smoke | Retired from the public endpoint workflow; service repository owns builds |
| Development release topology, split-object lifecycle, fault recovery, admin, movement, replication | Retained under `./run.sh lab GROUP`, configured independently |

## Optional sustained activity

`./run.sh soak URL` runs a separate five-minute soak with 32 clients. Each client rotates among 64 KiB, 1 MiB and 16 MiB objects and 32 key slots; each slot has an original and a copy key. Every completed cycle verifies original and copied bytes with streaming SHA-256 and size checks, HEAD and tags, versioned deletion, and multipart completion followed by byte and metadata verification. It then removes that cycle's data versions and delete marker. Workers finish started cycles before teardown. Only failed or unacknowledged uploads are aborted during cleanup; deliberate abort coverage remains in the default endpoint suite.

At the defaults, the workload can visit 2,048 distinct keys while keeping at most 128 live versions/markers and 32 pending uploads. Short runs might visit only part of the key set; the report records actual coverage. More keys and mixed sizes broaden the workload but do not prove coverage of every pool, erasure set or disk. No pool placement, fault injection, global configuration changes or second cluster is involved.

Progress is printed every 30 seconds and statistics are saved during the run. Request logs rotate at 64 MiB with three backups, limiting that log set to approximately 256 MiB. Teardown uses the runner's in-memory ownership records and does not depend on retaining the complete request log. All reports remain after teardown. See [soak guide](soak.md) for longer runs and concurrency settings.

The release's withdrawn merged noncurrent cleanup is not treated as a fixed feature. Default tests do not require split-object space reclamation. Forced pool placement, rebalance/decommission, node failures, replication, KMS/IAM integrations, console UI and performance qualification need separate lab or application checks.

Validation evidence for this consolidation is recorded in [validation record](validation.md). Harness tests and a disposable HTTP fixture validate the runner; execution on the actual Ubuntu 26.04 cluster remains the deployment check.
