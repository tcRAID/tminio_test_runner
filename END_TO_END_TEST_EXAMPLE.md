# End-To-End Test Example

This example shows the normal validation flow after a user changes MinIO
source code:

1. Run source validation against the changed source tree.
2. Run smoke validation against a temporary local MinIO built from that source.
3. Run local operations validation for restart, ILM config persistence, and purge.
4. Run local fault injection validation for removed-drive and corrupted-file
   behavior.
5. Deploy the changed MinIO build to a real environment.
6. Run the long Locust workload against the deployed endpoint.

Replace the paths, endpoint, and credentials with values from your environment.

## Example Assumptions

```bash
export RUNNER_DIR=/home/tminio_test_runner/tminio_test_runner
export MINIO_DIR=/home/tminio_test_runner/minio
```

`MINIO_DIR` must point to the MinIO source tree that contains your code
changes and a `go.mod` file.

## 1. Prepare The Runner Environment

```bash
cd "$RUNNER_DIR"

python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r minio-test-requirements.txt
```

The host must also have a Go version compatible with the MinIO source tree.

## 2. Run Source Validation

Start with the main server package. This builds MinIO and runs MinIO's own Go
tests for `./cmd`.

```bash
cd "$RUNNER_DIR"
. .venv/bin/activate

python3 minio_test_runner.py source \
  --minio-dir "$MINIO_DIR" \
  --packages ./cmd \
  --timeout 60m
```

For a broader check, run all Go packages:

```bash
python3 minio_test_runner.py source \
  --minio-dir "$MINIO_DIR" \
  --timeout 90m
```

For concurrency-sensitive changes, add the Go race detector:

```bash
python3 minio_test_runner.py source \
  --minio-dir "$MINIO_DIR" \
  --packages ./cmd \
  --race \
  --race-timeout 120m
```

Expected result:

- The command exits with status `0`.
- The report `summary.md` says `Result: PASS`.
- The report contains passed `go-env`, `go-build-minio`, and `go-test` steps.
- If `--race` was used, `go-test-race` also passes.

## 3. Run Local Smoke Validation

Smoke validation does not use a user endpoint. It builds MinIO from
`MINIO_DIR`, starts it on `127.0.0.1` with a temporary local filesystem drive,
runs the smoke workload, stops MinIO, and removes the temporary data directory.

```bash
cd "$RUNNER_DIR"
. .venv/bin/activate

python3 minio_test_runner.py smoke \
  --minio-dir "$MINIO_DIR" \
  --duration 10m \
  --bucket-prefix local-smoke
```

Expected result:

- The command exits with status `0`.
- The report `summary.md` says `Result: PASS`.
- The report contains passed `go-build-minio`, `local-minio-start`,
  `locust-smoke`, and `local-minio-stop` steps.
- No temporary `work/local-minio-data` directory remains after a passing run.

## 4. Run Local Operations Validation

Operations validation also does not use a user endpoint. It builds MinIO from
`MINIO_DIR`, starts it with a persistent temporary data directory, verifies clean
restart persistence, verifies a forced restart after `SIGKILL`, and then purges
the test bucket and bucket configuration.

```bash
cd "$RUNNER_DIR"
. .venv/bin/activate

python3 minio_test_runner.py ops \
  --minio-dir "$MINIO_DIR" \
  --bucket-prefix local-ops
```

Expected result:

- The command exits with status `0`.
- The report `summary.md` says `Result: PASS`.
- The report contains passed `ops-minio-start`, `ops-prepare-state`,
  `ops-verify-clean-restart`, `ops-verify-sigkill-restart`, `ops-purge-bucket`,
  and `ops-minio-stop-final` steps.
- No temporary `work/ops-minio-data` directory remains after a passing run.

Ops validates lifecycle configuration persistence, not actual background ILM
expiry timing. Keep MinIO source ILM/scanner Go tests in the source gate for
expiry execution coverage.

## 5. Run Local Fault Injection Validation

Fault validation also does not use a user endpoint. It builds MinIO from
`MINIO_DIR`, starts it with temporary filesystem drives, verifies removed-drive
PUT/GET failures, verifies corrupted-file GET failure after restart, then
re-adds a removed erasure drive and verifies healing evidence.

```bash
cd "$RUNNER_DIR"
. .venv/bin/activate

python3 minio_test_runner.py fault \
  --minio-dir "$MINIO_DIR" \
  --bucket-prefix local-fault
```

Expected result:

- The command exits with status `0`.
- The report `summary.md` says `Result: PASS`.
- The report contains passed `fault-disk-removed-put-get`,
  `fault-verify-drive-restored`, `fault-corrupt-object-file`,
  `fault-corrupt-get`, `fault-mc-admin-heal`,
  `fault-erasure-verify-heal`, and `fault-minio-stop-final` steps.
- No temporary `work/fault-minio-data` directory remains after a passing run.

## 6. Deploy To The Real Environment

Deploy the same changed MinIO source to your real validation environment using
your normal deployment process.

After deployment, collect the endpoint and credentials:

```bash
export MINIO_ENDPOINT=https://minio.example.internal:9000
export MINIO_ACCESS_KEY=<access-key>
export MINIO_SECRET_KEY=<secret-key>
```

Confirm the validation host can reach the deployed endpoint:

```bash
curl -fsS "$MINIO_ENDPOINT/minio/health/live"
curl -fsS "$MINIO_ENDPOINT/minio/health/ready"
```

Both commands should exit with status `0`.

## 7. Run A Short Long-Run Shakedown

Before starting a long unattended run, execute a short endpoint run to confirm
credentials, network access, and cleanup behavior.

```bash
cd "$RUNNER_DIR"
. .venv/bin/activate
mkdir -p longrun-reports

export MINIO_TEST_BUCKET_PREFIX=longrun-shakedown
export MINIO_TEST_CLEANUP=1
export MINIO_LONG_OBJECT_LIMIT=500

locust -f longrun/minio_long.py MinioLongUser \
  --headless \
  --host "$MINIO_ENDPOINT" \
  --users 2 \
  --spawn-rate 1 \
  --run-time 5m \
  --html longrun-reports/shakedown.html \
  --csv longrun-reports/shakedown \
  --exit-code-on-error 1
```

Expected result:

- Locust exits with status `0`.
- The final Locust summary shows `0` failures.
- Buckets with prefix `longrun-shakedown` are cleaned up when users stop.

## 8. Run The Long Validation

Run the long workload against the deployed endpoint. Adjust `--users`,
`--spawn-rate`, and `--run-time` for your environment size.

```bash
cd "$RUNNER_DIR"
. .venv/bin/activate
mkdir -p longrun-reports

export MINIO_TEST_BUCKET_PREFIX=longrun-validation
export MINIO_TEST_CLEANUP=1
export MINIO_LONG_OBJECT_LIMIT=500

locust -f longrun/minio_long.py MinioLongUser \
  --headless \
  --host "$MINIO_ENDPOINT" \
  --users 8 \
  --spawn-rate 1 \
  --run-time 12h \
  --html longrun-reports/longrun.html \
  --csv longrun-reports/longrun \
  --exit-code-on-error 1
```

Expected result:

- Locust exits with status `0`.
- The final Locust summary shows `0` failures.
- `longrun-reports/longrun.html` and `longrun-reports/longrun_stats.csv`
  are produced.
- Buckets with prefix `longrun-validation` are cleaned up when Locust stops.

## TLS Notes

If the deployed endpoint uses HTTPS with a certificate trusted by the host,
no extra setting is needed.

If this is an isolated validation environment with a private or self-signed
certificate, install the endpoint CA certificate in the host trust store before
running Locust.

## Final Pass Criteria

The validation is complete when all of these are true:

- Source validation passed.
- Local smoke validation passed and cleaned its temporary local MinIO drive.
- Local operations validation passed restart, ILM config persistence, and purge
  checks, then cleaned its temporary local MinIO drive.
- Local fault validation passed removed-drive PUT/GET, corrupted-file GET, and
  re-added erasure drive healing checks, then cleaned its temporary local MinIO
  drives.
- The changed MinIO build is deployed to the real environment.
- The long Locust run completed against the deployed endpoint with zero
  failures.
- Long-run report artifacts were saved.
- Any leftover buckets matching `MINIO_TEST_BUCKET_PREFIX` were removed.
