# MinIO Validation Runner

This repository packages an external validation runner for MinIO source and
S3-compatible endpoint verification. The suite validates build readiness,
source-level Go test results, functional S3 behavior, and long-running mixed
S3 workload stability against an existing MinIO deployment.

## Validation Coverage

| Validation area | Command |
| --- | --- |
| Source build and focused Go package validation | `python3 minio_test_runner.py source --packages ./cmd` |
| Full source build and Go package validation | `python3 minio_test_runner.py source` |
| Functional S3 correctness validation | `python3 minio_test_runner.py smoke` |
| Long-running S3 correctness and stability validation | `python3 minio_test_runner.py long --duration 12h --users 8` |

Smoke mode is intentionally single-user and deterministic. Long mode covers
concurrent and extended-duration workload behavior.

## Execution Environment

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r minio-test-requirements.txt

export MINIO_DIR=/opt/minio-RELEASE.2025-06-13T11-33-47Z
export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=test-access-key
export MINIO_SECRET_KEY=test-secret-key
export MINIO_REGION=us-east-1
```

## Standard Validation Execution

```bash
python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py smoke
```

The runner writes validation evidence under `test-reports/` by default,
including `summary.md`, `report.json`, command logs, and Locust HTML/CSV
reports for functional runs.

## Package Scope

The package contains the runner, Locust workloads, dependency list, and
validation documentation. It does not vendor the MinIO source tree. Source
validation uses `MINIO_DIR` or `--minio-dir` to target the MinIO source release
under test.

## Documentation

- [USAGE.md](USAGE.md): execution environment, commands, reports, and troubleshooting.
- [TEST_PLAN.md](TEST_PLAN.md): validation coverage, case procedures, expected results, and cleanup.
