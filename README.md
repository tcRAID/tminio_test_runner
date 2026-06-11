# MinIO Validation Runner

## Start Here

Read [END_TO_END_TEST_EXAMPLE.md](END_TO_END_TEST_EXAMPLE.md) and follow the
commands.

Normal flow:

1. Change MinIO source code.
2. Run `source`.
3. Run local `smoke`.
4. Run local `ops`.
5. Run local `fault`.
6. Deploy MinIO.
7. Run Locust long test against the deployed endpoint.

## Entry Points

| Purpose | Command |
| --- | --- |
| Source build and Go tests | `python3 minio_test_runner.py source --minio-dir /path/to/minio --packages ./cmd` |
| Local smoke test | `python3 minio_test_runner.py smoke --minio-dir /path/to/minio` |
| Local restart, ILM config, and purge test | `python3 minio_test_runner.py ops --minio-dir /path/to/minio` |
| Local drive fault injection test | `python3 minio_test_runner.py fault --minio-dir /path/to/minio` |
| Long endpoint test | `locust -f longrun/minio_long.py MinioLongUser --host "$MINIO_ENDPOINT"` |

`source`, `smoke`, `ops`, and `fault` use a MinIO source path. `smoke` starts
and cleans up a temporary local MinIO. `ops` restarts a local MinIO with the same
data directory and validates persistence and purge behavior. `fault` injects
local drive removal, object-file corruption, and re-add/heal validation against
temporary filesystem drives.

`longrun/minio_long.py` uses a deployed endpoint supplied by the user.

Required host tools are Python 3, Go compatible with the MinIO source tree, and
MinIO Client `mc` for the local fault re-add/heal validation.

In the release package layout, `tminio_test_runner/` and `tMinIO/` are sibling
directories. The runner auto-detects sibling `../tMinIO`; for another source
checkout, pass `--minio-dir` or set `MINIO_DIR`.

## Documents

| File | Use it for |
| --- | --- |
| [END_TO_END_TEST_EXAMPLE.md](END_TO_END_TEST_EXAMPLE.md) | Copy-paste run example for source, smoke, ops, fault, deploy, long run |
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
python3 minio_test_runner.py ops --minio-dir /path/to/minio
python3 minio_test_runner.py fault --minio-dir /path/to/minio
```

After deploy:

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
