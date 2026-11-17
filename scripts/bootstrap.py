#!/usr/bin/env python3
"""Launch a checkout or a verified release archive from any working directory."""
import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def ensure_runtime():
    requirements = ROOT / "requirements/release.txt"
    pins = [line.strip().split("==") for line in requirements.read_text().splitlines()
            if line.strip() and not line.startswith("#")]
    try:
        if all(importlib.metadata.version(name) == version for name, version in pins):
            return
    except importlib.metadata.PackageNotFoundError:
        pass
    cache = Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "tminio_test_runner/venv"
    python = cache / "bin/python"
    if Path(sys.prefix).resolve() == cache.resolve():
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(requirements)], check=True)
        return
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(cache)], check=True)
    os.execv(str(python), [str(python), "-I", str(__file__), *sys.argv[1:]])


def main():
    if sys.version_info < (3, 10) or sys.platform != "linux":
        raise ValueError("The runner needs Linux and Python 3.10 or newer.")
    if (ROOT / "bundle-manifest.json").is_file():
        os.execv(sys.executable, [sys.executable, "-I", str(ROOT / "scripts/bundle_bootstrap.py"), *sys.argv[1:]])
    source = str(ROOT / "src")
    sys.path.insert(0, source)
    os.environ["PYTHONPATH"] = source
    from tminio_test_runner.cli import main as run
    args = sys.argv[1:]
    # Help and invalid legacy commands must not need downloads or a service.
    if (args[:1] in (["--help"], ["-h"], ["source"], ["smoke"])
            or args[:1] in (["endpoint"], ["soak"]) and any(a in ("--help", "-h") for a in args[1:])):
        return run(args)
    if args[:1] == ["binary"] and (len(args) < 2 or args[1] in ("--help", "-h")
                                  or not Path(args[1]).is_file() or not os.access(args[1], os.X_OK)):
        from tminio_test_runner.entrypoint import launch_binary
        return launch_binary(ROOT / "scripts", args[1:])
    ensure_runtime()
    if args[:1] == ["binary"]:
        from tminio_test_runner.binary_check import main as binary_main
        return binary_main(args[1:])
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Cannot prepare test runtime: {error}. Use the bundled tar.gz on nodes without internet.", file=sys.stderr)
        raise SystemExit(2)
