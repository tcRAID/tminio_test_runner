#!/usr/bin/env python3
"""One-command system tests for an already running MinIO endpoint."""
import importlib.metadata
import os
from pathlib import Path
import runpy
import subprocess
import sys

HELP = """Usage: ./run.sh [http://HOST:9000]

Runs all 20 current-service checks with automatic setup and teardown.
Endpoint and credentials are prompted when omitted. HTTP is supported.
Automation: MINIO_ENDPOINT, MINIO_ACCESS_KEY and MINIO_SECRET_KEY.

Other commands:
  ./run.sh soak http://HOST:9000   Bounded 5-minute, 8-client workload
  ./run.sh unit                   Local harness regression tests
  ./run.sh lab --list             Advanced tests requiring a disposable lab
  ./run.sh endpoint --help        Optional endpoint settings and defaults

Reports remain in the printed minio-check-* directory. No pre-created data,
MinIO binary, service restart or JSON configuration is needed for the default run.
The release archive includes dependencies and runs without internet access.
"""


def ensure_runtime(root):
    """Source checkout installs pinned libraries once; archives use bundled libraries."""
    if (root / "bundle-manifest.json").is_file():
        return
    pins = [line.strip().split("==") for line in (root / "suite/requirements-release.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")]
    try:
        if all(importlib.metadata.version(name) == version for name, version in pins):
            return
    except importlib.metadata.PackageNotFoundError:
        pass
    cache = Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "tminio_test_runner/venv"
    python = cache / "bin/python"
    if Path(sys.prefix).resolve() == cache.resolve():
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r",
                        str(root / "suite/requirements-release.txt")], check=True)
        return
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(cache)], check=True)
    os.execv(str(python), [str(python), "-I", str(root / "minio_test_runner.py"), *sys.argv[1:]])


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent
    if args and args[0] in ("-h", "--help"):
        print(HELP)
        return 0
    if args[:1] in (["endpoint"], ["soak"]) and any(a in ("-h", "--help") for a in args[1:]):
        print(HELP + "\nS3 endpoint options (optional):\n"
              "  --timeout 1800 --cleanup-timeout 300 --lifecycle-timeout 600\n"
              "  --work-dir . --region us-east-1 --expected-commit ID --ca-file CA.pem\n"
              "  soak only: --duration 300\n")
        return 0
    if args and args[0] in ("source", "smoke"):
        print("Source builds and local-server smoke were retired. Use ./run.sh http://HOST:9000 against a running service.", file=sys.stderr)
        return 2
    # Verify archives even when users invoke this Python file directly.
    if (root / "_bootstrap.py").is_file() and not globals().get("_BUNDLE_VERIFIED", False):
        os.execv(sys.executable, [sys.executable, "-I", str(root / "_bootstrap.py"), "runner", *args])
    ensure_runtime(root)
    sys.path.insert(0, str(root / "suite"))
    mode = args.pop(0) if args and args[0] in ("endpoint", "soak", "unit", "lab") else "endpoint"
    if mode in ("endpoint", "soak"):
        from endpoint_check import main as endpoint_main
        return endpoint_main(args, soak=mode == "soak")
    lab_args = ["unit", *args] if mode == "unit" else (args or ["--list"])
    sys.argv = [str(root / "suite/run.py"), *lab_args]
    runpy.run_path(sys.argv[0], run_name="__main__")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Cannot prepare test runtime: {error}. Use the bundled tar.gz on nodes without internet.", file=sys.stderr)
        raise SystemExit(2)
