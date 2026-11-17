# Repository layout

The repository uses Python's conventional `src` layout with tests outside the
application package, following [PyPA's src layout guidance](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)
and [pytest's test-layout guidance](https://docs.pytest.org/en/stable/explanation/goodpractices.html).
There is no universal mandatory directory structure for a system-test repository.

```text
tminio_test_runner/
├── README.md                  # Quick start
├── LICENSE
├── run.sh                     # Public one-command entrypoint
├── pyproject.toml             # Pytest discovery, markers and import settings
├── src/tminio_test_runner/     # Runner, endpoint cases and shared support code
├── tests/
│   ├── conftest.py             # Shared reporting and selection hooks
│   ├── unit/                  # Local harness regressions; no service needed
│   └── system/                # Advanced lab cases and live-cluster fixtures
├── docs/                      # Usage, test plan, lab and validation guides
│   └── validation/            # Historical dependency evidence
├── config/                    # Lab and service inventory examples
├── requirements/              # Pinned release and optional development dependencies
├── scripts/                   # Bootstrap, archive builder and developer launchers
└── .github/workflows/          # CI for the harness
```

`src/tminio_test_runner/endpoint_cases.py` and `protocol_cases.py` implement the
checks shipped as the endpoint application. `tests/unit/` verifies the harness
itself; `tests/system/` holds the separately configured advanced pytest lab suite.
Network fixtures live only under `tests/system/`. Plain pytest defaults to unit
tests so local development does not implicitly select lab operations.

Run these commands from the root; an absolute path to `run.sh` also works from
another directory. Reports are written beneath the caller's working directory.

```bash
./run.sh http://HOST:9000
./run.sh unit
./run.sh lab --list
python3 scripts/package_release.py
```

The public launcher adds `src/` to Python's import path. No editable installation
or package publication is needed. `requirements/release.txt` is the pinned runtime
used by the launcher, archive and CI. `requirements/runtime.txt` is the minimal
S3 client dependency list; `requirements/lab.txt` declares supported development
ranges. Neither optional list is needed for ordinary `run.sh` use.

The archive mirrors this layout and adds `vendor/`, `vendor-native/` and a checked
`bundle-manifest.json`. It runs with local dependencies and never downloads at
runtime. The builder uses an explicit file allowlist, excluding local settings,
credentials, reports, virtual environments and caches. To reuse verified pinned
dependencies, pass `--vendor-from /path/to/extracted/archive`. Existing archive
outputs are not overwritten; use `--output /new/path/name.tar.gz` for another build.

Generated `dist/`, `test-reports/` and `minio-check-*/` directories are ignored.
Optional private lab settings go in ignored `config/*.local.json`. Keep credentials
in environment variables or the interactive prompt. Normal endpoint use requires
no configuration file. See [usage](usage.md), [test plan](test-plan.md),
[lab guide](lab.md) and [validation](validation.md).
