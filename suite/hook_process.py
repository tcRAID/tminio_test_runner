"""Journaled local hook processes. Remote/detached work still needs idempotent hooks."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import psutil


class UnverifiedHookCleanup(RuntimeError):
    """Local descendants cannot be proven stopped; automatic recovery is unsafe."""


def _sharing_violation(error):
    # CPython's file open can expose a Windows sharing error as errno13 without
    # winerror. A persistent ACL denial still fails once the short budget ends.
    return os.name == "nt" and (getattr(error, "winerror", None) in (5, 32, 33)
                                or isinstance(error, PermissionError) and error.errno == errno.EACCES)


def _retry_io(operation, deadline=None):
    retry_limit = time.monotonic() + 1
    deadline = retry_limit if deadline is None else min(deadline, retry_limit)
    while True:
        try:
            return operation()
        except OSError as error:
            remaining = deadline - time.monotonic()
            if not _sharing_violation(error) or remaining <= 0:
                raise
            time.sleep(min(0.025, remaining))


def _read(path, deadline=None):
    # Invalid JSON is never retried or converted into an empty/successful result.
    return json.loads(_retry_io(lambda: path.read_text(encoding="utf-8"), deadline))


def _write(path, value, deadline=None):
    deadline = time.monotonic() + 1 if deadline is None else deadline
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = _retry_io(lambda: os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), deadline)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output)
    _retry_io(lambda: temporary.replace(path), deadline)


def close_hook_gate(artifacts):
    _write(Path(artifacts) / "hook-shutdown.json", {"closed": True})


def _alive(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _identity(pid, created):
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created) > 0.001:
            raise RuntimeError("hook PID was reused; refusing to terminate another process")
        return process if _alive(process) else None
    except psutil.NoSuchProcess:
        return None


def _stop_tree(process, group=False):
    # Suspend before enumerating to reduce creation races. POSIX group killing
    # also catches new descendants that stayed in the owned session/group.
    owned = [process]
    try:
        if process.pid != os.getpid():
            process.suspend()
        for _ in range(4):
            children = process.children(recursive=True)
            for child in children:
                try:
                    child.suspend()
                except psutil.NoSuchProcess:
                    pass
            fresh = [p for p in children if all(p != old for old in owned)]
            owned.extend(fresh)
            if not fresh:
                break
        if group and os.name == "posix":
            if os.getpgid(process.pid) != process.pid:
                raise RuntimeError("registered hook no longer owns its process group")
            os.killpg(process.pid, signal.SIGKILL)
        for child in reversed(owned):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        _gone, live = psutil.wait_procs(owned, timeout=8)
        if any(_alive(child) for child in live):
            raise RuntimeError("hook process tree did not stop")
    except psutil.NoSuchProcess:
        pass
    finally:
        # If an OS permission error prevented cleanup, do not leave suspended
        # processes silently hidden; the journal and failure remain visible.
        for child in owned:
            try:
                if _alive(child):
                    child.resume()
            except psutil.NoSuchProcess:
                pass


def _clear(directory, token, deadline=None):
    deadline = time.monotonic() + 1 if deadline is None else deadline
    # Keep the result until after the identity journal is removed, so a killed
    # cleanup cannot leave an unexplained missing leader in the journal.
    for suffix in ("request", "gate", "release", "started", "json", "result"):
        _retry_io(lambda: (directory / f"{token}.{suffix}").unlink(missing_ok=True), deadline)


def _stop_registered(path):
    record = _read(path)
    token = path.stem
    assert record["token"] == token and len(token) == 32 and all(c in "0123456789abcdef" for c in token)
    result_path = path.with_suffix(".result")
    result = _read(result_path) if result_path.exists() else {}
    uncertain = (os.name == "nt" and path.with_suffix(".started").exists()
                 and not result.get("payload_stopped") and not result.get("released"))
    process = _identity(record["pid"], record["created"])
    if process is not None:
        _stop_tree(process, group=True)
        if uncertain:
            # A dead intermediate parent can hide a Windows grandchild from
            # psutil. Stop everything visible but never certify the remainder.
            close_hook_gate(path.parent.parent)
            _write(result_path, {"returncode": 125, "cleanup_uncertain": True})
            raise UnverifiedHookCleanup("Windows abnormal hook cleanup requires manual descendant/service verification")
        _write(path.with_suffix(".result"), {"returncode": 125, "payload_stopped": True})
    else:
        if uncertain or result.get("cleanup_uncertain"):
            close_hook_gate(path.parent.parent)
            raise UnverifiedHookCleanup("Windows abnormal hook cleanup requires manual descendant/service verification")
        if not result.get("payload_stopped") and not result.get("released"):
            raise RuntimeError("hook leader disappeared without verified payload termination")
    _clear(path.parent, token)


def cleanup_hooks(artifacts):
    """Call after stopping pytest; retain any registry entry we cannot verify."""
    errors = []
    directory = Path(artifacts) / "hook-processes"
    for path in directory.glob("*.json"):
        try:
            _stop_registered(path)
        except UnverifiedHookCleanup:
            errors.append("Windows hook cleanup is unverified; inspect descendants/services manually before recovery")
        except Exception as error:
            errors.append(f"hook process cleanup: {type(error).__name__}")
    return errors


def run_controlled(command, *, artifacts, timeout, env=None, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, recovery=False, label="hook"):
    assert isinstance(command, list) and command and all(isinstance(arg, str) for arg in command)
    assert 0 < float(timeout) <= 3600
    deadline = time.monotonic() + float(timeout) + 5  # includes supervisor startup and journal I/O
    artifacts = Path(artifacts)
    closed = artifacts / "hook-shutdown.json"
    if closed.exists() and not recovery:
        raise AssertionError("hook launch gate is closed")
    directory = artifacts / "hook-processes"
    directory.mkdir(mode=0o700, exist_ok=True)
    token = uuid.uuid4().hex
    owner = psutil.Process()
    request = {"command": command, "timeout": float(timeout), "owner": owner.pid,
               "owner_created": owner.create_time(), "recovery": recovery}
    _write(directory / f"{token}.request", request, deadline)
    process = None
    registered = False
    try:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--supervisor", str(directory), token],
                                   env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   start_new_session=(os.name == "posix"))
        created = psutil.Process(process.pid).create_time()
        _write(directory / f"{token}.json", {"pid": process.pid, "created": created, "token": token, "label": label}, deadline)
        registered = True
        if closed.exists() and not recovery:
            raise AssertionError("hook launch gate closed before payload execution")
        _write(directory / f"{token}.gate", {"open": True}, deadline)
        result_path = directory / f"{token}.result"
        while not result_path.exists():
            if process.poll() is not None:
                raise AssertionError("hook supervisor exited unexpectedly")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("controlled hook", timeout)
            time.sleep(0.025)
        result = _read(result_path, deadline)
        if result.get("timed_out"):
            raise subprocess.TimeoutExpired("controlled hook", timeout)
        code = result["returncode"]
        if code:
            _stop_registered(directory / f"{token}.json")
            registered = False
        else:
            _write(directory / f"{token}.release", {"release": True}, deadline)
            process.wait(timeout=max(0, min(5, deadline - time.monotonic())))
            _clear(directory, token, deadline)
            registered = False
        return subprocess.CompletedProcess("controlled hook", code)
    finally:
        try:
            if registered:
                _stop_registered(directory / f"{token}.json")
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        # If launch failed before registration, the supervisor sees owner death
        # or its30s gate deadline and never starts the payload.
        if not registered:
            _clear(directory, token)


def _supervise(directory, token):
    request = _read(directory / f"{token}.request")
    result_path = directory / f"{token}.result"
    gate = directory / f"{token}.gate"
    deadline = time.monotonic() + 30
    while not gate.exists():
        if _identity(request["owner"], request["owner_created"]) is None or time.monotonic() >= deadline:
            _write(result_path, {"returncode": 125, "payload_stopped": True})
            return
        time.sleep(0.025)
    if (directory.parent / "hook-shutdown.json").exists() and not request["recovery"]:
        _write(result_path, {"returncode": 125, "payload_stopped": True})
        return
    try:
        _write(directory / f"{token}.started", {"payload_may_have_started": True})
        payload = subprocess.Popen(request["command"], stdin=subprocess.DEVNULL)
    except OSError:
        _write(result_path, {"returncode": 127, "payload_stopped": True})
        return
    timed_out = False
    try:
        code = payload.wait(timeout=request["timeout"])
    except subprocess.TimeoutExpired:
        timed_out = True
        _stop_tree(psutil.Process(payload.pid))
        code = payload.wait(timeout=10)
    _write(result_path, {"returncode": code, "timed_out": timed_out,
                         "payload_stopped": timed_out and os.name == "posix"})
    # Retain the identifiable leader until the caller acknowledges completion.
    # If the caller dies, stop its payload descendants before exiting instead.
    orphan_deadline = None
    while not (directory / f"{token}.release").exists():
        if _identity(request["owner"], request["owner_created"]) is None:
            # Keep the leader alive for the outer watchdog to identify/kill the
            # whole group, including reparented grandchildren. If that watchdog
            # is also gone, fail closed and stop ourselves after a bounded grace.
            orphan_deadline = orphan_deadline or time.monotonic() + 30
            if time.monotonic() >= orphan_deadline:
                _stop_tree(psutil.Process(), group=True)
                return
        time.sleep(0.025)
    _write(result_path, {"returncode": code, "released": True})


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--supervisor":
        raise SystemExit(2)
    _supervise(Path(sys.argv[2]), sys.argv[3])
