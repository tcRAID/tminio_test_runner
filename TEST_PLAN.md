# MinIO Test Plan

This document describes what to run, what each test proves, and what still
needs separate validation. For installation and execution instructions, see
[USAGE.md](USAGE.md).

## Recommended Gate

Use this first:

| Change type | Minimum test gate |
| --- | --- |
| Small source-only change | `python3 minio_test_runner.py source --packages ./cmd` |
| Normal code change | `python3 minio_test_runner.py source --packages ./cmd` and `python3 minio_test_runner.py smoke` |
| Storage, S3 API, metadata, multipart, versioning, object lock, or policy change | `python3 minio_test_runner.py source --race` and `python3 minio_test_runner.py smoke` |
| Larger or risky change | `python3 minio_test_runner.py source --race` and `python3 minio_test_runner.py long --duration 12h --users 8` |
| Nightly validation | `python3 minio_test_runner.py source --race --timeout 90m` and `python3 minio_test_runner.py long --duration 12h --users 8` |

Interpret results simply: any non-zero exit code, Locust failure, correctness
assertion failure, or runner timeout means the gate failed and must be
investigated.

## Goals

The goal of this tool is to help the team verify MinIO changes quickly and
repeatably after modifying the source code.

The tool is intended to confirm that:

- the MinIO source tree can be built;
- MinIO Go package tests pass;
- a deployed MinIO cluster still supports core S3 and bucket behavior;
- a long-running mixed S3 workload does not expose obvious correctness regressions, API failures, or stability issues.

The test tool has two major categories:

- Source tests: run Go build and Go tests against the MinIO source tree.
- Functional/system tests: use Locust to issue S3 API calls against an already-running MinIO cluster.

## Non-Goals

This tool currently does not cover:

- deploying, starting, stopping, or managing a MinIO cluster;
- replacing the complete upstream MinIO CI pipeline;
- running every advanced Makefile target, such as healing, decommission, replication, external IAM provider, or site replication tests;
- formal performance benchmarking or capacity certification;
- KMS, LDAP/OIDC, bucket notification, replication, tiering, audit log, or other features that require external systems;
- every S3 edge case or full AWS S3 compatibility validation.

## Test Levels

### Source Test

Command:

```bash
export MINIO_DIR=/path/to/minio-RELEASE.2025-06-13T11-33-47Z
python3 minio_test_runner.py source
```

Coverage:

- `go env`
- `go build` for the MinIO server binary
- `go test -count=1 -tags kqueue,dev -v ./...`
- optional `go test -race`

Purpose:

- Confirm that the source tree builds with the target Go toolchain.
- Confirm that Go package tests do not show immediate regressions.
- Provide the first feedback layer after source changes.

Pass criteria:

- `go build` exits with code `0`.
- `go test` exits with code `0`.
- If `--race` is enabled, race tests exit with code `0`.

Fail criteria:

- Any Go command exits with a non-zero code.
- The runner times out.
- The Go toolchain or source directory is missing.

### Functional Smoke Test

Command:

```bash
python3 minio_test_runner.py smoke --endpoint "$MINIO_ENDPOINT"
```

Purpose:

- Provide a fast cluster-level correctness check during development.
- Validate that common S3 and bucket behavior still works.
- Return a clear pass/fail signal within minutes.

Default execution:

- Locust headless mode.
- `MinioSmokeUser`.
- `--users 1`; smoke mode rejects other values so one completed smoke user cannot stop a larger run early.
- `--duration 10m` as the maximum run time.
- The smoke suite stops Locust after it completes.

Main test areas:

| Area | Coverage |
| --- | --- |
| Health | `/minio/health/live`, `/minio/health/ready` |
| Bucket CRUD | create, head, list, delete bucket |
| Object CRUD | put, get, head, delete object |
| Metadata | write and read user metadata |
| Listing | prefix listing and object count checks |
| Copy | server-side object copy |
| Range GET | `Range: bytes=x-y` |
| Conditional GET | `If-Match`, `If-None-Match` |
| Object Tags | put object with tags, get object tagging |
| Multipart | create multipart upload, upload parts, complete, abort |
| Versioning | enable versioning, read a specific version, verify delete marker |
| Presigned URL | presigned GET and presigned PUT |
| Bucket Policy | public-read policy and anonymous GET |
| Bucket Config | bucket tags, CORS, lifecycle |
| SSE-C | SSE-C put/get, and access without customer key must fail |
| Object Lock | governance retention, legal hold, bypass governance delete |

Pass criteria:

- The Locust process exits with code `0`.
- Locust failure count is `0`.
- All functional assertions pass.
- Test bucket cleanup succeeds or does not interrupt the test result.

Fail criteria:

- Any S3 API returns an unexpected error.
- Any correctness assertion fails, such as body hash mismatch or wrong version behavior.
- Health endpoints are unhealthy.
- The test account lacks permissions required to validate a required feature.
- Locust times out or exits unexpectedly.
- Required MinIO credentials are not provided.

### Functional Long-Running Test

Command:

```bash
python3 minio_test_runner.py long --endpoint "$MINIO_ENDPOINT" --duration 12h --users 8
```

Purpose:

- Validate cluster stability under a long-running mixed workload.
- Find issues that may not appear during short smoke runs, such as intermittent S3 failures, metadata/read-after-write issues, or multipart logic problems.
- Produce Locust latency, RPS, and failure statistics for comparing different builds or changes.

Default execution:

- Locust headless mode.
- `MinioLongUser`.
- `--duration 12h`.
- `--users 8`.
- Each Locust user creates its own test bucket.
- The test cleans up buckets when it stops.

Long-running workload mix:

| Operation | Purpose |
| --- | --- |
| PUT object | Create objects with different sizes and verify write metadata |
| GET object | Read known objects and verify SHA-256 |
| HEAD object | Validate metadata path |
| LIST objects | Validate prefix listing |
| COPY object | Validate server-side copy and metadata preservation |
| DELETE object | Validate delete behavior in a versioned bucket |
| Multipart upload | Validate multipart create, upload, and complete |
| Bucket tagging | Validate bucket config update/read path |

Pass criteria:

- The Locust process exits with code `0`.
- Failure count is `0` during the run.
- All object read-back hash checks pass.
- No unexpected S3 API failures are recorded.
- The configured test duration completes normally.

Fail criteria:

- Any correctness check fails.
- Any unexpected S3 error is recorded as a Locust failure.
- The runner times out.
- The cluster endpoint is unhealthy or unreachable.
- The test account lacks required permissions.

## Test Data Strategy

Functional tests create isolated test buckets. Bucket names follow this pattern:

```text
<bucket-prefix>-<test-kind>-<random-suffix>
```

By default, the runner generates a prefix such as:

```text
minio-test-20260521120000
```

The team can specify a prefix:

```bash
python3 minio_test_runner.py smoke --bucket-prefix pr-1234
```

Cleanup strategy:

- By default, tests delete created objects, versions, delete markers, multipart uploads, and buckets.
- With `--no-cleanup`, test data is kept for debugging.
- If the test host is killed or the cluster is interrupted, buckets may remain and must be cleaned manually.

## Test Account and Permissions

Use a dedicated test account. Do not use production root credentials.

The test account must be able to perform:

- bucket create, delete, list, and head;
- object put, get, head, delete, list, and copy;
- multipart upload lifecycle;
- bucket versioning;
- object lock retention and legal hold;
- bucket policy;
- bucket CORS;
- bucket lifecycle;
- bucket tagging;
- object tagging;
- SSE-C object access.

If the team wants only partial coverage, modify the locustfile or split out a
separate user class instead of running the full smoke test with insufficient
permissions.

## Reports and Metrics

Wrapper reports:

- `summary.md`: summary of each runner step.
- `report.json`: machine-readable result for CI.

Locust reports:

- `locust-smoke.html` or `locust-long.html`
- `locust-*_stats.csv`
- `locust-*_failures.csv`
- `locust-*_exceptions.csv`

Key metrics:

- Failure count must be `0`.
- Response time percentiles should not regress significantly from the team baseline.
- RPS should remain stable for the selected workload.
- Long-running failures should be inspected by API name.
- Cluster-side logs should be checked for corresponding errors, drive healing, timeouts, or 5xx responses.

## Recommended Release Gates

For normal changes:

```bash
python3 minio_test_runner.py source --packages ./cmd
python3 minio_test_runner.py smoke --endpoint "$MINIO_ENDPOINT"
```

For larger changes, or changes in storage/S3 paths:

```bash
python3 minio_test_runner.py source --race
python3 minio_test_runner.py long --endpoint "$MINIO_ENDPOINT" --duration 12h --users 8
```

For changes around bucket policy, versioning, multipart, object lock, or SSE,
run at least:

```bash
python3 minio_test_runner.py smoke --endpoint "$MINIO_ENDPOINT"
```

Then run the long-running test when the risk level justifies it.

## Risks and Limitations

Functional tests write data to the target cluster. Even though cleanup is
enabled by default, run these tests against a test tenant or test cluster when
possible.

The long-running user count is not a formal load-test configuration. `--users 8`
is a starting point for correctness and stability validation, not a cluster
capacity statement.

The current long-running workload focuses on S3 API correctness. It does not
actively validate:

- distributed healing correctness;
- site replication lag;
- ILM transition to an external tier;
- external IAM providers;
- KMS-backed SSE-S3/SSE-KMS;
- bucket notification delivery;
- audit log pipelines.

If one of these areas is part of the change under test, create an additional
test plan or reuse the relevant MinIO CI target.

## Maintenance and Extension Guidelines

When adding tests:

- Keep smoke tests fast, stable, and representative.
- Put repeatable, long-running mixed workloads in the long test class.
- Record every S3 operation through Locust events so latency and failures are visible.
- Add explicit assertions for correctness; do not only generate traffic.
- Ensure test-created buckets and objects can be cleaned up.
- For features requiring external systems, create a dedicated user class or separate test document.
