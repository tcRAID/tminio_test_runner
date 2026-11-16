"""Local selection and configuration checks for the release-test entrypoint."""
from __future__ import annotations

import glob
import math
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit


GROUPS = {
    "unit": "Local harness regressions; no cluster or configuration needed",
    "topology": "Node/pool/erasure topology and reported build identity",
    "concurrency": "Threaded S3, version rollback, multipart and legal hold",
    "lifecycle": "Placement, current/NC expiry, EODM and split-pool rollback protections",
    "admin": "Expiry worker resize during concurrent S3 traffic",
    "faults": "Source node outage, quorum loss and recovery",
    "replication": "All replication gates, including target outage and split versions",
    "movement": "Rebalance stop/resume and upgrade/rollback",
    "soak": "Sustained threaded S3 traffic for configured soak_seconds",
}
MODULES = {
    "topology": ["test_topology.py"],
    "concurrency": ["test_s3_concurrency.py::" + name for name in (
        "test_concurrent_version_rollback_put_delete", "test_multipart_complete_and_abort",
        "test_legal_hold_rejects_permanent_delete", "test_governance_retention_and_bypass")],
    "lifecycle": ["test_lifecycle.py", "test_split_lifecycle.py"],
    "admin": ["test_admin.py"],
    "faults": ["test_faults.py"],
    "replication": ["test_replication.py"],
    "movement": ["test_movement.py"],
    "soak": ["test_s3_concurrency.py::test_soak"],
}


def launch_endpoint(directory, arguments):
    """Run the endpoint check without cluster configuration or operational hooks."""
    if sys.platform != "linux":
        print("Endpoint mode requires Linux.", file=sys.stderr)
        return 2
    os.execv("/bin/bash", ["bash", str(directory / "run-endpoint.sh"), *arguments])


def launch_binary(directory, arguments):
    """Replace the runner with the isolated binary launcher; no cluster config/hooks.

    exec preserves signals and the check's exit status without introducing a
    parent process that could outlive or orphan the test MinIO process.
    """
    if sys.platform != "linux":
        print("Binary mode must run on the Linux host you want to validate.", file=sys.stderr)
        return 2
    launcher = Path(directory) / "run-binary.sh"
    os.execv("/bin/bash", ["bash", str(launcher), *arguments])


def test_paths(directory, groups, full=False):
    if full:
        return [str(directory)]  # New test modules are included automatically.
    paths = []
    for group in groups:
        if group == "unit":
            paths.extend(str(path) for path in sorted(directory.glob("test_*unit.py")))
        else:
            paths.extend(str(directory / name) for name in MODULES[group])
    return list(dict.fromkeys(paths))


def watchdog_seconds(settings, groups, override=None):
    value = override if override is not None else settings.get("max_seconds")
    soak = float(settings.get("soak_seconds", 300)) if "soak" in groups else 0
    if not math.isfinite(soak) or soak < 0:
        raise ValueError("soak_seconds must be finite and positive")
    if value is None:
        # Leave room for all convergence gates plus a long soak; a24h soak must
        # never inherit the old1h watchdog. Individual requests remain bounded.
        value = (14400 if set(groups) - {"unit", "soak"} else 900) + math.ceil(soak)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("max_seconds must be a finite positive number or null (automatic)")
    if soak and value <= soak:
        raise ValueError("max_seconds must exceed soak_seconds and leave time for other selected tests")
    return value


def log_patterns(settings, groups):
    if not set(groups) - {"unit"}:
        return []
    patterns = list(settings.get("server_logs") or [])
    if "replication" in groups:
        patterns.extend(settings.get("target_server_logs") or [])
    return list(dict.fromkeys(patterns))


def log_coverage_errors(settings, groups, *, required=False, after=False):
    errors = []
    names = ["server_logs"]
    if "replication" in groups:
        names.append("target_server_logs")
    if not set(groups) - {"unit"}:
        return errors
    for name in names:
        patterns = settings.get(name, [])
        if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
            errors.append(f"{name} must be a list of log paths/globs")
            continue
        if required and not patterns:
            errors.append(f"{name} must cover every node of that cluster for the full run")
        for index, pattern in enumerate(patterns):
            files = [p for p in glob.glob(pattern) if Path(p).is_file()]
            if not files and (after or not settings.get("diagnostics_hook")):
                errors.append(f"{name}[{index}] matches no log files")
            for filename in files:
                try:
                    with open(filename, "rb") as stream:
                        stream.read(1)
                except OSError:
                    errors.append(f"{name}[{index}] includes an unreadable file")
                    break
    return errors


def preflight(settings, groups, *, full=False):
    """Validate only selected dependencies; never connect or execute a hook."""
    errors = []
    selected = set(groups)
    if not selected - {"unit"}:
        return errors
    if not isinstance(settings, dict):
        return ["configuration must be a JSON object"]

    def number(name, default, minimum=1, maximum=None):
        value = settings.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
            errors.append(f"{name} is missing or outside its allowed numeric range")

    def endpoint(name):
        spec = settings.get(name)
        if not isinstance(spec, dict):
            errors.append(f"{name}.endpoint and credential environment variables are required")
            return
        try:
            parsed = urlsplit(spec.get("endpoint", ""))
            valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
        except (ValueError, TypeError):
            valid = False
        if not valid:
            errors.append(f"{name}.endpoint must be an http(s) URL without embedded credentials")
        for key, default in (("access_key_env", "TMINIO_TEST_ACCESS_KEY"), ("secret_key_env", "TMINIO_TEST_SECRET_KEY")):
            env_name = spec.get(key, default)
            if not isinstance(env_name, str) or not env_name or not os.getenv(env_name):
                errors.append(f"{name}.{key} must name a populated environment variable")
        ca = spec.get("verify_tls", True)
        if isinstance(ca, str):
            if not Path(ca).is_file():
                errors.append(f"{name}.verify_tls CA file is missing")
        elif not isinstance(ca, bool):
            errors.append(f"{name}.verify_tls must be a boolean or CA file path")

    def command(section, name):
        container = settings if section is None else settings.get(section, {})
        value = container.get(name) if isinstance(container, dict) else None
        label = f"{section}.{name}" if section else name
        if not isinstance(value, list) or not value or not all(isinstance(arg, str) and arg for arg in value):
            errors.append(f"{label} requires a nonempty argv array")
        elif not shutil.which(value[0]):
            errors.append(f"{label} executable is unavailable")

    def roots(name, expected=None):
        value = settings.get(name)
        if not isinstance(value, list) or len(value) < 2 or (expected is not None and len(value) != expected):
            errors.append(f"{name} must list readable drive directories for every pool (at least two)")
            return
        for drives in value:
            if not isinstance(drives, list) or not drives or not all(isinstance(path, str) and Path(path).is_dir() and os.access(path, os.R_OK | os.X_OK) for path in drives):
                errors.append(f"{name} contains an invalid or unreadable drive directory")
                break

    endpoint("source")
    number("workers", 16, maximum=128)
    number("rounds", 32)
    number("request_timeout", 20, maximum=120)
    if selected & {"topology", "lifecycle"}:
        number("expected_pools", None)
    if "topology" in selected:
        for key in ("expected_nodes", "expected_data", "expected_parity"):
            number(key, None)
    if "lifecycle" in selected:
        roots("pool_roots", settings.get("expected_pools"))
        number("history_versions", 31, minimum=12)
    if "soak" in selected:
        number("soak_seconds", 300, minimum=0.001)
    if selected & {"lifecycle", "admin", "replication"}:
        executable = settings.get("mc_path", "mc")
        if not isinstance(executable, str) or not shutil.which(executable):
            errors.append("mc_path must identify an available MinIO Client executable")
    if "faults" in selected:
        for name in ("node_down", "node_up", "quorum_loss", "quorum_restore"):
            command("faults", name)
    if "replication" in selected:
        endpoint("target")
        roots("target_pool_roots")
        number("replication_large_mib", 129, minimum=129, maximum=1024)
        for name in ("target_down", "target_up"):
            command("faults", name)
    if "movement" in selected:
        for name in ("rebalance_start", "rebalance_stop", "rebalance_resume", "rebalance_wait", "upgrade", "rollback"):
            command("maintenance", name)
    if settings.get("diagnostics_hook"):
        command(None, "diagnostics_hook")
    errors.extend(log_coverage_errors(settings, groups, required=full))
    return errors
