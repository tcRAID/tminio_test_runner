# Consolidated runner validation

This runner combines the original endpoint/Locust coverage with the service repository's development release suite. The supported default is the current-service HTTP endpoint command with 20 checks and automatic setup/teardown. The bounded soak and configured lab are separate selections.

HTTP 503 retries and socket-timeout validation on 2026-11-20:

- All 282 harness regressions passed from source and from the extracted archive launched from an unrelated directory with spaces in the archive path. New coverage checks the default/custom SDK read timeout, bounded 503 retries, exact backoff, replayable request bodies, concurrent counters, full redacted retry history across request-log rotation, expected 404 recovery, and ambiguous committed-write cleanup.
- The retry regressions verify that a third consecutive 503 fails the request and the run, even if a later whole-bucket teardown attempt succeeds. Normal endpoint workload requests remain strict, and workload transport timeouts are not retried.
- The extracted archive ran an unmodified four-client, five-second soak with `--read-timeout 120` against the same disposable two-pool/eight-directory fixture described below. It passed 32 cycles, verified 360,054,784 bytes, and removed its test data.
- Injected 503s after real committed PUT, COPY, DELETE and multipart-create operations produced eight logged 503s, eight retries and four recovered requests. The run passed, including reconciliation of unacknowledged versions and multipart uploads containing parts. This fixture reused its delete marker on repeated DELETE, so the validator was corrected to permit marker reuse while still requiring distinct PUT/COPY versions and upload IDs; the affected checks were rerun.
- Persistent injected GET failures produced exactly three 503s and two retries, then FAIL with successful teardown. Both injected scenarios preserved the unrelated bucket's bytes, versions, delete marker and pending upload, as did the normal run. The original fixture processes remained running until validation explicitly stopped them and removed their disposable data.
- One initial source run failed an existing hook-supervisor cleanup regression before the new retry tests ran. The focused hook file (11 tests), full source suite, and extracted archive suite then passed without changing hook code. Its cause was not established; the strict cleanup assertion remains in place.

These shortened checks validate runner behavior on the June development fixture and Ubuntu 22.04 host described below. They do not validate the deployed November release, Ubuntu 26.04, the six-node cluster, or a full one-hour run.

Extended soak validation on 2026-11-19:

- All 251 harness regressions passed from source and from the extracted archive launched from an unrelated directory, with spaces in the archive path. They include real repeated SIGINT/SIGTERM/SIGALRM during worker shutdown, automatic one-hour/24-hour deadlines, streaming checksums, key reuse, lost responses and request-log rotation.
- The verified archive ran in a private network namespace against a disposable, single-process MinIO fixture with two pools and eight directory drives. The unmodified 32-client workload ran for 45 seconds, completing 421 cycles and checking 5,106,040,832 bytes across all three object sizes and 842 distinct keys. A 10-second, 64-client workload completed 107 cycles, also covering all sizes. These are shortened correctness checks, not performance benchmarks.
- SIGINT and an injected deadline after positive live progress, plus a deliberately lost multipart-create response after the upload and a part were committed, all returned FAIL with successful teardown. A forced 8 KiB request-log limit rotated 33 times, retained at most four bounded files, and still cleaned all test data.
- Each scenario preserved an unrelated bucket's object bytes, versions, delete marker and pending multipart upload. The original fixture process remained running until validation explicitly stopped it; its disposable data was then removed.

The fixture binary was `DEVELOPMENT.2026-06-12T00-50-31Z`, commit `2f0f5c40587db38a1832f8a85942bcb5881f7621`, SHA-256 `6cef72876d8f9d9e30776a5478b15a43e346a155a90dbb15495077f9a9726f6c`. The host was Ubuntu 22.04.5 x86_64, Python 3.10.12. This evidence validates the extended runner and teardown behavior; it does not claim a full one-hour/24-hour run, execution on the user's six-node SSD cluster, or validation of the current release on Ubuntu 26.04. Use the [soak guide](soak.md) to perform that deployment check.

Revalidated after the repository layout change on 2026-11-17:

- All 224 local harness regressions passed from the source launcher and extracted archive. The archive was launched from another working directory with spaces in its path. They cover endpoint ownership, teardown, reporting, credential aliases, packaging integrity, group selection, fault recovery and hook supervision.
- A verified extracted transfer archive ran against a disposable HTTP MinIO fixture in a private network namespace without external network access. The fixture used two pools and eight directory drives in one process.
- All 20 default checks passed, including the five protocol checks imported from the original runner.
- A shortened 5-second, 8-client soak passed and left no workload versions, markers or uploads.
- The four explicitly selected advanced concurrency cases passed, including legal-hold release, GOVERNANCE bypass and automatic bucket teardown.
- Bad credentials, a 1-second test deadline, SIGINT, an injected object-check failure, partial setup, a committed PUT with a lost response and an unfinished multipart upload with a lost create response all returned failure while successfully cleaning their test data.
- The fixture's pre-existing bucket, bytes and version history remained unchanged, and its service process remained running throughout these scenarios.

The fixture binary was an existing development binary, not a newly built release artifact. The validation host was Ubuntu 22.04 x86_64 with Python 3.10.12. This does not claim execution on Ubuntu 26.04, aarch64, a real distributed deployment, an actual OS upgrade, or the full advanced lab matrix. Run the supplied command against the deployed target for that endpoint's results. HTTPS encryption round trips, external integrations, node outages, maintenance and two-cluster replication need their corresponding environments.

The retained advanced split-object tests reflect the release rollback: merged noncurrent cleanup was withdrawn, and split-object space retention remains an accepted tradeoff. A passing basic endpoint run does not establish the advanced lab gates or production qualification.

The repository and archive now share `src/`, `tests/unit/`, `tests/system/`,
`docs/`, `config/`, `requirements/` and `scripts/`. Pytest defaults to harness unit
tests; full lab execution requires `./run.sh lab all`. The archive was rebuilt
with the same pinned dependencies and verified source/dependency hashes. Reports
and local configuration are excluded from the transfer archive.
