"""Real, bounded local process regressions; no server or remote hook is used."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from tminio_test_runner import hook_process
def _payload(directory):
    # Both generations self-exit within20s as an additional test-only safeguard.
    child = (
        "import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); end=time.monotonic()+20; "
        "\nwhile time.monotonic()<end:\n p.write_text(str(time.monotonic())); time.sleep(.05)\n"
    )
    return [sys.executable, "-c",
            "import pathlib,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
            "pathlib.Path(sys.argv[3]).write_text(str(p.pid)); time.sleep(20)",
            child, str(directory / "heartbeat"), str(directory / "grandchild.pid")]


def _wait_for(path, seconds=8):
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.025)
    assert path.exists(), f"fixture did not create {path.name}"


def _assert_stopped(directory, *, journal_cleared=True):
    pid = int((directory / "grandchild.pid").read_text())
    try:
        process = psutil.Process(pid)
        assert not hook_process._alive(process), "grandchild survived hook cleanup"
    except psutil.NoSuchProcess:
        pass
    assert bool(list((directory / "hook-processes").glob("*.json"))) is not journal_cleared


def test_payload_cannot_run_before_group_registration(tmp_path):
    script = (
        "import json,pathlib,sys; root=pathlib.Path(sys.argv[1]); "
        "entries=list((root/'hook-processes').glob('*.json')); assert len(entries)==1; "
        "record=json.loads(entries[0].read_text()); assert record['pid']>0 and record['created']>0; "
        "assert entries[0].with_suffix('.gate').exists(); (root/'registered-before-payload').touch()"
    )
    result = hook_process.run_controlled([sys.executable, "-c", script, str(tmp_path)], artifacts=tmp_path, timeout=5)
    assert result.returncode == 0 and (tmp_path / "registered-before-payload").exists()
    assert not list((tmp_path / "hook-processes").iterdir())


@pytest.mark.timeout(35)
def test_hook_timeout_kills_grandchild_before_returning(tmp_path):
    expected = hook_process.UnverifiedHookCleanup if os.name == "nt" else subprocess.TimeoutExpired
    with pytest.raises(expected):
        # Windows may spend over a second starting the two Python generations
        # under compiler load. Allow startup before testing timeout cleanup;
        # both payload generations remain bounded by their 20s self-exit.
        hook_process.run_controlled(_payload(tmp_path), artifacts=tmp_path, timeout=5)
    _assert_stopped(tmp_path, journal_cleared=os.name != "nt")
    if os.name == "nt":
        assert (tmp_path / "hook-shutdown.json").exists()
        assert hook_process.cleanup_hooks(tmp_path), "unverified Windows cleanup must block automatic restoration"


@pytest.mark.timeout(35)
def test_outer_cleanup_stops_registered_group_after_calling_parent_is_killed(tmp_path):
    controller = (
        "import json,pathlib,sys; sys.path.insert(0,sys.argv[1]); from tminio_test_runner import hook_process; "
        "hook_process.run_controlled(json.loads(sys.argv[3]),artifacts=pathlib.Path(sys.argv[2]),timeout=20)"
    )
    process = subprocess.Popen([sys.executable, "-c", controller, str(Path(hook_process.__file__).resolve().parent.parent),
                                str(tmp_path), json.dumps(_payload(tmp_path))],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_for(tmp_path / "heartbeat")
        process.kill()
        process.wait(timeout=5)
        hook_process.close_hook_gate(tmp_path)
        errors = hook_process.cleanup_hooks(tmp_path)
        assert bool(errors) is (os.name == "nt")
        _assert_stopped(tmp_path, journal_cleared=os.name != "nt")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        hook_process.close_hook_gate(tmp_path)
        hook_process.cleanup_hooks(tmp_path)


def test_closed_gate_blocks_mutation_but_allows_recovery(tmp_path):
    hook_process.close_hook_gate(tmp_path)
    command = [sys.executable, "-c", "pass"]
    with pytest.raises(AssertionError, match="gate is closed"):
        hook_process.run_controlled(command, artifacts=tmp_path, timeout=5)
    assert hook_process.run_controlled(command, artifacts=tmp_path, timeout=5, recovery=True).returncode == 0


def test_missing_executable_fails_without_leaving_an_unverifiable_hook(tmp_path):
    result = hook_process.run_controlled([str(tmp_path / "missing-command")], artifacts=tmp_path, timeout=5)
    assert result.returncode != 0
    assert not list((tmp_path / "hook-processes").iterdir())


def test_destructive_fault_cannot_misclassify_itself_as_recovery_to_bypass_closed_gate(tmp_path):
    from tminio_test_runner import helpers
    marker = tmp_path / "must-not-execute"
    settings = {"faults": {
        "node_down": [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)],
        "node_up": [sys.executable, "-c", "pass"],
    }}
    hook_process.close_hook_gate(tmp_path)
    with pytest.raises(AssertionError, match="gate is closed"):
        helpers.run_hook(settings, "node_down", tmp_path)
    assert not marker.exists()
    helpers.run_hook(settings, "node_up", tmp_path, recovery=True)
    assert json.loads((tmp_path / "pending-recovery.json").read_text()) == {}


def test_reused_pid_is_never_killed(tmp_path):
    directory = tmp_path / "hook-processes"
    directory.mkdir()
    token = "a" * 32
    # A real PID with the wrong creation identity must fail closed, even if the
    # journal asks to kill the current test process.
    path = directory / (token + ".json")
    path.write_text(json.dumps({"token": token, "pid": os.getpid(), "created": 1}))
    assert hook_process.cleanup_hooks(tmp_path)
    assert path.exists()


def test_transient_result_read_lock_does_not_turn_success_into_cleanup_failure(tmp_path, monkeypatch):
    original = Path.read_text
    failures = []

    def briefly_locked(path, *args, **kwargs):
        if path.suffix == ".result" and not failures:
            failures.append(path)
            raise PermissionError(errno.EACCES, "simulated Windows sharing violation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", briefly_locked)
    monkeypatch.setattr(hook_process, "_sharing_violation", lambda error: isinstance(error, PermissionError))
    result = hook_process.run_controlled([sys.executable, "-c", "pass"], artifacts=tmp_path, timeout=5)
    assert result.returncode == 0 and len(failures) == 1
    assert not list((tmp_path / "hook-processes").iterdir())
    assert not (tmp_path / "hook-shutdown.json").exists()


def test_atomic_replace_retries_transient_lock(tmp_path, monkeypatch):
    original = Path.replace
    calls = []

    def briefly_locked(path, target):
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError(errno.EACCES, "simulated Windows sharing violation")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", briefly_locked)
    monkeypatch.setattr(hook_process, "_sharing_violation", lambda error: isinstance(error, PermissionError))
    target = tmp_path / "journal.json"
    hook_process._write(target, {"complete": True}, time.monotonic() + 1)
    assert json.loads(target.read_text()) == {"complete": True} and len(calls) == 2


def test_persistent_permission_denial_stops_at_original_deadline(monkeypatch):
    now, attempts = [0.0], []
    monkeypatch.setattr(hook_process.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(hook_process.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    monkeypatch.setattr(hook_process, "_sharing_violation", lambda error: isinstance(error, PermissionError))

    def denied():
        attempts.append(now[0])
        raise PermissionError(errno.EACCES, "permanent denial")

    with pytest.raises(PermissionError, match="permanent denial"):
        hook_process._retry_io(denied, deadline=0.05)
    assert attempts == [0, 0.025, 0.05]


def test_invalid_journal_json_is_not_retried_or_accepted(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{broken")
    with pytest.raises(json.JSONDecodeError):
        hook_process._read(path, time.monotonic() + 1)
