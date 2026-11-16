# Check a MinIO binary on Ubuntu

Run this on the **Ubuntu 26.04 machine**, passing the binary you intend to use:

```bash
python3 suite/run.py binary /absolute/path/to/minio
```

That is the normal command. No configuration file, credentials, mc, Go installation,
existing MinIO server, or deployment hooks are required. You can run the same
command on Ubuntu 22.04 first and compare the recorded binary SHA-256 and results.
Copy the `suite` directory to use it outside the repository. From inside
that directory, use `python3 run.py binary /path/to/minio`.

For an internal node without internet, use the **release test package** instead:

```bash
tar -xzf tminio_test_runner.tar.gz
cd tminio_test_runner
./run-binary.sh /absolute/path/to/minio
```

That package includes its Python dependencies and checks their hashes before
running. It needs Python 3.10+ and 4 GiB free disk; pip, venv and internet are not
needed. It does not include a MinIO binary or local credentials/configuration.
`./run-dev.sh unit` runs the bundled harness tests. The regular checkout
instructions below describe the online bootstrap; use the package's `run-binary.sh`
for the self-contained binary path. The package's default `run.sh` now
tests an already-running endpoint; see [ENDPOINT_CHECK.md](ENDPOINT_CHECK.md).

The script starts its own MinIO on a random **127.0.0.1** port with random credentials
and fresh test data. It does not connect to, stop, upgrade, or change your existing
MinIO service. It uses your current Linux account. Run it as the intended service
account if you also want to exercise that account's filesystem permissions.
The child process disables MinIO's console and automatic update check.

Needs Python 3.10+, `python3-venv`, approximately **4 GiB free disk**, and internet
access on the first run to install pinned boto3/botocore into a private cached
virtual environment. It does not install packages into the system Python. On a
minimal Ubuntu installation, install the prerequisites once:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv
```

Allow several minutes, depending on the disks and CPU. The default overall deadline
is 30 minutes. The test writes/reads several GiB, including two 1 GiB multipart
uploads; it is a correctness test, not a performance benchmark.

To exercise a particular filesystem, supply its existing parent directory. The
script creates a new child directory there; it never opens an existing MinIO data
directory:

```bash
python3 suite/run.py binary /absolute/path/to/minio --work-dir /mnt/test-storage
```

For a slower machine, append `--timeout 3600`. To install dependencies in advance
for a host without internet, prepare a Python environment with `requirements-binary.txt`, then run
`python binary_check.py /absolute/path/to/minio` using that environment.

The binary mode is separate from the configured cluster groups. `python3 run.py`
without arguments still runs the existing dev cluster release suite and requires
its configuration. `python3 run.py binary ...` runs only the isolated binary
checks, with its own JSON/JUnit reports; it does not read `config.local.json` or
invoke cluster fault/recovery hooks. The original `suite/run.sh`
command remains a shortcut. The binary mode is not added to the default cluster
run, because it needs a binary path and cannot validate the configured topology.

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

To rebuild the portable archive on a machine with pip and internet access:

```bash
python3 suite/package_release.py
```

The archive and companion SHA-256 file are written to `dist/`. Packaging uses an
explicit source-file allowlist and pinned pure-Python libraries, plus stable-ABI
psutil wheels for Linux x86_64 and aarch64. It excludes local configuration,
credentials, virtual environments, logs and test data. Existing output files are
never overwritten; use `--output /new/path/name.tar.gz` for another build. To
reuse dependencies without downloading, add `--vendor-from /path/to/extracted-package`;
the builder verifies the previous manifest and current exact dependency versions.

The safety/reporting tests are included in the existing `unit` group
(after installing `suite/requirements.txt`). They use Python's standard
`unittest` framework and botocore's request stubs; no MinIO is contacted:

```bash
python3 suite/run.py unit
# Or only the binary-check harness tests:
python3 -m unittest discover -s suite -p 'test_binary_check_unit.py' -v
```

Validation performed on 2026-11-12:

- 18 harness tests passed, including isolation, per-key delete errors, pagination,
  redaction, nonzero/JUnit failure reporting, shutdown and deadline propagation.
- After integration, `python3 suite/run.py unit` passed all **138**
  harness tests. The new `run.py binary ...` command also passed all 13 live
  checks on the same local binary. Dependency-free dispatch, argument forwarding,
  missing-binary rejection and the original launcher shortcut were checked.
- All 13 live checks passed on **Ubuntu 22.04.5, x86_64**, with Python 3.10.12 and
  boto3/botocore 1.44.1, against a locally available development MinIO binary.
  This exercised the full sizes and counts above, including both 1 GiB multipart
  uploads; the local run took roughly 40 seconds on this host.
- The tested binary reported `DEVELOPMENT.GOGET`, Go 1.25.11, with SHA-256
  `8ed3120b33a31c1eb7879a84e211c989f479d0704926eba817a7a73ce6fb7377`.
  It was already present locally, not newly built from this checkout.
- A real one-second deadline run failed as expected, stopped its MinIO process,
  and retained its test data. A separate launcher run installed dependencies in
  an isolated environment and exercised the same full suite.
- **Ubuntu 26.04 was not available here and has not been validated.** Run the
  command there with your actual candidate binary before relying on the result.

The supplied AI-generated Task 2/4 plan informed the datasets and assertions. This
one-command runner implements the user's subsequently simplified binary-runtime
check; it does not represent completion of all 23 operational plan cases.
