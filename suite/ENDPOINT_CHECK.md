# MinIO release test for Ubuntu 26.04

Run this command against your already deployed MinIO S3 API endpoint:

```bash
./run.sh http://HOST:9000
```

Enter your **access key** and **secret key** when prompted. The secret is hidden.
This performs **setup, all 20 checks, and teardown** automatically. You do not
need to create any buckets or objects. HTTP needs no certificate options.

Setup creates uniquely named test buckets and all required objects. Teardown
runs after success, failed checks, timeouts and normal interruption. It removes
the test buckets, every test version and marker, unfinished multipart uploads,
and local payloads. Reports remain. Existing application buckets stay untouched.
It does not start, stop or upgrade services, change global settings, set up
replication or contact another cluster.

## Copy and run

Copy `tminio_test_runner.tar.gz` and its `.sha256` file to
the internal node, then run:

```bash
sha256sum -c tminio_test_runner.tar.gz.sha256
tar -xzf tminio_test_runner.tar.gz
cd tminio_test_runner
./run.sh http://HOST:9000
```

All Python dependencies are included. Runtime needs Linux, Python 3.10+, access
to your MinIO endpoint, 2 GiB free local temporary space and approximately 5 GiB
free logical MinIO storage, plus erasure-coding overhead. No internet, package
installation, `mc`, Go, sudo or MinIO binary is needed.

From a source checkout the equivalent command is:

```bash
./run.sh http://HOST:9000
```

Source mode installs dependencies in a private venv on first use. Use the archive
on a node without internet.

## What the command checks

| Check | Assertions |
| --- | --- |
| Cluster health and identity | Four health probes; authenticated admin info confirms all nodes online, drives available and one consistent reported build. |
| Endpoint and test buckets | Authenticated S3 access, fresh bucket creation, versioning setup and a write/read probe. |
| Object operations | 0 B, 1 KiB, 1 MiB, 64 MiB and 1 GiB objects; PUT/GET/HEAD/COPY, SHA-256, metadata, tags, ranges and deletion. |
| Overwrite and batch delete | 100 overwrite cycles, latest content and batch-delete results. |
| Pagination and key names | 2,105 listing keys, pagination, prefixes, delimiters and special-character keys. |
| Multipart uploads | Two 1 GiB uploads, part replacement, STS uploader, abort and content integrity. |
| Access restrictions | Restricted STS permissions, invalid credentials and anonymous rejection. |
| Presigned URLs | GET/PUT, tamper rejection and expiration. |
| Conditional requests | GET/HEAD ETag preconditions, copy conditions, exact ranges and invalid range rejection. |
| Bucket configuration | Location and tags; CORS round trip when supported, or explicit HTTP 501 capability result. |
| Bucket policy | Anonymous read of exactly one synthetic object; sibling read, list and writes denied; policy removal verified. |
| Encryption behavior | HTTP rejects SSE-C and stores no object. HTTPS additionally tests encrypted round trips and wrong/missing keys. |
| Object lock configuration | Empty lock-enabled bucket reports lock/versioning without retained objects. Active retention/hold tests are lab-only. |
| Versioning | Exact data-version and delete-marker history; reads of older versions. |
| Null versions and restoration | Null-version reads, removal of newer versions or markers to restore older bytes, and suspended-versioning replacement. |
| Concurrent operations | Eight clients, 12 rounds each: shared-key versioned PUT/DELETE and independent overwrite/read checks. Every acknowledged data version and distinct marker must remain correct. |
| Lifecycle configuration | Lifecycle settings round-trip correctly. |
| Lifecycle actions | Backdated current and null-version objects really expire; a sole marker disappears; a marker protecting data remains; fresh/current and historical data remain readable. |
| Final data integrity | Recheck retained test data, version history, deleted keys, lifecycle settings and health. |
| Remove test buckets | Teardown verifies ownership, aborts unfinished uploads, deletes all test versions/markers and buckets, and removes local payloads. |

Actual lifecycle processing is required: saving configuration alone does not pass
that check. The script verifies backdated fixture timestamps and waits up to 600
seconds for scanner actions. It does not change scanner settings. It does not
require split-object noncurrent space reclamation, which this release does not
promise. Concurrent DELETE calls may return an existing marker, as this fork
implements; each distinct marker and every acknowledged data version is checked.

## Credentials and optional settings

Use an account allowed to read `admin:ServerInfo`, create/delete test buckets,
perform S3 object/version/tag/lifecycle/policy/CORS/object-lock configuration operations and use STS AssumeRole. MinIO
must accept the backdated test fixtures. Missing permissions and inaccessible
health routes cause failures. No permanent users or policies are created.

For automation, supply `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` through
your environment or secret manager; otherwise the script prompts for both.

| Option | Purpose |
| --- | --- |
| `--timeout 3600` | Increase the overall deadline; default 1800 seconds. |
| `--cleanup-timeout 600` | Separate teardown deadline; default 300 seconds. Cleanup still runs when the test deadline expires. |
| `--lifecycle-timeout 1800` | Increase the lifecycle wait; default 600 seconds. Increase the overall deadline too if needed. |
| `--expected-commit ID` | Require this exact reported MinIO commit ID on every node. Use a commit ID, not a release-tag name. |
| `--work-dir DIR` | Existing parent directory for reports and temporary payloads. |
| `--region REGION` | Server region; default `us-east-1`. |
| `--ca-file CA.pem` | Only for HTTPS using an internal CA. HTTP needs no certificate option. |

## Results and scope

Exit **0** means all 20 checks and teardown passed; **1** means failed or incomplete
checks or a cleanup failure; **2** means invalid arguments or setup.
Each run prints its `minio-check-*` report
directory containing `summary.txt`, `results.json`, `junit.xml`, `requests.jsonl`
and `objects.json`. Secrets and signed URL query strings are redacted.

Cleanup runs even if a check fails. The result cannot be PASS while test data
remains. If the service becomes unreachable or deletion is denied, cleanup
reports a failure and identifies the remaining buckets in `results.json`. A
forced kill or host failure can prevent teardown from running; reports identify
the run for investigation. Cleanup never adopts existing application buckets.

PASS establishes that the tested operations work on your current deployment.
It cannot prove pre-upgrade application data is unchanged without old checksums
or other independent evidence. No old deployment is required to run these checks.
The endpoint also cannot inspect server OS/kernel, storage mounts or systemd
settings. These need inspection on the servers themselves.

Forced pool placement, threshold crossings, healing, fault injection,
rebalance/decommission, external integrations, console login, long load tests
and replication between clusters are outside this command. The configured dev
suite is included separately and is never invoked automatically.
