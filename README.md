# MinIO Validation Runner

## Start Here

Read [END_TO_END_TEST_EXAMPLE.md](END_TO_END_TEST_EXAMPLE.md) and follow the
commands.

Normal flow:

1. Change MinIO source code.
2. Run `source`.
3. Run local `smoke`.
4. Deploy MinIO.
5. Run Locust long test against the deployed endpoint.

## Entry Points

| Purpose | Command |
| --- | --- |
| Source build and Go tests | `python3 minio_test_runner.py source --minio-dir /path/to/minio --packages ./cmd` |
| Local smoke test | `python3 minio_test_runner.py smoke --minio-dir /path/to/minio` |
| Long endpoint test | `locust -f longrun/minio_long.py MinioLongUser --host "$MINIO_ENDPOINT"` |

`source` and `smoke` use a MinIO source path. `smoke` starts and cleans up a
temporary local MinIO.

`longrun/minio_long.py` uses a deployed endpoint supplied by the user.

## Documents

| File | Use it for |
| --- | --- |
| [END_TO_END_TEST_EXAMPLE.md](END_TO_END_TEST_EXAMPLE.md) | Copy-paste run example for source, smoke, deploy, long run |
| [USAGE.md](USAGE.md) | Command options, reports, cleanup, troubleshooting |
| [TEST_PLAN.md](TEST_PLAN.md) | Test scope, coverage, expected result |
| [minio-testing.md](minio-testing.md) | Short validation overview |

## Minimal Commands

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r minio-test-requirements.txt

python3 minio_test_runner.py source --minio-dir /path/to/minio --packages ./cmd
python3 minio_test_runner.py smoke --minio-dir /path/to/minio
```

After deploy:

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
