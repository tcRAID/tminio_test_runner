# Running the tests

Run `./run.sh http://HOST:9000` and enter the access key and hidden secret key. The same command works from the source checkout and extracted archive. Defaults cover normal use; no test selection or configuration file is required.

For unattended runs, provide a complete credential pair through your environment or secret manager:

```bash
export MINIO_ENDPOINT=http://HOST:9000
read -rp 'Access key: ' MINIO_ACCESS_KEY
read -rsp 'Secret key: ' MINIO_SECRET_KEY; echo
export MINIO_ACCESS_KEY MINIO_SECRET_KEY
./run.sh
```

Credentials need `admin:ServerInfo`, STS AssumeRole, and create/delete/read/configure permissions for newly created test buckets, objects, versions, tags, policies, lifecycle, CORS and object-lock configuration. MinIO must accept the test's backdated object fixtures. Missing permissions fail the relevant checks. There are no default access or secret keys.

The suite uses fresh random `tminio-check-*` buckets. An existing bucket is never adopted. It records write intent before sending requests, including multipart uploads whose response may be lost, and checks ownership before deletion. It purges object versions, delete markers, unfinished uploads and buckets, then removes local payloads. Reports remain. Cleanup is independently bounded and never converts a test failure into PASS.

Normal defaults:

| Setting | Default |
| --- | --- |
| Endpoint if prompted and left blank | `http://localhost:9000` |
| Region | `us-east-1` |
| Test deadline | 1800 seconds |
| SDK socket read timeout | 60 seconds; optional `--read-timeout 120` for comparison |
| Cleanup deadline | 300 seconds |
| Lifecycle processing wait | 600 seconds |
| Default endpoint concurrency check | 8 clients, 12 rounds |
| Soak command | 300 seconds, 32 clients; 64 KiB, 1 MiB and 16 MiB objects |
| Soak deadline when omitted | Greater of 1800 seconds or duration + 600 seconds |
| Reports and temporary payload parent | Current directory |

Use `./run.sh endpoint --help` only when defaults need adjustment. For example, a slower deployment may need `./run.sh http://HOST:9000 --timeout 3600 --lifecycle-timeout 1800`. The test does not speed up the server's scanner or silently pass lifecycle without observing expiration. A timeout is a failed/incomplete check with diagnostics.

For a one-hour soak, run:

```bash
./run.sh soak http://HOST:9000 --duration 3600
```

For a `ReadTimeoutError`, `--read-timeout 120` allows a longer SDK socket read
wait; workload socket read timeouts fail the check. `--timeout` controls the overall run and does not
change the socket timeout. The report records the selected read timeout; a pass
with a longer wait does not establish the cause of the earlier failure. See the
[soak guide](soak.md) for timeout and shutdown behavior.

Soak automatically retries HTTP 503 responses up to twice per SDK call, during setup, workload and cleanup. It waits approximately one second, then two seconds, with a small random delay. There is no retry option to configure. Recovered requests can still pass; exhausted retries or an unreplayable body fail the run even if later cleanup succeeds. Other HTTP failures and transport timeouts are not automatically retried by this policy. Teardown independently retries whole-bucket cleanup after transient failures. The normal endpoint workload remains strict. The console and `results.json` show 503/retry/recovery counts, and `retry-events.jsonl` keeps the complete redacted event history.

The soak uses 32 clients by default. Each client rotates through 64 KiB, 1 MiB and 16 MiB objects and 32 key slots, checks original SHA-256 hashes, HEAD, tags, copy, versioned deletion and multipart abort, and removes each cycle's versions and markers. The default has 2,048 possible keys, normally at most 96 live versions/markers and 32 pending uploads. HTTP 503 retries can raise those bounds to 288 and 96 respectively if writes were committed before the error response; the runner reconciles and removes these artifacts before starting another cycle. The final report records how much of that key set was actually exercised.

Use `--duration 86400` for 24 hours. The deadline adjusts automatically when omitted; an explicit `--timeout` must exceed the duration by more than 120 seconds. `--workers 64` increases concurrency; accepted values are 1–128. Plan for approximately 2 GiB free test-machine RAM at the default concurrency and more at higher settings; this is guidance, not an enforced reservation. Console progress appears every 30 seconds, and recent request logs rotate to approximately 256 MiB. The separate `retry-events.jsonl` retains all retry events and can grow throughout the run. See the [soak guide](soak.md) for workload details and interpretation. It checks correctness during sustained activity and does not enforce a performance acceptance target.

For advanced lab tests, use `./run.sh lab --list`, then configure `config/lab.local.json` from `config/lab.example.json` and select a group, for example `./run.sh lab concurrency`. These tests may require multiple clusters, server logs, storage paths, mc and operational hooks. They are opt-in and use the separate `TMINIO_TEST_*` credential environment described in [lab guide](lab.md). No deployment-specific shutdown commands are guessed.

The former `source` and local-server `smoke` commands and Locust scripts have been retired. Their endpoint assertions are integrated into the normal command and the bounded soak; server build validation belongs in the service repository. There is one supported endpoint runner.
