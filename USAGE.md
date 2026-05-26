# MinIO Validation Runner Execution Guide

This package has two entry points:

- `minio_test_runner.py`: source validation and local smoke validation. The
  user supplies the MinIO source path. The runner builds MinIO and, for smoke,
  starts a temporary local MinIO server.
- `longrun/minio_long.py`: long-running Locust workload. The user supplies the
  MinIO/S3 endpoint and credentials directly to Locust through environment and
  `--host`.

## Package Layout

```text
tminio_test_runner/
  locustfiles/
    minio_s3.py
  longrun/
    minio_long.py
  minio_test_runner.py
  minio-test-requirements.txt
  README.md
  END_TO_END_TEST_EXAMPLE.md
  USAGE.md
  TEST_PLAN.md
  minio-testing.md
```

The MinIO source tree is external to this package.

## Runtime Setup

```bash
cd /opt/tminio_test_runner
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r minio-test-requirements.txt
```

Required host tools:

- Python 3
- Go compatible with the MinIO source tree under test
- `locust` and `boto3` from `minio-test-requirements.txt`

## Source Validation

Source validation builds the MinIO server binary and runs MinIO's own Go tests.

```bash
python3 minio_test_runner.py source --minio-dir /opt/minio --packages ./cmd
```

Common variants:

```bash
python3 minio_test_runner.py source --minio-dir /opt/minio
python3 minio_test_runner.py source --minio-dir /opt/minio --packages ./cmd ./internal/...
python3 minio_test_runner.py source --minio-dir /opt/minio --run TestIAM --packages ./cmd
python3 minio_test_runner.py source --minio-dir /opt/minio --race --packages ./cmd
```

The source profile executes:

- `go env`
- `go build -tags <build-tags> -trimpath`
- `go test -count=1 -timeout <timeout> -tags <test-tags> -v <packages>`
- `go test -race` when `--race` is present

Build outputs are written under `test-reports/<run>/work` and removed after a
passing run unless `--keep-workdir` is used.

## Local Smoke Validation

Smoke validation no longer targets a user-provided endpoint. It builds MinIO
from `--minio-dir`, starts that binary on `127.0.0.1` with a temporary local
filesystem drive, runs the single-user smoke workload, stops MinIO, and removes
the temporary data directory.

```bash
python3 minio_test_runner.py smoke --minio-dir /opt/minio
```

Useful options:

```bash
python3 minio_test_runner.py smoke --minio-dir /opt/minio --bucket-prefix validation-smoke
python3 minio_test_runner.py smoke --minio-dir /opt/minio --duration 10m
python3 minio_test_runner.py smoke --minio-dir /opt/minio --report-dir ./reports
python3 minio_test_runner.py smoke --minio-dir /opt/minio --keep-workdir
```

Local smoke behavior:

- The runner chooses free localhost API and console ports.
- The runner sets local root credentials automatically unless `--access-key`
  or `--secret-key` are supplied.
- Smoke always runs with one Locust user.
- Bucket cleanup is enabled inside the smoke workload.
- The local MinIO process is terminated in the runner `finally` path.
- The local MinIO data directory is removed even when smoke fails.

Smoke report steps include:

- `go-build-minio`
- `local-minio-start`
- `locust-smoke`
- `local-minio-stop`

## Long-Running Validation

Long-running validation is intentionally outside `minio_test_runner.py`. Run it
directly with Locust from `longrun/minio_long.py` against an endpoint you
provide.

```bash
export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=test-access-key
export MINIO_SECRET_KEY=test-secret-key

locust -f longrun/minio_long.py MinioLongUser \
  --headless \
  --host "$MINIO_ENDPOINT" \
  --users 8 \
  --spawn-rate 1 \
  --run-time 12h \
  --html longrun.html \
  --csv longrun
```

Additional long-run environment:

```bash
export MINIO_TEST_BUCKET_PREFIX=validation
export MINIO_TEST_CLEANUP=1
export MINIO_LONG_OBJECT_LIMIT=500
export MINIO_VERIFY_TLS=0
```

`MINIO_ENDPOINT` is preferred by the boto3 client. `--host` is also accepted as
the Locust host and is used when `MINIO_ENDPOINT` is not set.

## Validation Evidence

`minio_test_runner.py` creates timestamped reports:

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
```

Long-run evidence is produced by Locust from the `--html` and `--csv` options
you pass to the `locust` command.

## Cleanup

Source validation does not modify the MinIO source tree.

Local smoke cleanup is automatic:

- The local MinIO process is stopped.
- The temporary filesystem drive directory is removed.
- Smoke-created buckets are removed by the workload before the server stops.
- Passing runner work directories are removed unless `--keep-workdir` is set.

Long-run cleanup depends on the endpoint and Locust process lifetime:

- With `MINIO_TEST_CLEANUP=1`, each Locust user attempts to delete its bucket on
  stop.
- If Locust is interrupted or a user crashes, remove buckets matching
  `MINIO_TEST_BUCKET_PREFIX` manually.

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

Local smoke does not start:

- Confirm `--minio-dir` points to a MinIO source tree containing `go.mod`.
- Inspect `logs/go-build-minio.log`.
- Inspect `logs/local-minio.log`.
- Confirm localhost ports are not blocked by the host firewall.

Long-run endpoint cannot be reached:

- Confirm `MINIO_ENDPOINT` or `--host` includes `http://` or `https://`.
- Confirm the validation host can reach the endpoint.
- Confirm `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` are set.

TLS certificate error during long run:

- Install the endpoint CA certificate in the validation host trust store.
- For isolated validation environments, set `MINIO_VERIFY_TLS=0` for urllib
  health and presigned URL checks.
