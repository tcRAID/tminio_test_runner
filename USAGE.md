# MinIO Validation Runner Execution Guide

This guide defines the execution procedure for the MinIO validation runner.
The runner validates a MinIO source release and an existing MinIO/S3 endpoint
through source build checks, Go package tests, functional S3 correctness
checks, and long-running mixed S3 workloads.

## Execution Summary

The standard validation execution prepares the runner environment, targets the
MinIO source release, targets the MinIO/S3 endpoint, then executes source and
functional validation.

```bash
cd /opt/tminio_test_runner
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r minio-test-requirements.txt

export MINIO_DIR=/opt/minio-RELEASE.2025-06-13T11-33-47Z
export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=test-access-key
export MINIO_SECRET_KEY=test-secret-key
export MINIO_REGION=us-east-1

python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py smoke
```

## Validation Profiles

| Profile | Command | Coverage |
| --- | --- | --- |
| Source package validation | `python3 minio_test_runner.py source --packages ./cmd` | MinIO build plus focused Go package tests |
| Full source validation | `python3 minio_test_runner.py source` | MinIO build plus Go package tests across `./...` |
| Functional S3 validation | `python3 minio_test_runner.py smoke` | Single-user correctness coverage across core S3 and bucket behavior |
| Long-running S3 validation | `python3 minio_test_runner.py long --duration 12h --users 8` | Extended mixed S3 workload with repeated correctness checks |

Smoke mode is intentionally single-user and deterministic. Long mode covers
concurrent and extended-duration workload behavior.

## Package Layout

The validation package contains these files and directories:

```text
tminio_test_runner/
  locustfiles/
    minio_s3.py
  minio_test_runner.py
  minio-test-requirements.txt
  README.md
  USAGE.md
  TEST_PLAN.md
  minio-testing.md
```

The MinIO source tree is external to this package. Source validation targets
the release path supplied through `MINIO_DIR` or `--minio-dir`.

## Platform Requirements

The documented execution environment is Ubuntu 22.

Base packages:

```bash
sudo apt update
sudo apt install -y python3 python3-venv git ca-certificates build-essential curl
```

Source validation requires Go compatible with the MinIO source release. For
the referenced release, `go.mod` declares:

```text
go 1.24.0
toolchain go1.24.2
```

Runtime verification:

```bash
go version
python3 --version
python3 minio_test_runner.py --help
```

## Python Runtime

The runner uses an isolated Python virtual environment.

```bash
cd /opt/tminio_test_runner
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r minio-test-requirements.txt
```

Installed runtime dependencies:

- `boto3`: S3 API client used by the Locust workload.
- `locust`: workload execution and reporting framework.

## Source Target Configuration

Set the MinIO source directory under validation:

```bash
export MINIO_DIR=/opt/minio-RELEASE.2025-06-13T11-33-47Z
```

The directory must contain `go.mod`. If `MINIO_DIR` is not set, the runner
looks for an optional co-located release directory:

```text
minio-RELEASE.2025-06-13T11-33-47Z/
```

## Endpoint And Account Configuration

Functional validation requires a reachable MinIO/S3 endpoint and a dedicated
test account.

```bash
export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=test-access-key
export MINIO_SECRET_KEY=test-secret-key
export MINIO_REGION=us-east-1
```

The account used for full functional coverage must permit:

- bucket create, list, delete, and head
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

Command-line credential flags are accepted by the runner. Values passed through
`--access-key` and `--secret-key` are redacted from `report.json`.

TLS verification is enabled by default. For endpoints using a self-signed
certificate during validation, the command can include:

```bash
python3 minio_test_runner.py smoke --no-verify-tls
```

<<<<<<< HEAD
## Source Validation

=======
When the endpoint uses plain HTTP, smoke mode verifies that SSE-C requests are
rejected with MinIO's expected secure-transport error instead of attempting the
HTTPS-only SSE-C read-back path.

## Source Validation

>>>>>>> 1bf4e1d (Align validation docs with runner behavior)
Source validation builds the MinIO server binary and runs Go tests.

```bash
python3 minio_test_runner.py source
```

Profile variants:

```bash
python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py source --packages ./cmd ./internal/...
python3 minio_test_runner.py source --run TestIAM --packages ./cmd
python3 minio_test_runner.py source --timeout 90m
python3 minio_test_runner.py source --race --packages ./cmd
```

The `source` profile executes:

- `go env`
- MinIO `go build`
- `go test -count=1 -tags kqueue,dev -v`
- `go test -race` when `--race` is present

Acceptance criteria:

- All runner steps exit with code `0`.
- `summary.md` reports `PASS`.
- `report.json` contains `"passed": true`.

## Functional Smoke Validation

Smoke validation executes a deterministic single-user S3 correctness suite
against the configured MinIO/S3 endpoint.

```bash
python3 minio_test_runner.py smoke
```

Connection settings can also be supplied explicitly:

```bash
python3 minio_test_runner.py smoke \
  --endpoint http://minio.example.internal:9000
```

Default profile:

- `--users 1`
- `--spawn-rate 1`
- `--duration 10m`
- cleanup enabled

Operational flags:

```bash
python3 minio_test_runner.py smoke --bucket-prefix validation-smoke
python3 minio_test_runner.py smoke --no-cleanup
python3 minio_test_runner.py smoke --no-verify-tls
python3 minio_test_runner.py smoke --report-dir ./reports
```

Acceptance criteria:

- Locust failure count is `0`.
- All S3 correctness assertions pass.
- `summary.md` reports `PASS`.
- `report.json` contains `"passed": true`.

## Long-Running Functional Validation

Long mode runs an extended mixed S3 workload. Each Locust user creates an
isolated test bucket, enables versioning, and continuously performs weighted
object and bucket operations with correctness checks.

```bash
python3 minio_test_runner.py long \
  --endpoint "$MINIO_ENDPOINT" \
  --duration 12h \
  --users 8 \
  --spawn-rate 1
```

Profile variants:

```bash
python3 minio_test_runner.py long --duration 2h --users 4
python3 minio_test_runner.py long --duration 12h --users 16 --spawn-rate 2
python3 minio_test_runner.py long --bucket-prefix nightly-$(date +%Y%m%d)
python3 minio_test_runner.py long --locust-arg=--loglevel --locust-arg=DEBUG
```

Acceptance criteria:

- The run completes the configured duration.
- Locust failure count is `0`.
- Object hash verification checks pass.
- `summary.md` reports `PASS`.
- `report.json` contains `"passed": true`.

## Direct Locust Execution

The Locust workload can be executed without the wrapper when an external
Locust framework owns process management.

Smoke class:

```bash
locust -f locustfiles/minio_s3.py MinioSmokeUser \
  --headless \
  --host "$MINIO_ENDPOINT" \
  --users 1 \
  --spawn-rate 1 \
  --run-time 10m
```

Long-running class:

```bash
locust -f locustfiles/minio_s3.py MinioLongUser \
  --headless \
  --host "$MINIO_ENDPOINT" \
  --users 8 \
  --spawn-rate 1 \
  --run-time 12h
```

Required environment:

```bash
export MINIO_ACCESS_KEY=test-access-key
export MINIO_SECRET_KEY=test-secret-key
export MINIO_REGION=us-east-1
```

Additional workload environment:

```bash
export MINIO_VERIFY_TLS=0
export MINIO_TEST_BUCKET_PREFIX=validation
export MINIO_TEST_CLEANUP=0
export MINIO_LONG_OBJECT_LIMIT=500
```

## Validation Evidence

The wrapper creates a timestamped report directory for every run:

```text
test-reports/
  20260521-120000-source/
    report.json
    summary.md
    logs/
  20260521-130000-smoke/
    report.json
    summary.md
    locust-smoke.html
    locust-smoke_stats.csv
    locust-smoke_failures.csv
    logs/
  20260521-220000-long/
    report.json
    summary.md
    locust-long.html
    locust-long_stats.csv
    locust-long_failures.csv
    logs/
```

Evidence files:

- `summary.md`: human-readable step summary and pass/fail result.
- `report.json`: machine-readable validation result for automation.
- `locust-*.html`: Locust HTML report.
- `locust-*_stats.csv`: latency, RPS, and failure statistics.
- `locust-*_failures.csv`: Locust failure details.
- `locust-*_exceptions.csv`: Locust exception details when emitted.
- `logs/`: command output captured by the wrapper.

Process exit codes:

- `0`: validation passed.
- non-zero: source failure, functional failure, runner error, or timeout.

## Cleanup

Functional validation creates validation buckets and attempts to delete them
during normal completion. Bucket names include the configured prefix:

```text
<bucket-prefix>-<test-kind>-<random-suffix>
```

When `--no-cleanup` is set, or when a run is interrupted, buckets with the run
prefix remain available for inspection and require manual removal.

Manual cleanup procedure:

1. Identify the bucket prefix from `report.json`.
2. List buckets matching the prefix.
3. Delete object versions, delete markers, current objects, and incomplete
   multipart uploads.
4. Remove bucket policy, lifecycle, CORS, tagging, legal hold, and retention
   configuration where present.
5. Delete the buckets.

## Troubleshooting

Missing Python module:

```text
required Python module not found: locust
```

Resolution:

```bash
. .venv/bin/activate
python3 -m pip install -r minio-test-requirements.txt
```

Missing credentials:

```text
MinIO credentials are required. Set MINIO_ACCESS_KEY and MINIO_SECRET_KEY
```

Resolution:

```bash
export MINIO_ACCESS_KEY=test-access-key
export MINIO_SECRET_KEY=test-secret-key
```

Endpoint cannot be reached:

- `MINIO_ENDPOINT` includes `http://` or `https://`.
- The validation host can reach the endpoint.
- DNS, firewall, and load balancer routing allow access.

TLS certificate error:

- Install the endpoint CA certificate in the validation host trust store.
<<<<<<< HEAD
- For isolated validation environments, add `--no-verify-tls`.
=======
- For isolated validation environments, add `--no-verify-tls`; this applies to
  boto3 calls, health checks, and presigned URL checks.
>>>>>>> 1bf4e1d (Align validation docs with runner behavior)

`AccessDenied`:

- Confirm that the test account has the permissions listed in this guide.
- The related validation case fails when the endpoint or account cannot
  exercise the required S3 feature.

Object lock validation failure:

- Confirm that the cluster supports object lock.
- Confirm that the test account has object lock, retention, legal hold, and
  governance bypass permissions.

Buckets remain after an interrupted run:

- Search for buckets using the run's `--bucket-prefix`.
- Execute the manual cleanup procedure in this guide.
