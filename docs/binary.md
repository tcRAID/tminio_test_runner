# Optional binary development check

This development tool starts its own private localhost MinIO process. For the
already deployed community MinIO service, use the [endpoint command](endpoint.md).
From the repository or extracted archive root:

```bash
./scripts/run-binary.sh /absolute/path/to/minio
```

The binary must be executable. The tool uses random credentials, ports and fresh
local directories, and does not control an existing service. Linux, Python 3.10+
and approximately 4 GiB free local disk are required. The archive includes its
dependencies; a source checkout installs them in a cached venv on first use.

Optional `--work-dir /mnt/test-storage` selects an existing parent directory;
`--timeout 3600` extends the default 30-minute deadline. No lab configuration or
production credentials are required. Allow several minutes for the full dataset.

## What it checks

- Binary execution, architecture, SHA-256, MinIO version, OS/kernel, disk space and
  process limits. Records embedded Go build information when Go is available.
  Both static and dynamically linked binaries are accepted if they run; this
  checks the supplied binary, without imposing an invented Go version or hash.
- Startup, authenticated S3 access, node readiness/liveness and cluster read/write
  health. Health readiness may be polled during startup; object correctness checks
  never retry. SDK retries are disabled and recorded.
- 0 B, 1 KiB, 1 MiB, 64 MiB and 1 GiB PUT/GET/HEAD/COPY, metadata, tags, valid and
  invalid Range GET, and deletion. Downloads are checked by SHA-256.
- 100 immediate PUT/overwrite/DELETE/recreate cycles, plus batch deletion of
  existing/missing keys with per-object response checks.
- Exactly 2,105 keys: 1,000 flat, 1,000 nested, 100 Unicode and 5 special-character
  keys. Checks every downloaded object, three listing pages, delimiter grouping,
  prefix pagination, spaces, plus signs, percent signs and XML-sensitive characters.
- Two 1 GiB multipart uploads with 16 MiB parts, replacement of an already uploaded
  part, and a separate restricted STS client uploading parts. Checks part lists and
  whole-object SHA-256. An aborted upload must disappear and reject completion.
- Read-only and prefix-restricted STS sessions, allowed and forbidden operations,
  invalid credentials, anonymous access denial, and valid/altered/expired presigned
  URLs. These are newly created test permissions, not your production IAM policies.
- Versioned reads, delete markers, explicit version deletion and exact paginated
  version history.
- Current/noncurrent expiration, retention-count and marker-cleanup rule
  **configuration**. Actual age-based expiration needs longer observation and is
  explicitly not claimed by this short test. No system clock changes.
- Clean shutdown/restart of the test process, followed by checksum and metadata
  verification of every retained object, retained version IDs, delete markers,
  deleted-key absence, lifecycle rules, and a new write.
- Shutdown and server log checks for panic, fatal and common storage error signatures.

## Results

The command prints PASS or FAIL for each check and the report directory:

```text
minio-check-YYYYMMDD-HHMMSS-.../
  summary.txt        readable results and coverage limits
  results.json       binary/host/client identities and individual results
  junit.xml          CI-compatible pass/fail results
  server.log         sanitized logs from the test MinIO process
  requests.jsonl     request IDs, errors, retry counts, write intents and checksums
  objects.json       retained objects, version IDs, hashes and deleted-key manifest
```

Exit `0` means all implemented local checks passed. Exit `1` means a check failed
or execution was interrupted. Exit `2` indicates an argument/setup problem. Failures
preserve test data for inspection. A successful run removes only its own temporary
data/config directories and keeps the reports. Cleanup uses a directory ownership
marker and rejects symlinks. Normal interruption stops only the process group it
created. A machine crash or SIGKILL cannot be cleaned up automatically; its process
ID is in `requests.jsonl`, and its files remain in that run's directory.

The directory is private to your account. An unclean interruption may leave
`server.private.log`, which should be inspected/redacted before sharing. Generated
root/STS credentials and signed URLs are excluded or redacted from normal reports.
No production credentials are requested.

A PASS establishes that **this binary performed these operations on this host with
fresh data**. It does not certify your existing data, actual service configuration,
TLS/proxy routing, replication, distributed quorum/pools, healing, application
integration, long-running lifecycle processing, performance, or rollback/recovery.
Those need the real deployment. In particular, testing a newer binary with fresh
data does not establish that an older binary can safely read its metadata.

## Maintainer validation

Build the archive with `python3 scripts/package_release.py`. Run harness checks
with `./run.sh unit`; filter binary harness cases with
`./run.sh unit -- -k binary_check -v`.

Historical binary validation on 2026-11-12 passed all 13 live checks on Ubuntu
22.04.5 x86_64 and Python 3.10.12 using a local development binary.
A one-second deadline stopped its process and retained diagnostic data as designed
for this optional binary tool. These are historical results, not evidence for a
particular official community MinIO release or another host. Endpoint and harness
validation is recorded in [validation.md](validation.md).
