# MinIO Test Tool Usage Guide

## Read This First

If you only need the normal workflow, do this:

```bash
cd /path/to/tminio_test_runner
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r minio-test-requirements.txt

export MINIO_DIR=/path/to/minio-RELEASE.2025-06-13T11-33-47Z
export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=your-access-key
export MINIO_SECRET_KEY=your-secret-key
export MINIO_REGION=us-east-1

python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py smoke
```

Use this table to choose the command:

| Situation | Run |
| --- | --- |
| Quick feedback after source changes | `python3 minio_test_runner.py source --packages ./cmd` |
| Full local source build and Go tests | `python3 minio_test_runner.py source` |
| Fast deployed-cluster correctness check | `python3 minio_test_runner.py smoke` |
| Pre-merge, nightly, or stability validation | `python3 minio_test_runner.py long --duration 12h --users 8` |

The details below cover installation requirements, permissions, reports, and
troubleshooting.

## What The Runner Does

This document explains how to install, configure, and run the MinIO test tool
on Linux, with Ubuntu 22 as the target environment.

The runner provides three modes:

- `source`: builds the MinIO source tree and runs Go tests. This mode does not use Locust.
- `smoke`: runs fast functional checks against an already-running MinIO cluster using Locust.
- `long`: runs a long-running functional/system workload against an already-running MinIO cluster using Locust.

Functional tests do not build, start, stop, or manage MinIO. The team must
provide a reachable MinIO/S3 endpoint before running `smoke` or `long`.

The tool lives outside the MinIO source tree. For `source` mode, point the
runner at a local MinIO checkout or extracted release with either `MINIO_DIR`
or `--minio-dir`. If neither is set, the runner looks for this optional
co-located directory:

```text
minio-RELEASE.2025-06-13T11-33-47Z/
```

## Directory Layout

When releasing this tool to the team, keep these repo files and directories together:

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

The MinIO source directory is external to this repo. Keep it anywhere convenient
and set:

```bash
export MINIO_DIR=/path/to/minio-RELEASE.2025-06-13T11-33-47Z
```

## Ubuntu 22 Requirements

Install base packages:

```bash
sudo apt update
sudo apt install -y python3 python3-venv git ca-certificates build-essential curl
```

Install Go 1.24.2 or newer if you plan to run `source` tests. This MinIO source
tree declares the following in `go.mod`:

```text
go 1.24.0
toolchain go1.24.2
```

The Ubuntu 22 apt package for Go is usually too old. Use your team's standard
Go installation method or install the official Go tarball.

Verify the installed versions:

```bash
go version
python3 --version
```

## Python Virtual Environment

Create and activate a virtual environment from the tool root:

```bash
cd /path/to/tminio_test_runner
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r minio-test-requirements.txt
```

The requirements file currently installs:

- `boto3`: the S3 API client used by the Locust tests.
- `locust`: the workload and reporting framework used for functional/system tests.

## Cluster Prerequisites

Before running `smoke` or `long`, prepare:

- A MinIO/S3 endpoint, for example `http://minio.example.internal:9000`.
- An access key and secret key.
- A test account with the required bucket and object permissions.
- Preferably, a dedicated test cluster, test tenant, or dedicated test account.

Functional tests create temporary buckets and delete them at the end of the
test unless cleanup is disabled. The test account should allow at least:

- bucket create, list, delete, and head
- object put, get, head, delete, list, and copy
- multipart upload create, upload part, complete, and abort
- object tagging
- bucket tagging
- bucket policy
- bucket CORS
- bucket lifecycle
- bucket versioning
- object lock governance and legal hold
- SSE-C put, get, and head

If the test account policy does not allow one of these features, the related
test will fail. That is expected: it means the account or cluster cannot fully
validate that feature.

## Recommended Environment Variables

Use environment variables for credentials so secrets are not passed as process
arguments:

```bash
export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=your-access-key
export MINIO_SECRET_KEY=your-secret-key
export MINIO_REGION=us-east-1
```

Command-line credential flags are supported for ad-hoc use, and the runner
redacts them from `report.json`. Environment variables are still preferred
because command-line arguments can appear in shell history or process listings.

If the endpoint uses a self-signed TLS certificate, you can temporarily disable
TLS verification:

```bash
python3 minio_test_runner.py smoke --no-verify-tls
```

For regular long-running tests, install the proper CA certificate instead of
keeping TLS verification disabled.

## Source Test

Run the source build and Go tests:

```bash
export MINIO_DIR=/path/to/minio-RELEASE.2025-06-13T11-33-47Z
python3 minio_test_runner.py source
```

Common variants:

```bash
python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py source --packages ./cmd ./internal/...
python3 minio_test_runner.py source --run TestIAM --packages ./cmd
python3 minio_test_runner.py source --timeout 90m
python3 minio_test_runner.py source --race --packages ./cmd
```

`source` mode runs:

- `go env`
- `go build` for the MinIO server binary
- `go test -count=1 -tags kqueue,dev -v`
- `go test -race` when `--race` is set

This mode is not a complete replacement for `make test`. It does not run every
MinIO Makefile target such as lint, shell verification, replication, healing,
or decommission tests.

## Functional Smoke Test

Smoke tests are meant for fast development feedback against an existing cluster.

Run with environment variables:

```bash
python3 minio_test_runner.py smoke
```

Or pass connection settings explicitly:

```bash
python3 minio_test_runner.py smoke \
  --endpoint http://minio.example.internal:9000
```

Defaults:

- `--users 1`; smoke mode intentionally rejects other values
- `--spawn-rate 1`
- `--duration 10m`
- cleanup enabled

Common options:

```bash
python3 minio_test_runner.py smoke --bucket-prefix alice-dev
python3 minio_test_runner.py smoke --no-cleanup
python3 minio_test_runner.py smoke --no-verify-tls
python3 minio_test_runner.py smoke --report-dir ./reports
```

Use `--no-cleanup` only for debugging because it leaves test buckets and objects
on the target cluster.

## Functional Long-Running Test

Long-running tests are intended for pre-merge, nightly, or stability validation.
The default design target is about 12 hours.

```bash
python3 minio_test_runner.py long \
  --endpoint "$MINIO_ENDPOINT" \
  --duration 12h \
  --users 8 \
  --spawn-rate 1
```

Common variants:

```bash
python3 minio_test_runner.py long --duration 2h --users 4
python3 minio_test_runner.py long --duration 12h --users 16 --spawn-rate 2
python3 minio_test_runner.py long --bucket-prefix nightly-$(date +%Y%m%d)
python3 minio_test_runner.py long --locust-arg=--loglevel --locust-arg=DEBUG
```

In `long` mode, each Locust user creates its own test bucket and continuously
runs a mixed S3 workload. The test attempts to clean up its buckets when it
stops.

## Direct Locust Usage

If the team wants to integrate directly with an existing Locust framework, use
the locustfile directly:

```bash
locust -f locustfiles/minio_s3.py MinioSmokeUser \
  --headless \
  --host "$MINIO_ENDPOINT" \
  --users 1 \
  --spawn-rate 1 \
  --run-time 10m
```

Long-running:

```bash
locust -f locustfiles/minio_s3.py MinioLongUser \
  --headless \
  --host "$MINIO_ENDPOINT" \
  --users 8 \
  --spawn-rate 1 \
  --run-time 12h
```

Direct Locust execution still requires credentials:

```bash
export MINIO_ACCESS_KEY=...
export MINIO_SECRET_KEY=...
export MINIO_REGION=us-east-1
```

Optional environment variables:

```bash
export MINIO_VERIFY_TLS=0
export MINIO_TEST_BUCKET_PREFIX=my-test
export MINIO_TEST_CLEANUP=0
export MINIO_LONG_OBJECT_LIMIT=500
```

## Reports

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

Important files:

- `summary.md`: human-readable summary.
- `report.json`: machine-readable result for CI or automation.
- `locust-*.html`: Locust HTML report.
- `locust-*_stats.csv`: request latency, RPS, and failure statistics.
- `locust-*_failures.csv`: Locust failure details.
- `logs/`: runner and Locust logs.

Process exit codes:

- `0`: tests passed.
- non-zero: source test failure, Locust failure, runner error, or timeout.

## Recommended Development Workflow

During development:

```bash
python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py smoke
```

Before merging larger changes:

```bash
python3 minio_test_runner.py source --race
python3 minio_test_runner.py long --duration 12h --users 8
```

CI or nightly:

```bash
python3 minio_test_runner.py source --race --timeout 90m
python3 minio_test_runner.py long \
  --endpoint "$MINIO_ENDPOINT" \
  --duration 12h \
  --users 8 \
  --spawn-rate 1 \
  --bucket-prefix nightly-$(date +%Y%m%d)
```

## Troubleshooting

Missing Python module:

```text
required Python module not found: locust
```

Fix:

```bash
. .venv/bin/activate
python3 -m pip install -r minio-test-requirements.txt
```

Missing credentials:

```text
MinIO credentials are required. Set MINIO_ACCESS_KEY and MINIO_SECRET_KEY
```

Fix:

```bash
export MINIO_ACCESS_KEY=your-access-key
export MINIO_SECRET_KEY=your-secret-key
```

Endpoint cannot be reached:

- Make sure `MINIO_ENDPOINT` includes `http://` or `https://`.
- Make sure the test host can reach the cluster.
- Check firewall, DNS, and load balancer settings.

TLS certificate error:

- Preferred fix: install the correct CA certificate.
- Temporary workaround: add `--no-verify-tls`.

`AccessDenied`:

- Confirm that the test account has the bucket and object permissions listed in this document.
- If the team only wants partial coverage, adjust the locustfile or account policy accordingly.

Object lock test failure:

- Confirm that the cluster supports object lock.
- Confirm that the test account has object lock, retention, and legal hold permissions.

Buckets remain after an interrupted test:

- Search for buckets using the same `--bucket-prefix`.
- Manually delete the leftover test buckets.
- Use `--no-cleanup` only when debugging; avoid it for nightly runs.
