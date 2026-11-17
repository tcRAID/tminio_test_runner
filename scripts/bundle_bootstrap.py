"""Archive launcher: verified local imports, no installation or downloads."""
import hashlib
import json
import os
from pathlib import Path
import platform
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
    root = Path(__file__).resolve().parents[1]
    verify_bundle(root)
    architecture = {"AMD64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    native = root / "vendor-native" / architecture
    if not native.is_dir():
        raise ValueError("The release archive requires x86_64 or aarch64 Linux.")
    trusted = [str(native), str(root / "vendor"), str(root / "src"), str(root / "scripts")]
    sys.path[:0] = trusted
    sys.dont_write_bytecode = True
    # Child pytest/hook supervisors also resolve the bundled packages first.
    os.environ.update(PYTHONPATH=os.pathsep.join(trusted), PYTHONNOUSERSITE="1",
                      PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      PYTEST_PLUGINS="pytest_timeout")
    args = sys.argv[1:]
    if args[:1] == ["binary"] and (len(args) < 2 or args[1] in ("--help", "-h")
                                  or not Path(args[1]).is_file() or not os.access(args[1], os.X_OK)):
        from tminio_test_runner.entrypoint import launch_binary
        return launch_binary(root / "scripts", args[1:])
    if args[:1] == ["binary"]:
        from tminio_test_runner.binary_check import main as run
        return run(args[1:])
    from tminio_test_runner.cli import main as run
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f"Cannot run MinIO release test package: {error}", file=sys.stderr)
        raise SystemExit(2)
