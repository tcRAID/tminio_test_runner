# Soak testing a running cluster

The soak test repeatedly checks data correctness under sustained activity on one
running MinIO cluster. It creates its own test buckets and removes its test data
when the run finishes. Use the same HTTP S3 endpoint and credentials as the normal
endpoint checks; no configuration file or pre-created data is required.

## Run it

Start with the normal endpoint suite, then a one-hour soak:

```bash
./run.sh http://HOST:9000
./run.sh soak http://HOST:9000 --duration 3600
```

Run each step after the previous one passes. For a longer stability check:

```bash
./run.sh soak http://HOST:9000 --duration 86400
./run.sh http://HOST:9000
```

The last command checks functional behavior again after the soak. Run the test
from a separate machine through the S3 endpoint your applications use. The
self-contained archive needs no internet or dependency installation at runtime.

The defaults are 32 clients and five minutes. For a six-node cluster, begin with
32 clients and review request latency, node/drive health, server logs, and resource
use on the cluster and test machine. To increase concurrency after that run passes:

```bash
./run.sh soak http://HOST:9000 --duration 3600 --workers 64
```

There is no need to set object sizes or key counts. More clients increase load;
a longer duration increases observation time. Neither establishes that a cluster
with 72 SSDs has reached its capacity or exercised every drive.

## Workload and bounds

Each client rotates among 64 KiB, 1 MiB and 16 MiB objects. It uses 32 key slots,
with an original and a copy key in each slot. Each cycle:

1. Uploads an object and checks HEAD and tags.
2. Copies it and downloads both objects, verifying their SHA-256 checksums and
   sizes against the original payload. Downloads are checked as streams.
3. Deletes the original and confirms it is no longer visible.
4. Starts a multipart upload, uploads one part, and aborts the upload.
5. Removes that cycle's data versions and delete marker before proceeding.

This exercises multipart creation, part transfer and abort. The normal endpoint
suite separately checks completed large multipart uploads.

| Setting | Default | Adjustment |
| --- | --- | --- |
| Duration | 300 seconds | `--duration SECONDS` |
| Concurrent clients | 32 | `--workers 1` through `--workers 128` |
| Object sizes | 64 KiB, 1 MiB, 16 MiB | Automatic rotation |
| Key slots per client | 32, with 2 keys per slot | Fixed |
| Possible distinct keys | 2,048 | 64 × client count |
| Live versions and delete markers | At most 96 | At most 3 × client count |
| Pending multipart uploads | At most 32 | At most client count |
| Test deadline | Greater of 1800 seconds or duration + 600 seconds | Normally automatic |
| Cleanup deadline | 300 seconds | `--cleanup-timeout SECONDS` |

Each slot is cleaned before reuse, so stored versions and uploads do not accumulate
throughout a long run. The larger key set is visited over successive cycles;
short runs may exercise only some slots. Reports record actual completed work
and attempted PUT/COPY key coverage. This does not prove coverage of every pool, erasure set or disk,
because the runner does not control storage placement.

If you supply `--timeout`, it must exceed the requested duration by more than
120 seconds. Cleanup has its own deadline and still runs after a test timeout.
Plan for approximately 2 GiB free test-machine RAM with 32 clients, and more at
higher concurrency. This estimate is not an enforced reservation: measure the
test machine as well as the servers when selecting concurrency.

## Progress and results

The console prints elapsed time, completed cycles, verified GiB and unique keys
every 30 seconds. At the same interval, `results.json` saves the current statistics
in its `soak` section. This includes the duration, worker count, key slots, object
sizes, cycles per worker and per size, unique keys used, verified bytes, elapsed
time and run state. These snapshots show partial progress before the whole run
finishes. The final reports retain this information. A very short or interrupted
run can provide little coverage even with many configured clients.

Request logs rotate at 64 MiB with three backups: `requests.jsonl` and
`requests.jsonl.1` through `.3`. Together, these use approximately 256 MiB in total. The oldest request details are removed
as the log rotates; summaries and statistics remain. Stored test data is also
bounded. Choose a local report directory with sufficient free space, particularly
when keeping reports from several runs.

A passing result requires the selected checks and teardown to pass. Review
failures alongside existing cluster monitoring for unexpected request errors,
checksum mismatches, crashes, OOM events, disk errors, or latency and memory
that progressively deteriorate under steady activity. The runner checks operation
correctness; it does not measure server memory leaks or enforce throughput and
latency targets.

## Cleanup and scope

Setup uses fresh owned test buckets. Teardown runs after success, a failed check,
a timeout or normal interruption. It removes versions, delete markers, pending
uploads, buckets and local payloads. Reports remain. Cleanup relies on in-memory
ownership records, so log rotation does not discard the information needed for
normal teardown. Cleanup failures fail the run and report remaining bucket names. On interruption,
the runner first waits for in-flight requests and cycle cleanup before removing
buckets, so shutdown is not necessarily immediate.

A forced kill, test-machine failure or loss of access to MinIO can prevent complete
cleanup; a surviving report is evidence for investigation, not a guarantee of
automatic recovery. Existing application buckets are never adopted.

The soak does not stop services, change global settings, force pool placement,
inject faults or contact a second cluster. Passing establishes that the tested
operations remained correct during the observed workload; it does not certify
all hardware paths or pre-upgrade application data.
