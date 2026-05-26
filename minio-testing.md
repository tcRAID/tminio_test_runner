# MinIO Validation Documentation

The validation package has two entry points.

`minio_test_runner.py` owns source and smoke validation. The user supplies only
the MinIO source path. For smoke validation, the runner builds MinIO, starts a
local server with a temporary filesystem drive, runs the smoke workload, stops
the server, and removes the temporary data.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r minio-test-requirements.txt

python3 minio_test_runner.py source --minio-dir /opt/minio --packages ./cmd
python3 minio_test_runner.py smoke --minio-dir /opt/minio
```

Long-running validation is not a runner subcommand. It is a direct Locust
workload against a user-supplied MinIO/S3 endpoint.

```bash
export MINIO_ENDPOINT=http://minio.example.internal:9000
export MINIO_ACCESS_KEY=test-access-key
export MINIO_SECRET_KEY=test-secret-key

locust -f longrun/minio_long.py MinioLongUser \
  --headless \
  --host "$MINIO_ENDPOINT" \
  --users 8 \
  --spawn-rate 1 \
  --run-time 12h
```

Documentation set:

- [USAGE.md](USAGE.md): validation environment, execution commands, evidence, cleanup, and troubleshooting.
- [TEST_PLAN.md](TEST_PLAN.md): validation coverage matrix, case procedures, expected results, and cleanup.
