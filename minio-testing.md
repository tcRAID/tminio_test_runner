# MinIO Test Tool Documentation

Start here if you only need the common workflow:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r minio-test-requirements.txt

export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=your-access-key
export MINIO_SECRET_KEY=your-secret-key
export MINIO_DIR=/path/to/minio-RELEASE.2025-06-13T11-33-47Z

python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py smoke
```

Use `python3 minio_test_runner.py long --duration 12h --users 8` for
pre-merge, nightly, or stability validation.

The documentation for this test tool is split into two primary documents:

- [USAGE.md](USAGE.md): Linux/Ubuntu 22 installation, configuration, execution, reports, and troubleshooting.
- [TEST_PLAN.md](TEST_PLAN.md): test goals, scope, pass/fail criteria, limitations, and release gate recommendations.
