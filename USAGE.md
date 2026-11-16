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
| Cleanup deadline | 300 seconds |
| Lifecycle processing wait | 600 seconds |
| Concurrency | 8 clients, 12 rounds |
| Soak command | 300 seconds, 8 clients |
| Reports and temporary payload parent | Current directory |

Use `./run.sh endpoint --help` only when defaults need adjustment. For example, a slower deployment may need `./run.sh http://HOST:9000 --timeout 3600 --lifecycle-timeout 1800`. The test does not speed up the server's scanner or silently pass lifecycle without observing expiration. A timeout is a failed/incomplete check with diagnostics.

The bounded soak command accepts `--duration SECONDS`; increase `--timeout` too for runs longer than the default deadline. It checks original payload hashes, HEAD, tags, copy, versioned deletion and multipart abort, removing each cycle's versions and markers. There are at most 16 keys, 24 live versions/markers and 8 pending uploads. It measures correctness under load, not a performance acceptance target.

For advanced lab tests, use `./run.sh lab --list`, then configure `suite/config.local.json` from the example and select a group, for example `./run.sh lab concurrency`. These tests may require multiple clusters, server logs, storage paths, mc and operational hooks. They are opt-in and use the separate `TMINIO_TEST_*` credential environment described in [suite/README.md](suite/README.md). No deployment-specific shutdown commands are guessed.

The former `source` and local-server `smoke` commands and Locust scripts have been retired. Their endpoint assertions are integrated into the normal command and the bounded soak; server build validation belongs in the service repository. There is one supported endpoint runner.
