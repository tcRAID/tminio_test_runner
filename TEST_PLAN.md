# MinIO Test Plan

This test plan defines the validation coverage, execution procedure, expected
result, and cleanup rules for `tminio_test_runner`.

The package has two entry points:

- `minio_test_runner.py` for source validation, local smoke validation, local
  operations validation, and local fault injection validation.
- `longrun/minio_long.py` for long-running Locust validation against a
  user-supplied endpoint.

The runner validates an external MinIO source tree. It does not modify files in
the MinIO source tree.

## Validation Gate Matrix

| Change type | Validation execution |
| --- | --- |
| Small source-only change | `python3 minio_test_runner.py source --minio-dir /path/to/minio --packages ./cmd` |
| Normal code change | `python3 minio_test_runner.py source --minio-dir /path/to/minio --packages ./cmd` and `python3 minio_test_runner.py smoke --minio-dir /path/to/minio` |
| Storage, S3 API, metadata, multipart, versioning, object lock, policy, restart, ILM, purge, or disk fault handling change | `python3 minio_test_runner.py source --minio-dir /path/to/minio --race`, `python3 minio_test_runner.py smoke --minio-dir /path/to/minio`, `python3 minio_test_runner.py ops --minio-dir /path/to/minio`, and `python3 minio_test_runner.py fault --minio-dir /path/to/minio` |
| Larger or risky change | Source + smoke + ops + fault above, then `locust -f longrun/minio_long.py MinioLongUser --host "$MINIO_ENDPOINT" --headless --users 8 --spawn-rate 1 --run-time 12h` |
| Nightly validation | `python3 minio_test_runner.py source --minio-dir /path/to/minio --race --timeout 90m` and direct Locust long run |

Any non-zero exit code, Locust failure, correctness assertion failure, runner
timeout, or failed cleanup step is a validation failure.

## Scope

| Mode | Entry point | Target | Primary coverage |
| --- | --- | --- | --- |
| `source` | `minio_test_runner.py source` | MinIO source tree | Go environment capture, MinIO build, MinIO-owned Go tests, optional race detector |
| `smoke` | `minio_test_runner.py smoke` | Local MinIO built from the source tree | Single-user S3 correctness against a temporary localhost MinIO |
| `ops` | `minio_test_runner.py ops` | Local MinIO built from the source tree | Restart persistence, forced restart persistence, lifecycle config persistence, strict purge |
| `fault` | `minio_test_runner.py fault` | Local MinIO built from the source tree | Removed local drive PUT/GET failure, object-file corruption GET failure, re-added erasure drive healing |
| `long` | `locust -f longrun/minio_long.py MinioLongUser` | User-supplied MinIO/S3 endpoint | Extended mixed S3 workload with repeated correctness checks |

`source`, `smoke`, `ops`, and `fault` require only `--minio-dir` or `MINIO_DIR`.
They do not require a user-provided endpoint. `smoke`, `ops`, and `fault` start
and stop their own local MinIO.

`long` is not a `minio_test_runner.py` subcommand. It is direct Locust
execution and requires endpoint credentials.

## Host Requirements

- Python 3 with venv support, or an equivalent isolated Python environment.
- Go compatible with the MinIO source tree under test.
- MinIO Client `mc` on `PATH` for `fault` re-add/heal validation.
- Python dependencies from `minio-test-requirements.txt`.

## Source Validation

Purpose:

- Confirm the selected MinIO source tree builds.
- Run MinIO's own Go package tests.
- Optionally run the same selected tests under Go's race detector.

Command:

```bash
python3 minio_test_runner.py source --minio-dir /path/to/minio --packages ./cmd
```

Full package validation:

```bash
python3 minio_test_runner.py source --minio-dir /path/to/minio
```

Targeted race validation:

```bash
python3 minio_test_runner.py source \
  --minio-dir /path/to/minio \
  --packages ./cmd \
  --run Batch \
  --race
```

Runner steps:

| Step | Command behavior | Expected result |
| --- | --- | --- |
| Source path validation | Resolve `--minio-dir` / `MINIO_DIR` and require `go.mod` | Correct MinIO source tree is selected |
| Go environment capture | `go env` | `logs/go-env.log` records toolchain settings |
| Build | `go build -tags <build-tags> -trimpath` with MinIO linker flags when available | `work/bin/minio` is produced |
| Go tests | `go test -count=1 -timeout <timeout> -tags <test-tags> -v <packages>` | Selected package tests pass |
| Race tests | `go test -race` when `--race` is present | Race detector reports no races in executed paths |

Default source settings:

| Setting | Default |
| --- | --- |
| Packages | `./...` |
| Test tags | `kqueue,dev` |
| Build tags | `kqueue` |
| Test timeout | `60m` |
| Race timeout | `100m` |

Source coverage limits:

- Source mode does not start a live MinIO endpoint.
- Source mode does not create S3 buckets or objects.
- Source mode does not measure Go coverage percentage because it does not pass
  `go test -cover`.
- `--race` only detects races in paths exercised by the selected Go tests.
- If MinIO does not already have a Go test for a behavior, source mode will not
  cover that behavior.

## MinIO-Owned Go Test Inventory

The runner's source coverage comes from MinIO's own Go tests in the selected
source tree. For the prepared `minio/` tree inspected on this Linux host:

| Inventory item | Observed count | Notes |
| --- | ---: | --- |
| `_test.go` files in the tree | 247 | Raw file count before Go build constraints |
| Packages with `_test.go` files | 46 | Mostly `cmd` and `internal/...` packages |
| `_test.go` files under `cmd/` | 143 | Main MinIO server package tests |
| `_test.go` files under `internal/` | 104 | Library and subsystem package tests |
| `Test...` functions | 1097 | Normal tests executed by `go test` |
| `Benchmark...` functions | 872 | Compiled but not executed unless `-bench` is supplied |
| Generated `_gen_test.go` files | 33 | Mostly serialization encode/decode round-trip tests |

Major source-test themes under `cmd/`:

| Theme | Representative files or test names | What it validates |
| --- | --- | --- |
| S3 object and bucket APIs | `object-api-*`, `object-handlers-*`, `bucket-handlers*`, `api-*` | PUT/GET/HEAD/LIST/DELETE/COPY, multipart, range, metadata, handler errors |
| Authentication and request signing | `auth-handler`, `signature-v2`, `signature-v4`, `streaming-signature-v4`, `post-policy`, `sts-handlers` | Auth classification, signed headers, presigned requests, POST policy, STS/IAM paths |
| Erasure and storage internals | `erasure-*`, `xl-storage-*`, `format-erasure`, `bitrot`, `naughty-disk` | Erasure encode/decode, quorum, format metadata, disk failure behavior, bitrot |
| Healing and consistency | `erasure-heal*`, `erasure-healing*`, `xl-storage-free-version` | Healing decisions, corrupted parts/metadata, dangling objects, free versions |
| Metadata, listing, and cache | `metacache-*`, `data-usage-*`, `data-scanner` | Listing cache behavior, data usage cache, scanner state |
| Replication, site replication, and tiering | `bucket-replication*`, `site-replication*`, `tier*` | Replication state, resync status, tier stats and config |
| Batch jobs | `batch-expire*`, `batch-replicate*`, `batch-rotate*`, `batch-handlers*` | Batch config parsing, serialization, filters, handlers, helper behavior |
| Metrics and locking | `metrics-v2*`, `namespace-lock`, `local-locker*`, `lock-rest-*`, `dynamic-timeouts` | Metrics groups, namespace locks, REST lock protocol, timeout adaptation |

For a specific race concern, such as a batch metrics map race, the runner can
only find the issue if a selected MinIO Go test concurrently exercises that
path. The strongest source-level coverage for a known race is a dedicated
MinIO Go test for the exact sequence, run with:

```bash
python3 minio_test_runner.py source \
  --minio-dir /path/to/minio \
  --packages ./cmd \
  --run <TestName> \
  --race
```

## Local Smoke Validation

Purpose:

- Validate functional S3 behavior against a local MinIO built from the source
  tree under test.
- Avoid requiring a pre-existing endpoint or user-supplied endpoint settings.
- Ensure all local runtime state is cleaned after the run.

Command:

```bash
python3 minio_test_runner.py smoke --minio-dir /path/to/minio
```

Runner steps:

| Step | Behavior | Expected result |
| --- | --- | --- |
| Build local MinIO | Build source into `work/bin/minio` | Build exits `0` |
| Start local MinIO | Start `minio server <temp-drive>` on free localhost ports | `/minio/health/live` returns `200` |
| Run smoke workload | Execute `MinioSmokeUser` with one Locust user | Locust failures are `0` |
| Stop local MinIO | Terminate the process group | No local MinIO process remains |
| Remove temp drive | Delete `work/local-minio-data` | No temporary drive directory remains |

Smoke S3 coverage:

| Case | Area | What is verified |
| --- | --- | --- |
| `SMK-001` | Health endpoints | `/minio/health/live` and `/minio/health/ready` return `200` |
| `SMK-002` | Basic object API | Bucket create/head, object put/get/head, prefix list, copy |
| `SMK-003` | Metadata, tags, range, conditionals | Metadata, object tags, range GET, `If-Match`, `If-None-Match`, conditional copy |
| `SMK-004` | Multipart | Create upload, upload parts, complete, verify body hash, abort upload |
| `SMK-005` | Versioning | Enable versioning, read by version ID, delete marker, list versions |
| `SMK-006` | Presigned URL and policy | Presigned GET/PUT, bucket policy, anonymous public-read GET |
| `SMK-007` | Bucket config | Bucket tagging, lifecycle, optional CORS when implemented |
| `SMK-008` | SSE-C | HTTP local MinIO rejects SSE-C with the expected secure-transport error |
| `SMK-009` | Object lock | Governance retention, legal hold, bypass governance delete |
| `SMK-010` | Report and cleanup | Report files exist; local process and temp drive are cleaned |

Cleanup requirements:

- `local-minio-stop` must be present in `summary.md`.
- The local MinIO process must not keep listening on the selected API port.
- `work/local-minio-data` must be removed even if smoke fails.
- Passing runs remove the whole `work/` directory unless `--keep-workdir` is
  set.

## Local Operations Validation

Purpose:

- Verify that persisted object and bucket state survives clean restart.
- Verify that an object acknowledged before a forced process stop is readable
  after restart.
- Verify lifecycle, tagging, policy, versioning, and delete marker metadata
  survive restart.
- Verify purge removes versions, delete markers, current objects, bucket
  configuration, and the bucket.

Command:

```bash
python3 minio_test_runner.py ops --minio-dir /path/to/minio
```

Runner steps:

| Step | Behavior | Expected result |
| --- | --- | --- |
| Build local MinIO | Build source into `work/bin/minio` | Build exits `0` |
| Start local MinIO | Start `minio server <persistent-temp-drive>` | Health endpoint returns `200` |
| Prepare state | Create versioned bucket, objects, tags, policy, lifecycle config, and delete marker | All operations succeed |
| Clean restart | Stop MinIO, restart with same data directory | Process becomes healthy |
| Verify clean restart | Read persisted object/version/config state | State matches pre-restart values |
| Forced restart | Write probe object, kill MinIO with `SIGKILL`, restart with same data directory | Process becomes healthy |
| Verify forced restart | Read probe object | Probe body hash matches |
| Purge bucket | Delete all versions/delete markers/current objects/config, delete bucket | Bucket no longer exists |
| Final cleanup | Stop MinIO and remove temp drive | No temporary data directory remains |

Ops coverage limits:

- Lifecycle coverage validates config persistence. Background ILM expiry depends
  on scanner timing and is better covered by MinIO source tests such as
  lifecycle/scanner/expiry tests in `./cmd` and `./internal/bucket/lifecycle`.
- Local ops uses a single local filesystem drive. It does not replace erasure or
  distributed cluster validation.

## Local Fault Injection Validation

Purpose:

- Verify PUT fails while the local filesystem drive path is removed.
- Verify GET fails while the local filesystem drive path is removed.
- Verify the server process remains running during removed-drive request
  failures.
- Verify GET fails after an object file is corrupted on disk and MinIO is
  restarted.
- Verify a local erasure setup can accept a write while one drive is missing,
  then verify the object is readable and materialized on that drive after re-add
  and heal.

Command:

```bash
python3 minio_test_runner.py fault --minio-dir /path/to/minio
```

Runner steps:

| Step | Behavior | Expected result |
| --- | --- | --- |
| Build local MinIO | Build source into `work/bin/minio` | Build exits `0` |
| Start local MinIO | Start `minio server <temp-drive>` | Health endpoint returns `200` |
| Prepare state | Create bucket, baseline object, and corruption target object | All operations succeed |
| Removed drive PUT/GET | Rename the drive directory away, attempt PUT and GET, restore the drive | PUT and GET fail; process remains running |
| Verify restore | Read the baseline object after drive restore | Body hash matches original |
| Corrupt object file | Stop MinIO and flip bytes in the target object's on-disk file | File mutation succeeds |
| Corrupted GET | Restart MinIO and GET the corrupted target object | GET fails; process remains running |
| Start local erasure MinIO | Start MinIO with four temporary filesystem drives | Health endpoint returns `200` |
| Removed erasure drive PUT | Rename one erasure drive away, PUT a new object, and verify it remains readable from the remaining drives | PUT succeeds; removed drive has no object files |
| Re-add and heal | Restore the drive, run `mc admin heal`, read the object, and check the re-added drive's files | Body hash matches; `xl.meta` and data part appear on the re-added drive |
| Final cleanup | Stop MinIO and remove temp drives | No temporary data directory remains |

Fault coverage limits:

- Fault mode uses local filesystem directories as MinIO drives. The removed
  single-drive checks validate request failure and process survival.
- The heal check uses a local file-backed erasure setup. It validates re-add
  healing evidence, but it is not a substitute for a real multi-node hardware
  failure test.

## Long-Running Locust Validation

Purpose:

- Validate a user-supplied MinIO/S3 endpoint under a longer mixed workload.
- Keep endpoint ownership outside the runner.

Command:

```bash
export MINIO_ENDPOINT=https://minio.example.internal:9000
export MINIO_ACCESS_KEY=<access-key>
export MINIO_SECRET_KEY=<secret-key>

locust -f longrun/minio_long.py MinioLongUser \
  --headless \
  --host "$MINIO_ENDPOINT" \
  --users 8 \
  --spawn-rate 1 \
  --run-time 12h \
  --html longrun.html \
  --csv longrun
```

Long-run coverage:

| Case | Area | What is verified |
| --- | --- | --- |
| `LNG-001` | Startup | Each Locust user creates a bucket and enables versioning |
| `LNG-002` | PUT workload | Random object sizes with SHA-256 metadata |
| `LNG-003` | GET verification | Remembered objects are read and body hash matches metadata |
| `LNG-004` | HEAD/LIST | Repeated `HeadObject` and `ListObjectsV2` |
| `LNG-005` | COPY | Copy remembered objects and preserve metadata when available |
| `LNG-006` | DELETE | Delete remembered objects and update local memory |
| `LNG-007` | Multipart | Repeated multipart create/upload/complete |
| `LNG-008` | Bucket tagging | Repeated bucket tagging put/get |
| `LNG-009` | Cleanup | User bucket cleanup on Locust stop when `MINIO_TEST_CLEANUP=1` |

Long-run cleanup:

- Set `MINIO_TEST_CLEANUP=1` for normal validation.
- If Locust is interrupted, manually remove buckets matching
  `MINIO_TEST_BUCKET_PREFIX`.
- Long-run reports are Locust artifacts controlled by `--html` and `--csv`.

## Negative And Safety Cases

| Case | Command | Expected result |
| --- | --- | --- |
| Missing source path | `python3 minio_test_runner.py smoke --minio-dir /bad/path` | Runner fails before starting MinIO |
| Smoke users > 1 | `python3 minio_test_runner.py smoke --minio-dir /path/to/minio --users 2` | Runner rejects the command |
| Local startup failure | Use an invalid MinIO source or broken binary | Runner records failure and removes temp drive if created |
| Long missing credentials | Run Locust without `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | Locust user fails before workload starts |
| Secret redaction | Pass `--secret-key` to smoke | `report.json` redacts sensitive argv values |

## Evidence

`minio_test_runner.py` writes:

- `summary.md`
- `report.json`
- command logs under `logs/`
- Locust smoke HTML/CSV files for smoke runs

Direct long-run Locust writes artifacts requested by the operator, such as:

- `--html longrun.html`
- `--csv longrun`

## Release Acceptance

A runner release is acceptable when:

- `python3 -m py_compile minio_test_runner.py locustfiles/minio_s3.py longrun/minio_long.py` passes.
- `python3 minio_test_runner.py source --minio-dir /path/to/minio --packages ./cmd` passes.
- `python3 minio_test_runner.py smoke --minio-dir /path/to/minio` passes.
- `python3 minio_test_runner.py ops --minio-dir /path/to/minio` passes.
- `python3 minio_test_runner.py fault --minio-dir /path/to/minio` passes.
- A direct Locust import/help check for `longrun/minio_long.py` passes.
- No generated reports, Python caches, temporary local MinIO data directories,
  or local MinIO processes remain in the release tree.
