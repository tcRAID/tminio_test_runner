# MinIO Validation Documentation

The MinIO validation package documents both execution procedure and test
coverage. The standard validation sequence prepares the Python runtime,
targets the MinIO source release, targets the MinIO/S3 endpoint, and executes
source plus functional validation.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r minio-test-requirements.txt

export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=test-access-key
export MINIO_SECRET_KEY=test-secret-key
export MINIO_DIR=/opt/minio-RELEASE.2025-06-13T11-33-47Z

python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py smoke
```

Long-running validation is executed with:

```bash
python3 minio_test_runner.py long --duration 12h --users 8
```

Documentation set:

- [USAGE.md](USAGE.md): validation environment, execution commands, evidence, cleanup, and troubleshooting.
- [TEST_PLAN.md](TEST_PLAN.md): validation coverage matrix, case procedures, expected results, and cleanup.
