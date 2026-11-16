"""Local regressions for full/default selection and fail-closed prerequisites."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import conftest
import entrypoint


@pytest.fixture
def complete_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TMINIO_TEST_ACCESS_KEY", "local-access")
    monkeypatch.setenv("TMINIO_TEST_SECRET_KEY", "local-secret")
    roots = []
    for index in range(2):
        root = tmp_path / f"pool-{index}"
        root.mkdir()
        roots.append([str(root)])
    source_log, target_log = tmp_path / "source.log", tmp_path / "target.log"
    source_log.write_text("healthy")
    target_log.write_text("healthy")
    return {
        "source": {"endpoint": "https://source.invalid"},
        "target": {"endpoint": "https://target.invalid"},
        "expected_pools": 2, "expected_nodes": 4, "expected_data": 8, "expected_parity": 4,
        "mc_path": sys.executable, "pool_roots": roots, "target_pool_roots": roots,
        "server_logs": [str(source_log)], "target_server_logs": [str(target_log)],
        "faults": {name: [sys.executable, "-c", "pass"] for name in (
            "node_down", "node_up", "quorum_loss", "quorum_restore", "target_down", "target_up")},
        "maintenance": {name: [sys.executable, "-c", "pass"] for name in (
            "rebalance_start", "rebalance_stop", "rebalance_resume", "rebalance_wait", "upgrade", "rollback")},
    }


def test_groups_cover_all_integration_tests_without_soak_leaking_into_concurrency():
    directory = Path(__file__).parent
    paths = entrypoint.test_paths(directory, list(entrypoint.GROUPS))
    for file in directory.glob("test_*.py"):
        if file.name.endswith("unit.py"):
            assert str(file) in paths
            continue
        functions = [node.name for node in ast.parse(file.read_text(encoding="utf-8")).body
                     if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
        for function in functions:
            assert str(file) in paths or str(file) + "::" + function in paths, f"unmapped gate: {file.name}::{function}"
    assert all("test_soak" not in path for path in entrypoint.test_paths(directory, ["concurrency"]))
    assert entrypoint.test_paths(directory, ["soak"]) == [str(directory / "test_s3_concurrency.py") + "::test_soak"]


def test_complete_config_passes_full_local_preflight(complete_config):
    assert entrypoint.preflight(complete_config, list(entrypoint.GROUPS), full=True) == []


def test_source_subset_does_not_require_target_hooks_pool_roots_or_mc(complete_config):
    settings = {"source": complete_config["source"], "target": {"endpoint": "invalid"},
                "mc_path": "definitely-not-installed-mc", "target_server_logs": ["absent"]}
    assert entrypoint.preflight(settings, ["concurrency"]) == []
    assert entrypoint.preflight({}, ["unit"]) == []


@pytest.mark.parametrize("key,groups", [
    ("pool_roots", ["lifecycle"]), ("target_pool_roots", ["replication"]),
    ("target", ["replication"]), ("faults", ["faults"]),
    ("maintenance", ["movement"]), ("mc_path", ["admin"]),
])
def test_selected_group_requires_its_own_dependencies(complete_config, key, groups):
    complete_config[key] = None
    assert entrypoint.preflight(complete_config, groups)


def test_full_requires_both_clusters_log_coverage(complete_config):
    complete_config["target_server_logs"] = []
    assert any("target_server_logs" in error for error in entrypoint.preflight(complete_config, list(entrypoint.GROUPS), full=True))
    assert entrypoint.preflight(complete_config, ["concurrency"]) == []


def test_diagnostic_export_must_exist_after_run(complete_config, tmp_path):
    complete_config["target_server_logs"] = [str(tmp_path / "not-exported.log")]
    complete_config["diagnostics_hook"] = [sys.executable, "-c", "pass"]
    assert entrypoint.preflight(complete_config, list(entrypoint.GROUPS), full=True) == []
    assert entrypoint.log_coverage_errors(complete_config, list(entrypoint.GROUPS), required=True, after=True)


def test_soak_watchdog_is_derived_from_config_and_rejects_short_override():
    assert entrypoint.watchdog_seconds({"soak_seconds": 86400}, list(entrypoint.GROUPS)) == 100800
    assert entrypoint.watchdog_seconds({"soak_seconds": 86400}, ["soak"]) == 87300
    assert entrypoint.watchdog_seconds({"max_seconds": 123}, ["concurrency"]) == 123
    for value in (0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError):
            entrypoint.watchdog_seconds({"max_seconds": value}, ["unit"])
    with pytest.raises(ValueError, match="exceed soak_seconds"):
        entrypoint.watchdog_seconds({"soak_seconds": 86400}, ["soak"], 3600)


@pytest.mark.parametrize("outcome,deselected,executed,expected_status", [
    ("passed", 0, True, 0), ("skipped", 0, True, 1),
    ("passed", 1, True, 1), ("passed", 0, False, 1),
])
def test_full_session_rejects_skips_deselection_or_unexecuted_tests(tmp_path, outcome, deselected, executed, expected_status):
    results = [{"test": "test_gate.py::test_case", "phase": "call", "outcome": outcome, "seconds": 0.1}] if executed else []
    config = SimpleNamespace(release_results=results, evidence_dir=tmp_path,
                             release_deselected=deselected, getoption=lambda name: name in ("--release-full", "--require-complete"))
    session = SimpleNamespace(config=config, items=[SimpleNamespace(nodeid="test_gate.py::test_case")], exitstatus=0)
    conftest.pytest_sessionfinish(session, 0)
    assert int(session.exitstatus) == expected_status
    report = json.loads((tmp_path / "results.json").read_text())
    assert report["full_suite_completed"] is (expected_status == 0)
    assert report["production_release_qualified"] is False


def test_binary_mode_replaces_process_with_exact_argv_without_cluster_config(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(entrypoint.sys, "platform", "linux")
    monkeypatch.setattr(entrypoint.os, "execv", lambda path, args: seen.append((path, args)))
    directory = tmp_path / "suite with spaces"
    args = [str(tmp_path / "binary with spaces"), "--work-dir", str(tmp_path / "disk with spaces"), "--timeout", "123"]
    entrypoint.launch_binary(directory, args)
    assert seen == [("/bin/bash", ["bash", str(directory / "run-binary.sh"), *args])]
    assert "binary" not in entrypoint.GROUPS  # No implicit addition to cluster ALL.


def test_binary_help_works_without_cluster_dependencies_or_config(tmp_path):
    script = Path(__file__).resolve().with_name("run.py")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-S", str(script), "binary", "--help"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "localhost" in result.stdout and "--work-dir" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_binary_mode_rejects_missing_binary_before_bootstrap_or_cluster_work(tmp_path):
    script = Path(__file__).resolve().with_name("run.py")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["XDG_CACHE_HOME"] = str(tmp_path / "cache-must-not-exist")
    result = subprocess.run([sys.executable, "-S", str(script), "binary", str(tmp_path / "missing")],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and "executable file" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_binary_main_dispatch_does_not_enter_cluster_preflight(tmp_path, monkeypatch):
    import run as runner
    args = [str(tmp_path / "minio"), "--work-dir", str(tmp_path)]
    seen = []
    monkeypatch.setattr(sys, "argv", ["run.py", "binary", *args])
    monkeypatch.setattr(runner, "launch_binary", lambda directory, options: seen.append(options) or 7)
    monkeypatch.setattr(runner, "preflight", lambda *a, **kw: pytest.fail("binary mode read cluster configuration"))
    monkeypatch.setattr(runner, "recover", lambda *a, **kw: pytest.fail("binary mode invoked recovery"))
    assert runner.main() == 7
    assert seen == [args]


def test_endpoint_dispatch_preserves_argv_without_cluster_config(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(entrypoint.os, "execv", lambda path, args: seen.append((path, args)))
    args = ["https://minio.internal:9000", "--ca-file", str(tmp_path / "CA with spaces.pem")]
    entrypoint.launch_endpoint(tmp_path, args)
    assert seen == [("/bin/bash", ["bash", str(tmp_path / "run-endpoint.sh"), *args])]
    assert "endpoint" not in entrypoint.GROUPS


def test_endpoint_help_needs_no_dependencies_or_configuration(tmp_path):
    script = Path(__file__).resolve().with_name("run.py")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-S", str(script), "endpoint", "--help"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "S3 endpoint" in result.stdout and "--ca-file" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_endpoint_main_dispatch_never_enters_cluster_preflight(tmp_path, monkeypatch):
    import run as runner
    args = ["https://minio.internal:9000", "--work-dir", str(tmp_path)]
    seen = []
    monkeypatch.setattr(sys, "argv", ["run.py", "endpoint", *args])
    monkeypatch.setattr(runner, "launch_endpoint", lambda directory, options: seen.append(options) or 7)
    monkeypatch.setattr(runner, "preflight", lambda *a, **kw: pytest.fail("endpoint mode read cluster config"))
    monkeypatch.setattr(runner, "recover", lambda *a, **kw: pytest.fail("endpoint mode invoked recovery"))
    assert runner.main() == 7
    assert seen == [args]
