# Runner validation

This runner provides endpoint, soak and optional lab tests for MinIO. The supported default is the HTTP endpoint command with 20 checks and automatic setup/teardown. The bounded soak and configured lab are separate selections. The historical fixture runs below validate runner behavior; they do not certify a particular official community MinIO release.

Extended soak validation on 2026-11-19:

- All 251 harness regressions passed from source and from the extracted archive launched from an unrelated directory, with spaces in the archive path. They include real repeated SIGINT/SIGTERM/SIGALRM during worker shutdown, automatic one-hour/24-hour deadlines, streaming checksums, key reuse, lost responses and request-log rotation.
- The verified archive ran in a private network namespace against a disposable, single-process MinIO fixture with two pools and eight directory drives. The unmodified 32-client workload ran for 45 seconds, completing 421 cycles and checking 5,106,040,832 bytes across all three object sizes and 842 distinct keys. A 10-second, 64-client workload completed 107 cycles, also covering all sizes. These are shortened correctness checks, not performance benchmarks.
- SIGINT and an injected deadline after positive live progress, plus a deliberately lost multipart-create response after the upload and a part were committed, all returned FAIL with successful teardown. A forced 8 KiB request-log limit rotated 33 times, retained at most four bounded files, and still cleaned all test data.
- Each scenario preserved an unrelated bucket's object bytes, versions, delete marker and pending multipart upload. The original fixture process remained running until validation explicitly stopped it; its disposable data was then removed.

The fixture used a local development binary. The host was Ubuntu 22.04.5 x86_64, Python 3.10.12. This evidence validates the extended runner and teardown behavior; it does not claim a full one-hour/24-hour run or validation of a deployed distributed cluster. Use the [soak guide](soak.md) to perform that deployment check.

Revalidated after the repository layout change on 2026-11-17:

- All 224 local harness regressions passed from the source launcher and extracted archive. The archive was launched from another working directory with spaces in its path. They cover endpoint ownership, teardown, reporting, credential aliases, packaging integrity, group selection, fault recovery and hook supervision.
- A verified extracted transfer archive ran against a disposable HTTP MinIO fixture in a private network namespace without external network access. The fixture used two pools and eight directory drives in one process.
- All 20 default checks passed, including the five protocol checks imported from the original runner.
- A shortened 5-second, 8-client soak passed and left no workload versions, markers or uploads.
- The four explicitly selected advanced concurrency cases passed, including legal-hold release, GOVERNANCE bypass and automatic bucket teardown.
- Bad credentials, a 1-second test deadline, SIGINT, an injected object-check failure, partial setup, a committed PUT with a lost response and an unfinished multipart upload with a lost create response all returned failure while successfully cleaning their test data.
- The fixture's pre-existing bucket, bytes and version history remained unchanged, and its service process remained running throughout these scenarios.

The fixture binary was an existing development binary, not a newly built release artifact. The validation host was Ubuntu 22.04 x86_64 with Python 3.10.12. This does not claim execution on other operating systems or architectures, a real distributed deployment, an actual OS upgrade, or the full advanced lab matrix. Run the supplied command against the deployed target for that endpoint's results. HTTPS encryption round trips, external integrations, node outages, maintenance and two-cluster replication need their corresponding environments.

Advanced lab cases require the server capabilities and topology described in the [lab guide](lab.md). A passing basic endpoint run does not establish the advanced lab gates or production qualification.

The repository and archive now share `src/`, `tests/unit/`, `tests/system/`,
`docs/`, `config/`, `requirements/` and `scripts/`. Pytest defaults to harness unit
tests; full lab execution requires `./run.sh lab all`. The archive was rebuilt
with the same pinned dependencies and verified source/dependency hashes. Reports
and local configuration are excluded from the transfer archive.
