"""Offline maintenance harness checks; no cluster or maintenance hook is used."""

import json
import os
import subprocess
import sys

import pytest

from tests.system import test_movement as movement


def _settings(script):
    return {"maintenance": {"rebalance_wait": [sys.executable, "-c", script],
                            "rebalance_wait_timeout": 5}}


def test_maintenance_progress_uses_scoped_environment_and_whitelisted_report(tmp_path):
    script = (
        "import json, os; "
        "assert os.environ['TMINIO_TEST_BUCKET'] == 'dev-release-unit'; "
        "assert os.path.isdir(os.environ['TMINIO_TEST_ARTIFACTS']); "
        "print(json.dumps({'moved_versions': 3, 'failed_versions': 0, 'extra': 'private-value'}))"
    )
    proof = movement.run_maintenance_hook(_settings(script), "rebalance_wait", tmp_path,
                                          "dev-release-unit", require_progress=True)
    assert proof == {"moved_versions": 3, "failed_versions": 0}
    report = (tmp_path / "maintenance-hooks.jsonl").read_text()
    assert "private-value" not in report
    assert json.loads(report)["progress"] == proof


@pytest.mark.parametrize("proof", [
    {"moved_versions": 0, "failed_versions": 0},
    {"moved_versions": 3, "failed_versions": 1},
    {"moved_versions": True, "failed_versions": 0},
    {"moved_versions": 3, "failed_versions": False},
    {"moved_versions": 3},
    [],
])
def test_rebalance_noop_or_incomplete_proof_cannot_pass(tmp_path, proof):
    script = f"print({json.dumps(proof)!r})"
    with pytest.raises(AssertionError):
        movement.run_maintenance_hook(_settings(script), "rebalance_wait", tmp_path,
                                      "dev-release-unit", require_progress=True)
    report = json.loads((tmp_path / "maintenance-hooks.jsonl").read_text())
    assert "progress" not in report


def test_malformed_hook_output_is_not_disclosed(tmp_path):
    with pytest.raises(AssertionError, match="must emit one JSON") as failure:
        movement.run_maintenance_hook(_settings("print('private-credential-output')"),
                                      "rebalance_wait", tmp_path, "dev-release-unit", require_progress=True)
    assert "private-credential" not in str(failure.value)
    assert "private-credential" not in (tmp_path / "maintenance-hooks.jsonl").read_text()


def test_maintenance_timeout_terminates_hook_and_records_sanitized_failure(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    processes = []

    def capture_process(*args, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["stdout"] != subprocess.PIPE
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(movement.subprocess, "Popen", capture_process)
    settings = {"maintenance": {"upgrade": [sys.executable, "-c", "import time; time.sleep(60)"],
                                "hook_timeout": 0.2}}
    with pytest.raises(AssertionError, match="unverified" if os.name == "nt" else "exceeded"):
        movement.run_maintenance_hook(settings, "upgrade", tmp_path, "dev-release-unit")
    assert processes and all(process.poll() is not None for process in processes)
    report = json.loads((tmp_path / "maintenance-hooks.jsonl").read_text())
    assert report["error"] == ("cleanup_uncertain" if os.name == "nt" else "timeout")
    assert "time.sleep" not in json.dumps(report)


def test_maintenance_journal_preserves_other_pending_operation(tmp_path):
    movement._pending(tmp_path, "rebalance", "rebalance_stop", "dev-release-a")
    movement._pending(tmp_path, "upgrade", "rollback", "dev-release-b")
    movement._pending(tmp_path, "rebalance")
    journal = tmp_path / "maintenance-pending-recovery.json"
    assert json.loads(journal.read_text()) == {"upgrade": {"hook": "rollback", "bucket": "dev-release-b"}}
    assert not list(tmp_path.glob("*.tmp"))
    movement._pending(tmp_path, "upgrade")
    assert json.loads(journal.read_text()) == {}


@pytest.mark.parametrize("rollback_verified", [True, False])
def test_failed_upgrade_always_rolls_back_and_keeps_intent_until_verified(tmp_path, monkeypatch, rollback_verified):
    calls = []
    settings = {"maintenance": {"upgrade": ["unused-upgrade"], "rollback": ["unused-rollback"]}}
    original_builds = {"node1": ["old-version", "old-commit"]}
    state = {"rolled_back": False}

    monkeypatch.setattr(movement, "_seed", lambda *args, **kwargs: [])
    monkeypatch.setattr(movement, "_identity", lambda *args, **kwargs: [])
    monkeypatch.setattr(movement, "_verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(movement, "eventually", lambda function, **kwargs: function())

    def builds(client):
        if state["rolled_back"] and not rollback_verified:
            return {"node1": ["new-version", "new-commit"]}
        return original_builds

    def hook(settings, name, artifacts, bucket):
        # Intent must precede even a partially failed upgrade.
        journal = json.loads((artifacts / "maintenance-pending-recovery.json").read_text())
        assert journal["upgrade"] == {"hook": "rollback", "bucket": bucket}
        calls.append(name)
        if name == "upgrade":
            raise RuntimeError("simulated partial upgrade")
        state["rolled_back"] = True

    monkeypatch.setattr(movement, "_builds", builds)
    monkeypatch.setattr(movement, "run_maintenance_hook", hook)
    expected_error = RuntimeError if rollback_verified else AssertionError
    with pytest.raises(expected_error):
        movement.test_upgrade_and_rollback_preserve_old_and_new_versions(
            settings, object(), lambda: "dev-release-unit", tmp_path)
    assert calls == ["upgrade", "rollback"]
    journal = json.loads((tmp_path / "maintenance-pending-recovery.json").read_text())
    assert bool(journal) is not rollback_verified
    report = json.loads((tmp_path / "upgrade-rollback-observations.json").read_text())
    assert report.get("rollback_oracle_verified", False) is rollback_verified


def test_upgrade_uses_candidate_commit_separately_from_baseline(tmp_path, monkeypatch):
    settings = {"expected_commit": "old-commit", "expected_upgrade_commit": "new-commit",
                "maintenance": {"upgrade": ["unused-upgrade"], "rollback": ["unused-rollback"]}}
    state = {"build": ["old-version", "old-commit"]}
    calls = []
    monkeypatch.setattr(movement, "_seed", lambda *args, **kwargs: [])
    monkeypatch.setattr(movement, "_identity", lambda *args, **kwargs: [])
    monkeypatch.setattr(movement, "_verify", lambda *args, **kwargs: None)
    monkeypatch.setattr(movement, "eventually", lambda function, **kwargs: function())
    monkeypatch.setattr(movement, "_builds", lambda client: {"node1": state["build"]})

    def hook(settings, name, artifacts, bucket):
        calls.append(name)
        state["build"] = (["new-version", "new-commit"] if name == "upgrade"
                          else ["old-version", "old-commit"])

    monkeypatch.setattr(movement, "run_maintenance_hook", hook)
    movement.test_upgrade_and_rollback_preserve_old_and_new_versions(
        settings, object(), lambda: "dev-release-unit", tmp_path)
    assert calls == ["upgrade", "rollback"]
    report = json.loads((tmp_path / "upgrade-rollback-observations.json").read_text())
    assert report["upgraded_builds"]["node1"][1] == "new-commit"
    assert report["rollback_oracle_verified"] is True
