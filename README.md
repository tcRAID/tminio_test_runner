# tminio_test_runner

External MinIO source and functional test runner. Use it to build/test a local
MinIO source tree, then run S3 correctness workloads against an existing
MinIO/S3 endpoint.

## What To Run

| Goal | Command |
| --- | --- |
| Fast source feedback | `python3 minio_test_runner.py source --packages ./cmd` |
| Full source test pass | `python3 minio_test_runner.py source` |
| Fast cluster correctness check | `python3 minio_test_runner.py smoke` |
| Long stability/correctness run | `python3 minio_test_runner.py long --duration 12h --users 8` |

Smoke mode is a single-user correctness check. Use `long` mode for concurrent
or long-running workloads.

## Setup Once

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r minio-test-requirements.txt

export MINIO_DIR=/path/to/minio-RELEASE.2025-06-13T11-33-47Z
export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=your-access-key
export MINIO_SECRET_KEY=your-secret-key
export MINIO_REGION=us-east-1
```

## Most Common Flow

```bash
python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py smoke
```

Reports are written under `test-reports/` by default.

## Repo Scope

This repo contains the runner, Locust workloads, and documentation. It does
not vendor the MinIO source tree. For source builds/tests, point the runner at
a local MinIO checkout or extracted release with `MINIO_DIR` or `--minio-dir`.

## Documentation

- [USAGE.md](USAGE.md): installation, configuration, commands, reports, and troubleshooting.
- [TEST_PLAN.md](TEST_PLAN.md): test scope, pass/fail criteria, risks, and release gate guidance.
