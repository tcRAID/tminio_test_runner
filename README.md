# tminio_test_runner

System tests for a running MinIO S3 endpoint, including the tMinIO release on Ubuntu 26.04. This repository owns the system tests; the [tMinIO repository](https://github.com/tcRAID/tMinIO) owns the service.

Run one command:

```bash
./run.sh http://HOST:9000
```

Enter the access key and secret key when prompted. The secret is hidden. Running `./run.sh` without arguments also prompts for the endpoint. No buckets, objects, configuration file, MinIO binary or previous deployment are needed.

The default run performs **20 checks**, creates its test data, and removes that data afterward. It tests cluster health, object integrity, listing, multipart, versioning/restoration, concurrent access, permissions, signed URLs, bucket policies/configuration, HTTP encryption rejection and real lifecycle processing. It uses one cluster and does not restart services or change global configuration. Full coverage is listed in [test plan](docs/test-plan.md).

Use the self-contained archive on an internal node:

```bash
sha256sum -c tminio_test_runner.tar.gz.sha256
tar -xzf tminio_test_runner.tar.gz
cd tminio_test_runner
./run.sh http://HOST:9000
```

The archive includes Python dependencies and needs no internet, pip, venv, mc, Go or sudo at runtime. Requirements: Linux, Python 3.10+, 2 GiB local temporary space, roughly 5 GiB logical cluster space plus erasure overhead, and a test account with the required S3 and `admin:ServerInfo` permissions. A source checkout installs pinned dependencies in a private cached venv on first use and needs internet plus `python3-venv` for that step.

All normal settings have defaults: 30-minute overall deadline, separate 5-minute cleanup deadline, 10-minute lifecycle wait, `us-east-1` region, and reports in the current directory. HTTP needs no certificate options. For automation, supply `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`. Existing `TMINIO_TEST_ACCESS_KEY` / `TMINIO_TEST_SECRET_KEY` pairs also work; conflicting pairs fail before setup.

Exit **0** means every selected check and teardown passed. Failed/incomplete checks or cleanup return **1**; invalid input/setup returns **2**. The printed `minio-check-*` directory contains the summary, JSON and JUnit reports. Success, failure, timeout and normal interruption all trigger teardown. If deletion is denied or the endpoint is unreachable, the run fails and reports the remaining bucket names. Forced termination or host loss can prevent cleanup.

Additional commands are optional:

```bash
./run.sh soak http://HOST:9000  # 5 minutes, 32 clients, mixed object sizes
./run.sh unit                  # harness tests; no endpoint or credentials
./run.sh lab --list            # explicitly configured advanced lab tests
```

For sustained activity, run `./run.sh soak http://HOST:9000 --duration 3600` for one hour; the deadline adjusts automatically. Soak automatically retries HTTP 503 responses up to twice and reports every recovery or exhausted retry. A soak PASS can include recovered 503s; workload socket read timeouts fail the check. Teardown independently retries bucket cleanup after transient failures. The [soak guide](docs/soak.md) explains the workload, reports and longer runs.

See [usage guide](docs/usage.md) for details and [lab guide](docs/lab.md) for the advanced lab. Replication, outages, maintenance, active retention and legal holds are never part of the default run. PASS covers the tested operations on the current deployment; it does not establish pre-upgrade application data integrity or inspect the servers' OS/kernel configuration.

Build a transfer archive on a connected workstation with `python3 scripts/package_release.py`. The output is `dist/tminio_test_runner.tar.gz` and its SHA-256 file. The allowlisted archive excludes local credentials, configuration and reports. To reuse pinned dependencies from a previously verified extracted archive, add `--vendor-from /path/to/extracted/archive`.

The consolidated release suite was moved from the service repository's development release tests. Imported MinIO-derived code is distributed under [AGPL-3.0](LICENSE).

Repository layout: application code in `src/tminio_test_runner/`, harness regressions in
`tests/unit/`, advanced cluster tests in `tests/system/`, guides in `docs/`, examples
in `config/`, and build/launch utilities in `scripts/`. See [repository layout](docs/repository-layout.md)
for maintainer commands and packaging details.
