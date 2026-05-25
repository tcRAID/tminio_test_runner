# MinIO Test Plan

This test plan defines the MinIO validation coverage, execution procedure,
expected result, and cleanup for each validation case. For installation
commands and option details, see [USAGE.md](USAGE.md).

## Validation Gate Matrix

The validation suite is executed according to this matrix:

| Change type | Validation execution |
| --- | --- |
| Small source-only change | `python3 minio_test_runner.py source --packages ./cmd` |
| Normal code change | `python3 minio_test_runner.py source --packages ./cmd` and `python3 minio_test_runner.py smoke` |
| Storage, S3 API, metadata, multipart, versioning, object lock, or policy change | `python3 minio_test_runner.py source --race` and `python3 minio_test_runner.py smoke` |
| Larger or risky change | `python3 minio_test_runner.py source --race` and `python3 minio_test_runner.py long --duration 12h --users 8` |
| Nightly validation | `python3 minio_test_runner.py source --race --timeout 90m` and `python3 minio_test_runner.py long --duration 12h --users 8` |

Any non-zero exit code, Locust failure, correctness assertion failure, or
runner timeout is a validation failure and requires investigation.

## Scope

This plan covers three validation modes:

- `source`: build a MinIO source tree and run Go tests.
- `smoke`: run a single-user S3 correctness suite against an existing
  MinIO/S3 endpoint.
- `long`: run a longer mixed S3 correctness and stability workload against an
  existing MinIO/S3 endpoint.

The runner validates an existing MinIO source tree and an already-running
MinIO/S3 endpoint. Deployment, start, stop, and upgrade operations are outside
this validation package.

## Coverage At A Glance

The following matrices summarize the validated behavior before the detailed
case procedures.

### Mode Coverage Summary

| Mode | Case IDs | Primary coverage | Main pass signal |
| --- | --- | --- | --- |
| `source` | `SRC-001` to `SRC-006` | Source directory validation, Go environment capture, MinIO build, Go package tests, targeted Go tests, race tests | Go commands exit `0`; runner report is `PASS` |
| `smoke` | `SMK-001` to `SMK-010` | Single-user S3 correctness across health, bucket/object CRUD, metadata, tags, multipart, versioning, presigned URL, policy, config, SSE-C, object lock | Locust failures are `0`; all assertions pass; runner report is `PASS` |
| `long` | `LNG-001` to `LNG-009` | Long-running mixed S3 workload with repeated PUT/GET/HEAD/LIST/COPY/DELETE/multipart/tagging operations | Locust failures are `0`; hash checks pass; run completes configured duration |
| Negative/config | `NEG-001` to `NEG-003` | Missing credential handling, smoke user-count guard, sensitive argument redaction | Expected failures occur before unsafe workload behavior |

### Source And Runner Coverage Matrix

| Case | Area | Validated behavior | Validation significance |
| --- | --- | --- | --- |
| `SRC-001` | Source path validation | `MINIO_DIR` / `--minio-dir`, `go.mod` presence | Confirms the run targets the intended source release |
| `SRC-002` | Go environment capture | `go env` output and runner log capture | Preserves toolchain evidence for result traceability |
| `SRC-003` | Build | `go build`, build tags, linker flags, output binary | Confirms the source tree produces a MinIO binary |
| `SRC-004` | Go package tests | `go test` for selected packages, including `./cmd` or `./...` | Validates source-level behavior before endpoint validation |
| `SRC-005` | Targeted Go tests | `go test -run <expr>` | Validates a named source-level behavior or regression case |
| `SRC-006` | Race tests | `go test -race` with selected packages | Validates concurrency safety for higher-risk changes |
| `NEG-001` | Credential safety | Missing `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | Confirms the runner requires explicit test credentials |
| `NEG-002` | Smoke determinism | Rejects `smoke --users 2` | Preserves deterministic single-user smoke execution |
| `NEG-003` | Report security | Redacts `--access-key` and `--secret-key` in `report.json` | Confirms credentials are not exposed in validation evidence |

### Smoke Functional Coverage Matrix

| Case | S3/API area | What is verified | Required cluster/account capability |
| --- | --- | --- | --- |
| `SMK-001` | Health endpoints | `/minio/health/live` and `/minio/health/ready` return `200` | Endpoint reachability and health probes |
| `SMK-002` | Basic bucket/object CRUD | Create/head bucket, put/get/head object, prefix list, copy object | Bucket CRUD and object CRUD |
| `SMK-003` | Metadata, tags, range, conditionals | User metadata, object tags, range GET, `If-Match`, `If-None-Match`, conditional copy | Object tagging and conditional object access |
| `SMK-004` | Multipart upload | Create upload, upload parts, complete, read-back hash, abort upload | Multipart upload lifecycle |
| `SMK-005` | Versioning | Enable versioning, read by version ID, delete marker creation, list versions | Bucket versioning and versioned object operations |
| `SMK-006` | Presigned URL and policy | Presigned GET, presigned PUT, public-read bucket policy, anonymous GET | Presigned URL support and bucket policy updates |
| `SMK-007` | Bucket configuration | Bucket tagging, lifecycle put/get, and CORS when implemented | Bucket tagging, lifecycle, and optional CORS permissions |
| `SMK-008` | SSE-C | HTTPS: SSE-C put/get succeeds and access without customer key is rejected. HTTP: SSE-C is rejected as requiring secure transport. | SSE-C object access or secure-transport rejection |
| `SMK-009` | Object lock | Object-lock bucket, governance retention, legal hold, bypass governance delete | Object lock, retention, legal hold, governance bypass |
| `SMK-010` | Smoke report | Exit code, `summary.md`, `report.json`, Locust HTML/CSV output, secret redaction | Runner reporting and evidence generation |

### Long-Running Coverage Matrix

| Case | Workload area | What is covered | Validation signal |
| --- | --- | --- | --- |
| `LNG-001` | User bucket setup | Per-user bucket creation and versioning enablement | No Locust setup failures |
| `LNG-002` | PUT workload | Random object sizes and SHA-256 metadata | `PutObject.long` has no unexpected failures |
| `LNG-003` | GET verification | Read remembered objects and compare body hash to metadata | `long-get-sha256` checks pass |
| `LNG-004` | HEAD/LIST workload | Metadata lookup and prefix listing during object churn | No unexpected HEAD/LIST failures |
| `LNG-005` | COPY workload | Server-side copy and metadata availability after copy | Copy/head succeeds; copied keys can be re-read |
| `LNG-006` | DELETE workload | Delete remembered objects in versioned buckets | Delete succeeds; deleted keys are removed from future checks |
| `LNG-007` | Multipart workload | Repeated multipart upload and later hash validation | Multipart operations succeed; completed objects are remembered |
| `LNG-008` | Bucket tagging workload | Repeated bucket tag update/read during object workload | Put/get bucket tagging succeeds |
| `LNG-009` | Long run report | Duration completion, Locust reports, failure count, latency/RPS review | Runner report is `PASS`; Locust failures are `0` |

### Validation Boundaries

| Area | Status | Separate validation artifact |
| --- | --- | --- |
| MinIO deployment, start, stop, or upgrade | Outside this validation package | Deployment or upgrade procedure with platform automation evidence |
| Distributed healing and decommission | Outside this validation package | Upstream MinIO or environment-specific healing/decommission validation |
| Site replication | Outside this validation package | Multi-site replication validation plan |
| ILM transition to external tier | Outside this validation package | Tier target setup and ILM transition verification |
| External IAM providers such as LDAP/OIDC | Outside this validation package | Identity-provider setup and authentication flow validation |
| KMS-backed SSE-S3/SSE-KMS | Outside this validation package | KMS setup, key policy, encryption, and read-back validation |
| Bucket notifications and audit log pipelines | Outside this validation package | External sink setup and delivery validation |
| Formal performance/capacity benchmarking | Outside this validation package | Dedicated benchmark plan with baseline and acceptance thresholds |

## Common Pre-Config

Complete this setup before running any case.

### Tool Host Setup

Purpose:

- Prepare a repeatable host environment for the runner and Locust workload.

Setup:

```bash
cd /opt/tminio_test_runner
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r minio-test-requirements.txt
```

Expected result:

- `python3` is available.
- `boto3` and `locust` are installed in the active virtual environment.
- `python3 minio_test_runner.py --help` prints `source`, `smoke`, and `long`.

Cleanup:

- Remove `.venv/` only when the local runner environment is no longer needed.

### Source Test Setup

Purpose:

- Point the runner at the MinIO source tree under test.

Setup:

```bash
export MINIO_DIR=/opt/minio-RELEASE.2025-06-13T11-33-47Z
go version
```

Expected result:

- `$MINIO_DIR/go.mod` exists.
- Go version is compatible with the source tree under test.

Cleanup:

- No source setup cleanup is required.
- Build outputs created by this runner are written below `test-reports/<run>/work`
  and are deleted automatically after a passing run unless `--keep-workdir` is
  used.

### Functional Test Setup

Purpose:

- Provide the endpoint, account, and permissions needed by smoke and long
  tests.

Setup:

```bash
export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=test-access-key
export MINIO_SECRET_KEY=test-secret-key
export MINIO_REGION=us-east-1
```

Required account permissions:

- bucket create, delete, list, and head
- object put, get, head, delete, list, and copy
- multipart upload create, upload part, complete, and abort
- object tagging
- bucket tagging
- bucket policy
- bucket CORS when the target endpoint implements the CORS API
- bucket lifecycle
- bucket versioning
- object lock governance and legal hold
- SSE-C put, get, and head for HTTPS endpoints

Expected result:

- The test host can reach `$MINIO_ENDPOINT`.
- Credentials belong to a dedicated test account, not production root
  credentials.
- The account has the required permissions for full smoke coverage.

Cleanup:

- Functional tests create validation buckets with this pattern:

```text
<bucket-prefix>-<test-kind>-<random-suffix>
```

- By default, the runner cleans up test buckets and objects.
- `--bucket-prefix <value>` assigns a traceable prefix for run artifacts.
- `--no-cleanup` preserves validation data for post-run inspection.

## Cleanup Policy

Automatic cleanup is part of each functional case. The locustfile attempts to:

- abort multipart uploads;
- delete object versions and delete markers;
- delete current objects;
- remove bucket policy, lifecycle, CORS, and tagging config;
- delete the test bucket.

Manual cleanup is needed when the runner host is killed, the cluster is
interrupted, permissions are incomplete, or `--no-cleanup` is used.

Manual cleanup steps:

1. Identify the run's bucket prefix from `test-reports/<run>/report.json`.
2. List buckets that start with the prefix.
3. Delete all objects, object versions, delete markers, and incomplete
   multipart uploads.
4. Remove bucket policy, lifecycle, CORS, tagging, legal hold, and retention
   configuration when present.
5. Delete the buckets.

## Source Test Cases

### SRC-001: Source Directory Validation

Purpose:

- Confirm the runner is pointed at a valid MinIO source tree before spending
  time on build or test commands.

Pre-config/setup:

- Complete [Tool Host Setup](#tool-host-setup).
- Set `MINIO_DIR` or pass `--minio-dir`.

Test steps:

```bash
python3 minio_test_runner.py source --skip-build --skip-tests
```

Expected result:

- The runner resolves the MinIO source directory.
- The runner confirms `go.mod` exists.
- The command exits with code `0` if the directory and Go tool are valid.
- A `test-reports/<timestamp>-source/report.json` and `summary.md` are created.

Cleanup:

- No MinIO source cleanup is required.
- Generated report directories remain as validation evidence until the run no
  longer requires retention.

### SRC-002: Go Environment Capture

Purpose:

- Record the Go toolchain and environment used for the test run so failures can
  be reproduced.

Pre-config/setup:

- Complete [Source Test Setup](#source-test-setup).

Test steps:

```bash
python3 minio_test_runner.py source --skip-build --skip-tests
```

Expected result:

- The `go-env` step passes.
- `logs/go-env.log` contains `go env` output.
- `summary.md` marks `go-env` as `passed`.

Cleanup:

- No cluster cleanup is required.
- Keep the report if the Go environment needs to be attached to a defect.

### SRC-003: MinIO Build

Purpose:

- Verify the MinIO source tree can produce a server binary with the configured
  build tags and linker flags.

Pre-config/setup:

- Complete [Source Test Setup](#source-test-setup).

Test steps:

```bash
python3 minio_test_runner.py source --skip-tests
```

Expected result:

- `go-build-minio` exits with code `0`.
- The built binary is created under `test-reports/<run>/work/bin/minio`.
- `summary.md` marks `go-env` and `go-build-minio` as `passed`.

Cleanup:

- On passing runs, `work/` is removed automatically unless `--keep-workdir` is
  used.
- On failing runs, inspect `logs/go-build-minio.log`, then remove the report
  directory when it is no longer needed.

### SRC-004: Go Package Tests

Purpose:

- Validate source-level correctness through MinIO's Go package tests.

Pre-config/setup:

- Complete [Source Test Setup](#source-test-setup).
- Package scope is selected by validation target. `./cmd` covers the focused
  command package path; `./...` covers the full source tree.

Test steps:

```bash
python3 minio_test_runner.py source --packages ./cmd
```

For full package coverage:

```bash
python3 minio_test_runner.py source --packages ./...
```

Expected result:

- `go-build-minio` exits with code `0`.
- `go-test` exits with code `0`.
- `summary.md` marks `go-env`, `go-build-minio`, and `go-test` as `passed`.
- `logs/go-test.log` contains no failing package or failing test.

Cleanup:

- No cluster cleanup is required.
- Keep `logs/go-test.log` for failed test triage.

### SRC-005: Targeted Go Test

Purpose:

- Validate a specific Go test or test group by name or regular expression.

Pre-config/setup:

- Complete [Source Test Setup](#source-test-setup).
- Know the target test name or regular expression.

Test steps:

```bash
python3 minio_test_runner.py source --run TestIAM --packages ./cmd
```

Expected result:

- Only matching tests in the selected packages run.
- The command exits with code `0` when the targeted test passes.
- `logs/go-test.log` includes the selected `-run` expression.

Cleanup:

- No cluster cleanup is required.
- Keep the report when attaching targeted reproduction evidence.

### SRC-006: Race Test

Purpose:

- Detect data races in Go tests for higher-risk changes.

Pre-config/setup:

- Complete [Source Test Setup](#source-test-setup).
- Ensure the host has enough CPU and memory for race-enabled tests.

Test steps:

```bash
python3 minio_test_runner.py source --race --packages ./cmd
```

Expected result:

- Normal build and Go tests pass.
- `go-test-race` exits with code `0`.
- `logs/go-test-race.log` contains no race detector warning.

Cleanup:

- No cluster cleanup is required.
- Race reports are retained as concurrency defect evidence.

## Functional Smoke Test Cases

Run smoke cases with:

```bash
python3 minio_test_runner.py smoke --bucket-prefix smoke-$(date +%Y%m%d%H%M%S)
```

Smoke mode runs one user and intentionally rejects `--users` values other than
`1`. The cases below are executed by `MinioSmokeUser`.

### SMK-001: Health Endpoints

Purpose:

- Confirm the target MinIO endpoint is reachable and reports live/ready status
  before deeper S3 behavior is tested.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Ensure `$MINIO_ENDPOINT/minio/health/live` and
  `$MINIO_ENDPOINT/minio/health/ready` are reachable from the test host.

Test steps:

1. Start `smoke` mode.
2. The runner requests `/minio/health/live`.
3. The runner requests `/minio/health/ready`.
4. Locust records HTTP latency and assertion checks.

Expected result:

- Both endpoints return HTTP `200`.
- Locust records no failure for health checks.

Cleanup:

- No bucket cleanup is required for this case.

### SMK-002: Basic Bucket and Object CRUD

Purpose:

- Validate the most common S3 path: bucket creation, object write/read/head,
  prefix listing, and server-side copy.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Test account must allow bucket CRUD and object CRUD.

Test steps:

1. Create a validation bucket with the configured bucket prefix.
2. Run `HeadBucket`.
3. Put `docs/readme.txt` with SHA-256 metadata.
4. Get the object and compare the body.
5. Head the object and verify metadata.
6. Put ten objects under `prefix/`.
7. List `prefix/` and verify ten objects are returned.
8. Copy the original object to `copies/readme-copy.txt`.
9. Get the copied object and compare the body.

Expected result:

- Bucket creation and head pass.
- Uploaded body matches downloaded body.
- Metadata value is preserved.
- Prefix listing returns exactly ten batch objects.
- Copied object body matches the original.

Cleanup:

- Delete copied and original objects.
- Delete batch objects under `prefix/`.
- Delete the validation bucket.

### SMK-003: Metadata, Tags, Range, and Conditionals

Purpose:

- Validate object metadata, object tagging, range reads, and conditional object
  access semantics.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Test account must allow object tagging and conditional object access.

Test steps:

1. Create a validation bucket.
2. Put `objects/ranged.bin` with metadata and object tags.
3. Get byte range `5-10`.
4. Read object tags.
5. Head the object and capture the ETag.
6. Get the object with `If-Match` using the captured ETag.
7. Get the object with `If-None-Match` using the captured ETag.
8. Copy the object with `CopySourceIfMatch`.

Expected result:

- Range read returns the expected bytes.
- Expected object tag is present.
- `If-Match` returns the full object body.
- `If-None-Match` returns `304` or `NotModified`.
- Conditional copy succeeds.

Cleanup:

- Delete copied object.
- Delete original object.
- Delete the validation bucket.

### SMK-004: Multipart Upload

Purpose:

- Validate multipart create, upload part, complete, read-back correctness, and
  abort behavior.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Test account must allow multipart upload lifecycle operations.

Test steps:

1. Create a validation bucket.
2. Create multipart upload for `large/multipart.bin`.
3. Upload a 6 MiB part and a 1 MiB part.
4. Complete the multipart upload.
5. Download the completed object.
6. Verify SHA-256 of the downloaded object.
7. Create a second multipart upload for `large/abort.bin`.
8. Upload one part.
9. Abort the second multipart upload.

Expected result:

- Completed multipart object is readable.
- Downloaded payload hash matches the expected hash.
- Abort operation succeeds and leaves no completed object for the aborted key.

Cleanup:

- Abort any incomplete multipart uploads.
- Delete `large/multipart.bin`.
- Delete the validation bucket.

### SMK-005: Versioning and Delete Markers

Purpose:

- Validate versioning enablement, read by version ID, and delete-marker
  creation.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Test account must allow bucket versioning and versioned object operations.

Test steps:

1. Create a validation bucket.
2. Enable bucket versioning.
3. Put version one of `versioned/object.txt`.
4. Put version two of the same key.
5. Read version one by `VersionId`.
6. Delete the key without a version ID.
7. List object versions for the key.

Expected result:

- Versioning configuration is accepted.
- Reading version one returns the version-one body.
- Delete without version ID creates a delete marker.
- Version listing shows at least two versions and at least one delete marker.

Cleanup:

- Delete all versions and delete markers.
- Delete the validation bucket.

### SMK-006: Presigned URL and Bucket Policy

Purpose:

- Validate signed URL access and bucket policy based anonymous read behavior.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Test account must allow presigned operations, object put/get, and bucket
  policy updates.
- The test host must be able to access the endpoint URL used inside generated
  presigned URLs.

Test steps:

1. Create a validation bucket.
2. Put `public/hello.txt`.
3. Generate a presigned GET URL.
4. Download the object through the presigned GET URL.
5. Generate a presigned PUT URL for `uploads/presigned-put.txt`.
6. Upload through the presigned PUT URL.
7. Read back the uploaded object through S3.
8. Apply a bucket policy allowing anonymous `GetObject` on `public/*`.
9. Read `public/hello.txt` anonymously through HTTP.

Expected result:

- Presigned GET returns the original body.
- Presigned PUT stores the expected body.
- S3 read-back of the presigned PUT object matches the uploaded body.
- Anonymous GET allowed by bucket policy returns the expected public object
  body.

Cleanup:

- Remove bucket policy.
- Delete objects under `public/` and `uploads/`.
- Delete the validation bucket.

### SMK-007: Bucket Configuration

Purpose:

- Validate bucket-level configuration APIs used by applications and operators:
  tagging, lifecycle, and CORS when the target endpoint implements CORS.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Test account must allow bucket tagging and lifecycle operations.
- Test account must allow CORS operations when the endpoint implements CORS.

Test steps:

1. Create a validation bucket.
2. Put bucket tags.
3. Read bucket tags and verify expected tag.
4. Put bucket CORS configuration.
5. If CORS is implemented, read bucket CORS configuration.
6. Put lifecycle configuration for `tmp/`.
7. Read lifecycle configuration.

Expected result:

- Bucket tag set includes the expected tag.
- CORS configuration contains one rule, or the endpoint returns `501` or
  `NotImplemented`.
- Lifecycle configuration contains one enabled rule.

Cleanup:

- Delete lifecycle configuration.
- Delete CORS configuration.
- Delete bucket tags.
- Delete the validation bucket.

### SMK-008: SSE-C Object Access

Purpose:

- Validate SSE-C encrypted object write/read behavior and confirm access
  without the customer key is rejected on HTTPS endpoints. On plain HTTP
  endpoints, validate that MinIO rejects SSE-C because secure transport is
  required.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- HTTPS endpoints: test account must allow SSE-C put, get, and head.

Test steps:

1. Create a validation bucket.
2. Generate a 256-bit customer key.
3. Put `encrypted/customer-key.bin` with SSE-C headers.
4. On HTTP endpoints, confirm the put is rejected with `400` or
   `InvalidRequest`.
5. On HTTPS endpoints, get the object with the same SSE-C key.
6. On HTTPS endpoints, compare the downloaded body.
7. On HTTPS endpoints, attempt `HeadObject` without the SSE-C key.

Expected result:

- HTTP endpoints reject SSE-C with `400` or `InvalidRequest`.
- HTTPS endpoints accept SSE-C put/get.
- HTTPS endpoints reject head without the customer key with `400` or
  `InvalidRequest`.

Cleanup:

- Delete the encrypted object during bucket cleanup.
- Delete the validation bucket.

### SMK-009: Object Lock Governance and Legal Hold

Purpose:

- Validate object lock bucket creation, governance retention, legal hold, and
  bypass governance delete behavior.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Target cluster must support object lock.
- Test account must allow object lock, retention, legal hold, and bypass
  governance retention.

Test steps:

1. Create a validation bucket with object lock enabled.
2. Put `locked/object.txt` with governance retention until tomorrow.
3. Read object retention.
4. Enable legal hold.
5. Read legal hold status.
6. Disable legal hold.
7. Delete the object version with `BypassGovernanceRetention=True`.

Expected result:

- Object lock enabled bucket is created.
- Retention mode is `GOVERNANCE`.
- Legal hold is enabled and read as `ON`.
- Legal hold is disabled.
- Version delete with governance bypass succeeds.

Cleanup:

- Disable legal hold if still enabled.
- Delete retained versions with governance bypass where required.
- Delete all versions and delete markers.
- Delete the validation bucket.

### SMK-010: Smoke Runner Result and Report

Purpose:

- Confirm the wrapper returns a clean pass/fail signal and creates usable
  evidence for CI or manual review.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Assign a unique bucket prefix for the run.

Test steps:

```bash
python3 minio_test_runner.py smoke --bucket-prefix smoke-$(date +%Y%m%d%H%M%S)
```

Expected result:

- Process exits with code `0`.
- `summary.md` result is `PASS`.
- `report.json` has `"passed": true`.
- Locust failures are `0`.
- `locust-smoke.html` and CSV report files are created.
- Sensitive CLI arguments are redacted in `report.json`.

Cleanup:

- Confirm no buckets with the run prefix remain.
- If buckets remain, perform [Cleanup Policy](#cleanup-policy).

## Functional Long-Running Test Cases

Run long mode with a unique bucket prefix:

```bash
python3 minio_test_runner.py long \
  --bucket-prefix long-$(date +%Y%m%d%H%M%S) \
  --duration 12h \
  --users 8 \
  --spawn-rate 1
```

The long-running workload is probabilistic and weighted. Validate the final
report and failure CSV instead of expecting a fixed operation count.

### LNG-001: Long User Bucket Setup

Purpose:

- Confirm each Locust user can create an isolated versioned bucket for the
  long-running workload.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Choose `--users` according to the target cluster and test host capacity.

Test steps:

1. Start long mode.
2. Each user creates a bucket with the configured prefix.
3. Each user enables bucket versioning.
4. Locust starts weighted S3 tasks.

Expected result:

- Every started user creates exactly one workload bucket.
- Versioning enablement succeeds for each bucket.
- No setup failures appear in Locust failures or exceptions.

Cleanup:

- On normal stop, each user runs bucket cleanup.
- If the run is interrupted, manually remove buckets with the long-run prefix.

### LNG-002: Weighted PUT Object Workload

Purpose:

- Continuously validate object creation across multiple payload sizes while
  storing SHA-256 metadata for later read-back checks.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Long mode must be running.

Test steps:

1. Locust chooses the weighted `put_object` task.
2. The user creates a random body of size `0`, `1`, `128`, `4096`, `65536`, or
   `1048576` bytes.
3. The user uploads the object with SHA-256 metadata.
4. The key and hash are remembered up to `MINIO_LONG_OBJECT_LIMIT`.

Expected result:

- PUT requests succeed.
- Object metadata includes the expected SHA-256.
- Locust records no unexpected `PutObject.long` failures.

Cleanup:

- Objects are removed when the user's bucket is cleaned up.

### LNG-003: Weighted GET Object Verification

Purpose:

- Detect intermittent read-after-write, data corruption, or metadata mismatch
  issues over a long run.

Pre-config/setup:

- Long mode must have remembered at least one uploaded key.

Test steps:

1. Locust chooses a remembered key.
2. The user downloads the object.
3. The user reads SHA-256 metadata.
4. The user hashes the downloaded body.
5. The user compares actual hash to expected metadata.

Expected result:

- GET requests succeed.
- Hash comparison passes whenever metadata is present.
- No `long-get-sha256` check failures are recorded.

Cleanup:

- Objects are removed when the user's bucket is cleaned up.

### LNG-004: Weighted HEAD and LIST Workload

Purpose:

- Validate metadata lookup and prefix listing behavior during concurrent object
  churn.

Pre-config/setup:

- Long mode must be running.
- HEAD coverage requires at least one remembered key.

Test steps:

1. Locust periodically runs `HeadObject.long` for remembered keys.
2. Locust periodically runs `ListObjectsV2.long` for prefix `load/`.
3. Locust records latency and failures for both operations.

Expected result:

- HEAD succeeds for remembered live keys.
- LIST succeeds and returns a valid S3 response.
- No unexpected HEAD or LIST failures are recorded.

Cleanup:

- Objects and bucket are removed by long user cleanup.

### LNG-005: Weighted COPY Workload

Purpose:

- Validate server-side copy and metadata preservation during mixed workload
  traffic.

Pre-config/setup:

- Long mode must have remembered at least one uploaded key.

Test steps:

1. Locust chooses a remembered source key.
2. The user copies it to `copy/<uuid>.bin`.
3. The user heads the copied object.
4. If SHA-256 metadata exists, the copied key is remembered for later reads.

Expected result:

- Copy succeeds.
- Copied object accepts `HeadObject`.
- Metadata is available for future verification when preserved by the endpoint.
- No unexpected `CopyObject.long` failures are recorded.

Cleanup:

- Copied objects are removed when the user's bucket is cleaned up.

### LNG-006: Weighted DELETE Workload

Purpose:

- Validate object delete behavior in a versioned bucket while the workload
  continues to create and read objects.

Pre-config/setup:

- Long mode must have remembered at least one uploaded key.

Test steps:

1. Locust chooses a remembered key.
2. The user deletes the object.
3. The key is removed from the in-memory remembered-key map.

Expected result:

- Delete request succeeds.
- Deleted key is not selected for future read verification by that user.
- No unexpected `DeleteObject.long` failures are recorded.

Cleanup:

- Delete markers and object versions are removed during bucket cleanup.

### LNG-007: Weighted Multipart Workload

Purpose:

- Validate multipart upload correctness repeatedly during long-running cluster
  activity.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Long mode must be running.

Test steps:

1. Locust creates multipart upload for `multipart/<uuid>.bin`.
2. The user uploads a 5 MiB first part.
3. The user uploads a second random-size part.
4. The user completes the multipart upload.
5. The completed key and expected SHA-256 are remembered.

Expected result:

- Multipart create, upload part, and complete operations succeed.
- Completed object is eligible for later GET hash verification.
- No unexpected multipart failures are recorded.

Cleanup:

- Completed multipart objects are removed during bucket cleanup.
- Incomplete multipart uploads are aborted during cleanup.

### LNG-008: Weighted Bucket Tagging Workload

Purpose:

- Validate bucket configuration update/read behavior during object workload
  activity.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Test account must allow bucket tagging.

Test steps:

1. Locust updates the user's bucket tag `last_update`.
2. Locust reads the bucket tagging configuration.
3. Locust records latency and failures.

Expected result:

- Put bucket tagging succeeds.
- Get bucket tagging succeeds.
- No unexpected bucket tagging failures are recorded.

Cleanup:

- Bucket tags are removed when the bucket is deleted during cleanup.

### LNG-009: Long Runner Completion and Report

Purpose:

- Confirm a long run produces reliable pass/fail evidence and useful metrics.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).
- Select duration, user count, and spawn rate appropriate for the environment.

Test steps:

```bash
python3 minio_test_runner.py long \
  --bucket-prefix long-$(date +%Y%m%d%H%M%S) \
  --duration 12h \
  --users 8 \
  --spawn-rate 1
```

Expected result:

- Process exits with code `0`.
- `summary.md` result is `PASS`.
- `report.json` has `"passed": true`.
- Locust failure count is `0`.
- `locust-long.html`, stats CSV, failures CSV, and exceptions CSV are created
  when emitted by Locust.
- RPS and latency remain within the team's accepted baseline for the selected
  environment.

Cleanup:

- Confirm all buckets with the long-run prefix are removed.
- If the run was interrupted or cleanup failed, perform [Cleanup Policy](#cleanup-policy).

## Negative and Configuration Cases

### NEG-001: Missing Credentials

Purpose:

- Confirm the runner requires explicit credentials and fails with a clear
  message when credentials are absent.

Pre-config/setup:

- Unset credentials:

```bash
unset MINIO_ACCESS_KEY
unset MINIO_SECRET_KEY
```

Test steps:

```bash
python3 minio_test_runner.py smoke
```

Expected result:

- Process exits non-zero.
- Report contains an error explaining that `MINIO_ACCESS_KEY` and
  `MINIO_SECRET_KEY` are required.
- No S3 workload starts.

Cleanup:

- Delete the generated failed report if it is not needed.
- Restore credential environment variables before running functional tests.

### NEG-002: Smoke Rejects Multiple Users

Purpose:

- Confirm smoke mode remains a deterministic single-user correctness check and
  cannot stop a multi-user run early.

Pre-config/setup:

- Complete [Functional Test Setup](#functional-test-setup).

Test steps:

```bash
python3 minio_test_runner.py smoke --users 2
```

Expected result:

- Process exits non-zero before Locust starts.
- Report explains that smoke mode requires `--users 1`.
- No test bucket is created.

Cleanup:

- Delete the generated failed report if it is not needed.

### NEG-003: Sensitive Argument Redaction

Purpose:

- Confirm command-line credentials are not written in clear text to
  `report.json`.

Pre-config/setup:

- Supply non-production dummy credentials or a disposable test account.

Test steps:

```bash
python3 minio_test_runner.py smoke \
  --access-key dummy-access \
  --secret-key dummy-secret \
  --users 2
```

Expected result:

- Process exits non-zero due to the `--users 2` rejection.
- `report.json` contains `<redacted>` for both credential values.
- Raw credential values do not appear in `report.json`.

Cleanup:

- Delete the generated failed report.

## Reports and Metrics

Wrapper reports:

- `summary.md`: human-readable summary of each runner step.
- `report.json`: machine-readable result for CI.
- `logs/`: command output captured by the wrapper.

Locust reports:

- `locust-smoke.html` or `locust-long.html`
- `locust-*_stats.csv`
- `locust-*_stats_history.csv`
- `locust-*_failures.csv`
- `locust-*_exceptions.csv`

Pass criteria:

- Runner process exits with code `0`.
- `report.json` has `"passed": true`.
- Locust failure count is `0`.
- All explicit correctness checks pass.
- Automatic cleanup is attempted. Remaining validation buckets are remediated
  through [Cleanup Policy](#cleanup-policy).

Failure triage:

1. Read `summary.md` for the failed step.
2. Inspect the step log in `logs/`.
3. Inspect Locust failures and exceptions CSV files.
4. Check MinIO server logs for corresponding API errors, drive issues,
   healing activity, timeout, or 5xx responses.
5. Preserve the report directory when opening a defect.

## Validation Boundaries And Assumptions

Functional tests write data to the target cluster. The validation environment
is a test tenant or test cluster.

The long-running user count is not a formal load-test configuration. `--users 8`
is a starting point for correctness and stability validation, not a capacity
statement.

This validation package does not include:

- deploying or upgrading MinIO;
- full upstream MinIO CI replacement;
- distributed healing correctness;
- site replication;
- ILM transition to an external tier;
- external IAM providers;
- KMS-backed SSE-S3/SSE-KMS;
- bucket notification delivery;
- audit log pipelines;
- formal performance benchmarking.

Changes in these areas require a dedicated validation case or the relevant
upstream MinIO CI target.
