# MinIO Validation Documentation

The validation package has two entry points.

`minio_test_runner.py` owns source, smoke, local operations, and local fault
validation. The user supplies only the MinIO source path. For smoke validation,
the runner builds MinIO, starts a local server with a temporary filesystem drive,
runs the smoke workload, stops the server, and removes the temporary data. For
operations validation, the runner restarts MinIO with the same temporary data
directory, verifies persisted object and bucket metadata, verifies forced
restart readability, and strictly purges the test bucket. For fault validation,
the runner injects local drive removal, object-file corruption, and local
erasure drive re-add/heal validation.

Required host tools are Python 3, Go compatible with the MinIO source tree, and
MinIO Client `mc` for the local fault re-add/heal validation.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r minio-test-requirements.txt

python3 minio_test_runner.py source --minio-dir /opt/minio --packages ./cmd
python3 minio_test_runner.py smoke --minio-dir /opt/minio
python3 minio_test_runner.py ops --minio-dir /opt/minio
python3 minio_test_runner.py fault --minio-dir /opt/minio
```

Long-running validation is not a runner subcommand. It is a direct Locust
workload against a user-supplied MinIO/S3 endpoint.

```bash
export MINIO_ENDPOINT=https://minio.example.internal:9000
export MINIO_ACCESS_KEY=<access-key>
export MINIO_SECRET_KEY=<secret-key>

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
- [END_TO_END_TEST_EXAMPLE.md](END_TO_END_TEST_EXAMPLE.md): copy-paste source, smoke, ops, fault, deploy, and long-run flow.
