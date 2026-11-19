#!/usr/bin/env python3
"""Build the self-contained MinIO release test package for Ubuntu 26.04."""
import argparse
from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from bundle_bootstrap import verify_bundle

# Deliberate allowlist: never package local config, logs, private mc aliases,
# credentials, virtual environments or test data from the working directory.
SOURCE_FILES = """
LICENSE
README.md
run.sh
pyproject.toml
src/tminio_test_runner/__init__.py
src/tminio_test_runner/__main__.py
src/tminio_test_runner/admin_config.py
src/tminio_test_runner/binary_check.py
src/tminio_test_runner/cli.py
src/tminio_test_runner/credentials.py
src/tminio_test_runner/endpoint_cases.py
src/tminio_test_runner/endpoint_check.py
src/tminio_test_runner/entrypoint.py
src/tminio_test_runner/helpers.py
src/tminio_test_runner/hook_process.py
src/tminio_test_runner/lab_cleanup.py
src/tminio_test_runner/lab_runner.py
src/tminio_test_runner/mc.py
src/tminio_test_runner/paths.py
src/tminio_test_runner/pool_layout.py
src/tminio_test_runner/protocol_cases.py
src/tminio_test_runner/service_hook.py
src/tminio_test_runner/soak_cases.py
src/tminio_test_runner/request_log.py
tests/__init__.py
tests/conftest.py
tests/system/__init__.py
tests/system/conftest.py
tests/system/test_admin.py
tests/system/test_faults.py
tests/system/test_lifecycle.py
tests/system/test_movement.py
tests/system/test_replication.py
tests/system/test_s3_concurrency.py
tests/system/test_split_lifecycle.py
tests/system/test_topology.py
tests/unit/__init__.py
tests/unit/test_admin_config_unit.py
tests/unit/test_binary_check_unit.py
tests/unit/test_endpoint_cases_unit.py
tests/unit/test_endpoint_check_unit.py
tests/unit/test_endpoint_teardown_unit.py
tests/unit/test_entrypoint_unit.py
tests/unit/test_fault_harness_unit.py
tests/unit/test_harness_unit.py
tests/unit/test_hook_process_unit.py
tests/unit/test_integration_merge_unit.py
tests/unit/test_movement_harness_unit.py
tests/unit/test_release_delta_unit.py
tests/unit/test_release_package_unit.py
tests/unit/test_runner_unit.py
tests/unit/test_service_hook_unit.py
tests/unit/test_soak_unit.py
docs/binary.md
docs/endpoint.md
docs/lab.md
docs/test-plan.md
docs/usage.md
docs/soak.md
docs/validation/dependencies-2026-11-08.txt
docs/validation.md
config/lab.example.json
config/service-inventory.example.json
requirements/lab.txt
requirements/release.txt
requirements/runtime.txt
scripts/bootstrap.py
scripts/bundle_bootstrap.py
scripts/package_release.py
scripts/run-binary.sh
scripts/run-endpoint.sh
docs/repository-layout.md
""".split()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def unpack_wheel(wheel, destination):
    """These packages use wheel-root purelib/platlib; reject unexpected layouts."""
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unexpected wheel path: " + member.filename)
            if len(path.parts) > 2 and path.parts[0].endswith(".data") and path.parts[1] == "scripts":
                continue  # Library imports do not need the optional jp.py CLI.
            if any(p.endswith(".data") for p in path.parts):
                raise ValueError("unexpected wheel path: " + member.filename)
            if member.is_dir():
                continue
            target = destination.joinpath(*path.parts)
            if target.exists():
                raise ValueError("wheel file collision: " + member.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))


def download(requirements, wheels, *, platform="any", abi="none", implementation="py"):
    subprocess.run([sys.executable, "-m", "pip", "download", "--disable-pip-version-check",
                    "--only-binary=:all:", "--no-deps", "--platform", platform,
                    "--python-version", "310", "--implementation", implementation, "--abi", abi,
                    "--dest", str(wheels), *requirements], check=True)


def pinned_versions(requirements):
    """Read exact package pins without importing any bundled dependency."""
    pins = {}
    for line in requirements.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if not match:
            raise ValueError("dependency must have one exact version pin: " + line)
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in pins:
            raise ValueError("duplicate dependency pin: " + name)
        pins[name] = match[2]
    return pins


def installed_versions(directory):
    versions = {}
    for metadata in sorted(directory.glob("*.dist-info/METADATA")):
        fields = Parser().parsestr(metadata.read_text())
        name = re.sub(r"[-_.]+", "-", fields.get("Name", "")).lower()
        version = fields.get("Version")
        if not name or not version or name in versions:
            raise ValueError("missing or duplicate dependency metadata: " + str(metadata))
        versions[name] = version
    return versions


def reuse_dependencies(bundle, destination, pins):
    """Reuse only manifest-verified files and the exact current dependency pins."""
    bundle = Path(bundle).resolve()
    verify_bundle(bundle)
    manifest = json.loads((bundle / "bundle-manifest.json").read_text())["files"]
    roots = ("vendor", "vendor-native/x86_64", "vendor-native/aarch64")
    expected_files = {name for name in manifest
                      if any(name.startswith(root + "/") for root in roots)}
    actual_files = set()
    for name in roots:
        directory = bundle / name
        if not directory.is_dir() or directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError("missing or symlinked dependency directory: " + name)
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("symlinked dependency: " + str(path))
            if path.is_file():
                actual_files.add(str(path.relative_to(bundle)))
        expected = {key: value for key, value in pins.items()
                    if (key == "psutil") == name.startswith("vendor-native/")}
        if installed_versions(directory) != expected:
            raise ValueError("bundled dependency versions differ from requirements/release.txt: " + name)
    if actual_files != expected_files:
        raise ValueError("dependency files differ from the verified bundle manifest")
    for name in sorted(expected_files):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / name, target)


def build(output, vendor_from=None):
    source = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.with_suffix(output.suffix + ".sha256").exists():
        raise ValueError("output already exists; choose a new archive filename")
    with tempfile.TemporaryDirectory(prefix="minio-release-build-") as temporary:
        work = Path(temporary)
        root = work / "tminio_test_runner"
        root.mkdir(parents=True)
        for name in SOURCE_FILES:
            path = source / name
            if any(parent.is_symlink() for parent in (path, *path.parents) if parent != source.parent):
                raise ValueError("refusing source symlink: " + name)
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        pins = pinned_versions(source / "requirements/release.txt")
        if vendor_from:
            reuse_dependencies(vendor_from, root, pins)
        else:
            wheels = work / "wheels"
            download([name + "==" + version for name, version in pins.items() if name != "psutil"], wheels)
            for wheel in sorted(wheels.glob("*.whl")):
                if not wheel.name.endswith("-none-any.whl"):
                    raise ValueError("nonportable pure dependency: " + wheel.name)
                unpack_wheel(wheel, root / "vendor")
            for arch in ("x86_64", "aarch64"):
                native_wheels = work / arch
                download(["psutil==" + pins["psutil"]], native_wheels,
                         platform="manylinux2014_" + arch, abi="abi3", implementation="cp")
                for wheel in native_wheels.glob("*.whl"):
                    unpack_wheel(wheel, root / "vendor-native" / arch)
        manifest = {"schema": 1, "created": datetime.now(timezone.utc).isoformat(),
                    "python": ">=3.10", "native_architectures": ["x86_64", "aarch64"],
                    "files": {str(path.relative_to(root)): digest(path) for path in sorted(root.rglob("*")) if path.is_file()}}
        (root / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # No local ownership, credentials or symlinks enter the archive.
        def normalize(info):
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if info.isdir() or info.name.endswith(".sh") else 0o644
            return info
        with tarfile.open(output, "w:gz") as archive:
            archive.add(root, arcname=root.name, filter=normalize)
    output.with_suffix(output.suffix + ".sha256").write_text(digest(output) + "  " + output.name + "\n")
    print(f"Archive: {output}\nBytes: {output.stat().st_size}\nSHA-256: {digest(output)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent.parent / "dist" / "tminio_test_runner.tar.gz"))
    parser.add_argument("--vendor-from", type=Path,
                        help="reuse verified, version-matched dependencies from an extracted package")
    args = parser.parse_args()
    build(args.output, args.vendor_from)
