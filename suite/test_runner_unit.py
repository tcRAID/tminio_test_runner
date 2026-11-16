"""Local recovery/watchdog checks; no deployed server is contacted."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import run as runner


def test_log_scan_ignores_old_crashes_but_detects_new_crashes(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("panic: old historical failure\n", encoding="utf-8")
    patterns = [str(log)]
    before = runner.snapshot_logs(patterns)
    with log.open("a", encoding="utf-8") as output:
        output.write("healthy new request\n")
    assert runner.scan_logs(patterns, before)["findings"] == []
    with log.open("a", encoding="utf-8") as output:
        output.write("panic: close of closed channel\n")
    assert runner.scan_logs(patterns, before)["findings"]


def test_log_scan_checks_beginning_of_replaced_log_even_when_it_grew(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("old healthy record\n" * 20, encoding="utf-8")
    patterns = [str(log)]
    before = runner.snapshot_logs(patterns)
    log.rename(tmp_path / "rotated.old")
    log.write_text("panic: close of closed channel\n" + "new healthy record\n" * 100,
                   encoding="utf-8")
    assert runner.scan_logs(patterns, before)["findings"], "rotation hid a new crash before the old offset"


def test_log_scan_detects_cross_chunk_crash_and_bounds_report_details(tmp_path):
    log = tmp_path / "large.log"
    log.write_bytes(b"x" * (1024 * 1024 - 3) + b"panic: cross-chunk\n" + b"panic: repeated\n" * 1005)
    report = runner.scan_logs([str(log)], {})
    assert report["finding_count"] == 1006
    assert len(report["findings"]) == 1000


@pytest.mark.parametrize("matches", [False, True])
def test_admin_recovery_requires_read_back_before_clearing_intent(tmp_path, monkeypatch, matches):
    import conftest

    journal = tmp_path / "pending-admin-restore.json"
    journal.write_text(json.dumps({"subsystem": "ilm", "arguments": ["expiration_workers=8"]}))
    calls = []
    ca = tmp_path / "private-ca.crt"
    ca.write_text("fixture-ca-content")

    class FakeMc:
        def __init__(self, *args):
            self.directory = args[1]

        def run(self, *args):
            assert (self.directory / "certs" / "CAs" / "src.crt").read_text() == "fixture-ca-content"
            calls.append(args)
            if args[:3] == ("admin", "config", "get"):
                return "ilm expiration_workers=" + ("8" if matches else "99")
            return ""

    monkeypatch.setattr(conftest, "Mc", FakeMc)
    monkeypatch.setenv("TMINIO_TEST_ACCESS_KEY", "lab-user")
    monkeypatch.setenv("TMINIO_TEST_SECRET_KEY", "lab-placeholder")
    errors = runner.recover({"source": {"endpoint": "https://unused.invalid", "verify_tls": str(ca)}}, tmp_path)
    assert any(call[:3] == ("admin", "config", "get") for call in calls)
    assert bool(errors) is not matches
    assert journal.exists() is not matches


def test_maintenance_recovery_retains_failed_entry_and_clears_only_success(tmp_path, monkeypatch):
    journal = tmp_path / "maintenance-pending-recovery.json"
    state = {"rebalance": {"hook": "rebalance_stop", "bucket": "dev-release-rebalance"},
             "upgrade": {"hook": "rollback", "bucket": "dev-release-upgrade"}}
    journal.write_text(json.dumps(state))
    settings = {"maintenance": {"rebalance_stop": ["lab-stop"], "rollback": ["lab-rollback"]}}

    def invoke(argv, **kwargs):
        assert kwargs["recovery"] is True
        assert kwargs["env"]["TMINIO_TEST_ARTIFACTS"] == str(tmp_path)
        assert kwargs["env"]["TMINIO_TEST_BUCKET"].startswith("dev-release-")
        return SimpleNamespace(returncode=7 if argv == ["lab-rollback"] else 0)

    monkeypatch.setattr(runner, "run_controlled", invoke)
    errors = runner.recover(settings, tmp_path)
    assert errors and json.loads(journal.read_text()) == {"upgrade": state["upgrade"]}
    monkeypatch.setattr(runner, "run_controlled", lambda *a, **kw: SimpleNamespace(returncode=0))
    assert runner.recover(settings, tmp_path) == []
    assert json.loads(journal.read_text()) == {}


def test_recover_only_reports_invalid_journal_as_failure(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{}")
    (tmp_path / "pending-recovery.json").write_text("{invalid")
    monkeypatch.setattr(sys, "argv", ["run.py", "--config", str(config), "--artifacts", str(tmp_path), "--recover-only"])
    assert runner.main() != 0
    report = json.loads((tmp_path / "recovery.json").read_text())
    assert report["recovery_errors"]
    assert report["production_release_qualified"] is False
    assert (tmp_path / "pending-recovery.json").exists()


def test_posix_cleanup_signals_group_even_if_parent_has_already_exited(monkeypatch):
    events = []
    process = SimpleNamespace(pid=424242, wait=lambda timeout: events.append(("wait", timeout)))
    monkeypatch.setattr(runner, "os", SimpleNamespace(
        name="posix", killpg=lambda pid, sig: events.append(("group", pid, sig))))
    monkeypatch.setattr(runner, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    runner.terminate_process_tree(process)
    assert events == [("group", 424242, runner.signal.SIGTERM), ("wait", 10),
                      ("group", 424242, runner.signal.SIGKILL), ("wait", 10)]


@pytest.mark.parametrize("interrupt", [False, True])
def test_runner_recovers_only_after_tree_termination_on_deadline_or_interrupt(tmp_path, monkeypatch, interrupt):
    config = tmp_path / "config.json"
    config.write_text("{}")
    events = []

    def wait(timeout):
        if interrupt:
            raise KeyboardInterrupt()
        raise subprocess.TimeoutExpired("local-fixture", timeout)

    process = SimpleNamespace(wait=wait)
    monkeypatch.setattr(runner, "start_test_process", lambda *args: process)
    monkeypatch.setattr(runner, "terminate_process_tree", lambda owned: events.append("terminated") if owned is process else None)

    def recover(settings, artifacts):
        assert events == ["terminated"], "restoration raced a still-running fault command"
        events.append("recovered")
        return []

    monkeypatch.setattr(runner, "recover", recover)
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", ["run.py", "unit", "--config", str(config), "--artifacts", str(evidence), "--max-seconds", "1"])
    assert runner.main() == 1
    report = json.loads((evidence / "runner.json").read_text())
    assert report["interrupted"] is interrupt
    assert report["parent_timeout"] is not interrupt
    assert events == ["terminated", "recovered"]


@pytest.mark.parametrize("residue", [
    "pending-recovery.json", "pending-admin-restore.json",
    "maintenance-pending-recovery.json", "hook-processes/owned.json", "unfinished-evidence.txt",
])
def test_runner_rejects_interrupted_artifacts_before_final_reports(tmp_path, monkeypatch, residue):
    evidence = tmp_path / "evidence"
    leftover = evidence / residue
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text("preserve recovery evidence", encoding="utf-8")
    monkeypatch.setattr(runner, "start_test_process", lambda *args: pytest.fail("started tests using interrupted evidence"))
    monkeypatch.setattr(runner, "recover", lambda *args: pytest.fail("normal run performed unrequested recovery"))
    monkeypatch.setattr(sys, "argv", ["run.py", "unit", "--artifacts", str(evidence)])
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2
    assert leftover.read_text(encoding="utf-8") == "preserve recovery evidence"
    assert not (evidence / "runner.json").exists()


def test_runner_accepts_existing_empty_artifacts_directory(tmp_path, monkeypatch):
    evidence = tmp_path / "empty-evidence"
    evidence.mkdir()

    def launch(*args):
        (evidence / "results.json").write_text(json.dumps({"selected_tests_passed": True}))
        return SimpleNamespace(wait=lambda timeout: 0)

    monkeypatch.setattr(runner, "start_test_process", launch)
    monkeypatch.setattr(runner, "terminate_process_tree", lambda process: None)
    monkeypatch.setattr(runner, "recover", lambda *args: [])
    monkeypatch.setattr(sys, "argv", ["run.py", "unit", "--artifacts", str(evidence)])
    assert runner.main() == 0


def test_recover_only_accepts_nonempty_interrupted_artifacts(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{}")
    evidence = tmp_path / "interrupted"
    evidence.mkdir()
    (evidence / "pending-recovery.json").write_text('{}')
    recovered = []
    monkeypatch.setattr(runner, "recover", lambda settings, path: recovered.append(path) or [])
    monkeypatch.setattr(runner, "start_test_process", lambda *args: pytest.fail("recovery-only started tests"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--config", str(config), "--artifacts", str(evidence), "--recover-only"])
    assert runner.main() == 0
    assert recovered == [evidence]


@pytest.mark.timeout(40)
def test_watchdog_termination_stops_spawned_child_before_recovery(tmp_path):
    # This owns both processes. Each self-exits after15s even if a regression
    # prevents cleanup, and neither process contacts a service or invokes hooks.
    heartbeat = tmp_path / "child-heartbeat"
    child = (
        "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); end=time.monotonic()+15; "
        "\nwhile time.monotonic()<end:\n p.write_text(str(time.monotonic())); time.sleep(0.05)\n"
    )
    parent = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(15)"
    )
    process = runner.start_test_process([sys.executable, "-c", parent, child, str(heartbeat)], os.environ.copy())
    try:
        deadline = time.monotonic() + 8
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert heartbeat.exists(), "watchdog fixture child did not start"
        runner.terminate_process_tree(process)
        assert process.poll() is not None
        stable = heartbeat.read_text()
        time.sleep(0.4)
        assert heartbeat.read_text() == stable, "child hook kept running after parent termination"
    finally:
        runner.terminate_process_tree(process)


def test_no_argument_runner_uses_local_config_and_every_group(tmp_path, monkeypatch):
    # Stub process launch: the default's destructive scope is checked local.
    monkeypatch.setattr(runner, "__file__", str(tmp_path / "run.py"))
    (tmp_path / "config.local.json").write_text(json.dumps({"max_seconds": 12345}))
    seen = {}

    def preflight(settings, groups, *, full):
        assert full and set(groups) == set(runner.GROUPS)
        assert settings["max_seconds"] == 12345
        return []

    def launch(command, environment):
        seen["command"] = command
        evidence = Path(environment["TMINIO_TEST_ARTIFACTS"])
        (evidence / "results.json").write_text(json.dumps({"selected_tests_passed": True, "full_suite_completed": True}))
        return SimpleNamespace(wait=lambda timeout: seen.setdefault("timeout", timeout) and 0)

    monkeypatch.setattr(runner, "preflight", preflight)
    monkeypatch.setattr(runner, "log_coverage_errors", lambda *args, **kwargs: [])
    monkeypatch.setattr(runner, "start_test_process", launch)
    monkeypatch.setattr(runner, "terminate_process_tree", lambda process: None)
    monkeypatch.setattr(runner, "recover", lambda *args: [])
    monkeypatch.setattr(sys, "argv", ["run.py"])
    assert runner.main() == 0
    command = seen["command"]
    assert command[3] == str(tmp_path)
    assert command[command.index("--config") + 1] == str(tmp_path / "config.local.json")
    assert all(option in command for option in ("--integration", "--run-admin", "--run-faults", "--release-full", "--require-complete"))
    assert seen["timeout"] == 12345
    report = json.loads(next((tmp_path / "artifacts").glob("*/runner.json")).read_text())
    assert report["mode"] == "full" and report["production_release_qualified"] is False


def test_incomplete_full_configuration_fails_before_launching_hooks(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setattr(sys, "argv", ["run.py", "--config", str(config), "--artifacts", str(tmp_path / "evidence")])
    monkeypatch.setattr(runner, "start_test_process", lambda *args: pytest.fail("preflight started pytest"))
    monkeypatch.setattr(runner, "recover", lambda *args: pytest.fail("preflight executed recovery"))
    assert runner.main() == 2
    output = capsys.readouterr().out
    for dependency in ("source.endpoint", "target.endpoint", "pool_roots", "target_pool_roots", "faults.target_down", "maintenance.upgrade", "server_logs", "target_server_logs"):
        assert dependency in output


def test_list_needs_no_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "__file__", str(tmp_path / "run.py"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--list"])
    assert runner.main() == 0
    assert "replication" in capsys.readouterr().out
    assert not (tmp_path / "artifacts").exists()


def test_unit_ignores_cluster_config_and_diagnostics(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"diagnostics_hook": ["must-not-run"], "server_logs": ["missing"]}))
    seen = {}

    def launch(command, environment):
        seen["command"] = command
        (Path(environment["TMINIO_TEST_ARTIFACTS"]) / "results.json").write_text(json.dumps({"selected_tests_passed": True}))
        return SimpleNamespace(wait=lambda timeout: 0)

    monkeypatch.setattr(runner, "start_test_process", launch)
    monkeypatch.setattr(runner, "terminate_process_tree", lambda process: None)
    monkeypatch.setattr(runner, "recover", lambda settings, artifacts: [] if settings == {} else pytest.fail("unit loaded dev config"))
    monkeypatch.setattr(sys, "argv", ["run.py", "unit", "--config", str(config), "--artifacts", str(tmp_path / "evidence")])
    assert runner.main() == 0
    assert not set(seen["command"]) & {"--config", "--integration", "--run-faults", "--run-admin"}


@pytest.mark.parametrize("extra", [["-m", "not soak"], ["--collect-only"], ["test_faults.py"], ["--config", "other.json"]])
def test_full_runner_rejects_pytest_selection_and_fixture_overrides(monkeypatch, extra):
    monkeypatch.setattr(sys, "argv", ["run.py", "--", *extra])
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2


def test_full_runner_cannot_ignore_entire_module_through_environment(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py"])
    monkeypatch.setenv("PYTEST_ADDOPTS", "--ignore=test_faults.py")
    with pytest.raises(SystemExit) as error:
        runner.main()
    assert error.value.code == 2


@pytest.mark.parametrize("results", [None, {"selected_tests_passed": False}, {"selected_tests_passed": True, "full_suite_completed": False}])
def test_full_runner_rejects_zero_exit_without_complete_evidence(tmp_path, monkeypatch, results):
    config = tmp_path / "config.json"
    config.write_text("{}")
    evidence = tmp_path / "evidence"

    def launch(*args):
        if results is not None:
            (evidence / "results.json").write_text(json.dumps(results))
        return SimpleNamespace(wait=lambda timeout: 0)

    monkeypatch.setattr(runner, "preflight", lambda *args, **kwargs: [])
    monkeypatch.setattr(runner, "log_coverage_errors", lambda *args, **kwargs: [])
    monkeypatch.setattr(runner, "start_test_process", launch)
    monkeypatch.setattr(runner, "terminate_process_tree", lambda process: None)
    monkeypatch.setattr(runner, "recover", lambda *args: [])
    monkeypatch.setattr(sys, "argv", ["run.py", "--config", str(config), "--artifacts", str(evidence)])
    assert runner.main() == 1
    assert json.loads((evidence / "runner.json").read_text())["gate_errors"]
