"""Archive launcher: verified local imports, no installation or downloads."""
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import sys


def verify_bundle(root):
    manifest = json.loads((root / "bundle-manifest.json").read_text())
    for name, expected in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid bundle manifest path")
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("bundle file escapes archive directory")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("bundle integrity check failed: " + name)


def main():
    if sys.version_info < (3, 10) or sys.platform != "linux":
        raise ValueError("This bundle needs Linux and Python 3.10 or newer.")
    mode = sys.argv[1]
    root = Path(__file__).resolve().parent
    verify_bundle(root)
    architecture = {"AMD64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    native = root / "vendor-native" / architecture
    if mode in {"dev", "runner"} and not native.is_dir():
        raise ValueError("Full dev suite native dependencies require x86_64 or aarch64 Linux.")
    trusted = [str(native), str(root / "vendor"), str(root / "suite")]
    sys.path[:0] = trusted
    sys.dont_write_bytecode = True
    # Child pytest/hook supervisors also resolve the bundled packages first.
    os.environ.update(PYTHONPATH=os.pathsep.join(trusted), PYTHONNOUSERSITE="1",
                      PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      PYTEST_PLUGINS="pytest_timeout")
    filename = {"binary": "binary_check.py", "endpoint": "endpoint_check.py", "dev": "run.py", "runner": "../minio_test_runner.py"}[mode]
    script = root / "suite" / filename
    sys.argv = [str(script), *sys.argv[2:]]
    runpy.run_path(str(script), run_name="__main__", init_globals={"_BUNDLE_VERIFIED": True})


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(f"Cannot run MinIO release test package: {error}", file=sys.stderr)
        raise SystemExit(2)
