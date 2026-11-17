"""Run explicitly configured system-test groups in a disposable lab."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

from .paths import ROOT, TESTS, CONFIG
from .entrypoint import GROUPS, launch_binary, launch_endpoint, log_coverage_errors, log_patterns, preflight, test_paths, watchdog_seconds

# Dispatch before importing third-party cluster dependencies. These modes can
# bootstrap their own client environment on a fresh Ubuntu host.
if __name__ == "__main__" and sys.argv[1:2] == ["binary"]:
    sys.exit(launch_binary(ROOT / "scripts", sys.argv[2:]))
if __name__ == "__main__" and sys.argv[1:2] == ["endpoint"]:
    sys.exit(launch_endpoint(ROOT / "scripts", sys.argv[2:]))

from .helpers import run_hook
from .admin_config import parse_config
from .hook_process import cleanup_hooks, close_hook_gate, run_controlled

CRASH = re.compile(r"WARNING: DATA RACE|fatal error:|panic:|concurrent map (?:read|write)|send on closed channel|close of closed channel", re.I)


def recover(settings, artifacts):
    close_hook_gate(artifacts)
    errors = cleanup_hooks(artifacts)
    if errors:
        return errors  # Never restore while an unverified hook may still mutate.
    pending = artifacts / "pending-recovery.json"
    if pending.exists():
        try:
            state = json.loads(pending.read_text())
            assert isinstance(state, dict) and all(isinstance(v, str) for v in state.values())
            for recovery in set(state.values()):
                try:
                    run_hook(settings, recovery, artifacts, recovery=True)
                except Exception as error:
                    errors.append(f"{recovery}: {type(error).__name__}")
        except Exception as error:
            errors.append(f"fault recovery journal: {type(error).__name__}")
    maintenance = artifacts / "maintenance-pending-recovery.json"
    if maintenance.exists():
        try:
            state = json.loads(maintenance.read_text())
            expected_hooks = {"rebalance": "rebalance_stop", "upgrade": "rollback"}
            assert isinstance(state, dict) and set(state) <= set(expected_hooks)
            for action, entry in list(state.items()):
                try:
                    assert entry["hook"] == expected_hooks[action]
                    assert isinstance(entry["bucket"], str) and entry["bucket"].startswith("dev-release-")
                    options = settings.get("maintenance", {})
                    command = options.get(entry["hook"])
                    assert isinstance(command, list) and command and all(isinstance(v, str) for v in command)
                    environment = os.environ.copy()
                    environment["TMINIO_TEST_BUCKET"] = entry["bucket"]
                    environment["TMINIO_TEST_ARTIFACTS"] = str(artifacts)
                    result = run_controlled(command, artifacts=artifacts, env=environment,
                                            timeout=options.get("hook_timeout", 120), recovery=True, label=entry["hook"])
                    assert result.returncode == 0, "maintenance recovery command failed"
                    del state[action]
                    temporary = maintenance.with_suffix(".tmp")
                    temporary.write_text(json.dumps(state), encoding="utf-8")
                    temporary.replace(maintenance)
                except Exception as error:
                    errors.append(f"maintenance {action}: {type(error).__name__}")
        except Exception as error:
            errors.append(f"maintenance recovery journal: {type(error).__name__}")
    admin = artifacts / "pending-admin-restore.json"
    if admin.exists():
        from .mc import Mc
        executable = shutil.which(settings.get("mc_path", "mc"))
        directory = artifacts / "mc-private"
        cli = Mc(executable or settings.get("mc_path", "mc"), directory, settings)
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            spec = settings["source"]
            if isinstance(spec.get("verify_tls"), str):
                ca_dir = directory / "certs" / "CAs"
                ca_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(spec["verify_tls"], ca_dir / "src.crt")
            cli.run("alias", "set", "src", spec["endpoint"],
                    os.environ[spec.get("access_key_env", "TMINIO_TEST_ACCESS_KEY")],
                    os.environ[spec.get("secret_key_env", "TMINIO_TEST_SECRET_KEY")], "--api", "S3v4")
            state = json.loads(admin.read_text())
            cli.run("admin", "config", "set", "src", state["subsystem"], *state["arguments"])
            restored = parse_config(cli.run("admin", "config", "get", "src", state["subsystem"]), state["subsystem"])
            expected = [state["subsystem"], *state["arguments"]]
            assert sorted(restored) == sorted(expected), "admin restoration read-back differs"
            admin.unlink()
        except Exception as error:
            errors.append(f"admin config restoration: {type(error).__name__}")
        finally:
            if directory.is_dir() and directory.parent == artifacts:
                try:
                    shutil.rmtree(directory)
                except OSError as error:
                    errors.append(f"private mc directory cleanup: {type(error).__name__}")
    return errors


def snapshot_logs(patterns):
    import glob
    snapshots = {}
    for pattern in patterns:
        for name in glob.glob(pattern):
            path = Path(name)
            if path.is_file():
                stat = path.stat()
                snapshots[name] = {"size": stat.st_size, "device": stat.st_dev, "inode": stat.st_ino}
    return snapshots


def scan_logs(patterns, before):
    import glob
    findings, files, errors = [], [], []
    finding_count = 0
    for pattern in patterns:
        for name in glob.glob(pattern):
            path = Path(name)
            if not path.is_file():
                continue
            try:
                with path.open("rb") as stream:
                    previous = before.get(name)
                    stat = os.fstat(stream.fileno())
                    start = previous["size"] if previous else 0
                    if previous and ((stat.st_dev, stat.st_ino) != (previous["device"], previous["inode"])):
                        start = 0  # a replacement can already be larger than the old log
                    if stat.st_size < start:
                        start = 0
                    stream.seek(start)
                    carry = ""
                    while block := stream.read(1024 * 1024):
                        text = carry + block.decode("utf-8", errors="replace")
                        for match in CRASH.finditer(text):
                            if match.end() <= len(carry):
                                continue  # already counted in the previous block
                            finding_count += 1
                            if len(findings) < 1000:
                                findings.append({"file": name, "category": match.group()})
                        carry = text[-128:]
            except OSError as error:
                errors.append({"file": name, "error_type": type(error).__name__})
                continue
            files.append(name)
            # Keep categories only; full logs may contain secrets. Bounded chunks
            # and capped details keep a48h diagnostic scan from exhausting RAM.
    return {"files": files, "coverage": "configured files only" if files else "NOT VERIFIED: no server logs supplied",
            "findings": findings, "finding_count": finding_count, "errors": errors}


def start_test_process(command, environment):
    # An isolated process group lets the watchdog stop hooks/mc as well as pytest.
    return subprocess.Popen(command, env=environment, start_new_session=(os.name == "posix"))


def terminate_process_tree(process):
    """Stop only the process tree/group created by this runner, before recovery."""
    if os.name == "posix":
        # Children may remain even when pytest has already exited. Its dedicated
        # session/group ID remains the pytest PID until those children exit.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            if sig == signal.SIGTERM:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        process.wait(timeout=10)
    elif process.poll() is None:
        # /PID is the exact pytest process we created. /T includes commands that
        # could otherwise finish a fault injection after recovery has started.
        completed = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if completed.returncode and process.poll() is None:
            raise RuntimeError("could not terminate owned pytest process tree")
        process.wait(timeout=10)


def main():
    directory = TESTS
    if sys.argv[1:2] == ["binary"]:
        return launch_binary(ROOT / "scripts", sys.argv[2:])
    if sys.argv[1:2] == ["endpoint"]:
        return launch_endpoint(ROOT / "scripts", sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__, epilog="Examples: ./run.sh lab all | ./run.sh lab replication | ./run.sh unit | ./run.sh lab concurrency -- -k multipart -v")
    parser.add_argument("groups", nargs="*", metavar="GROUP", help="optional groups; omitted means ALL tests (including faults, maintenance and soak)")
    parser.add_argument("--list", action="store_true", help="list groups without reading configuration or contacting servers")
    parser.add_argument("--config", default=str(CONFIG / "lab.local.json"), help="advanced: alternate config (default: config/lab.local.json)")
    parser.add_argument("--artifacts", help="advanced: new evidence directory")
    parser.add_argument("--max-seconds", type=int, help="advanced: override config max_seconds / automatic watchdog")
    parser.add_argument("--recover-only", action="store_true", help="advanced: retry journals in an existing --artifacts directory")
    arguments = sys.argv[1:]
    split = arguments.index("--") if "--" in arguments else len(arguments)
    args = parser.parse_args(arguments[:split])
    pytest_args = arguments[split + 1:] if split < len(arguments) else []
    if args.list:
        print("Binary development check: ./scripts/run-binary.sh /path/to/minio [--work-dir DIR] [--timeout SECONDS]")
        print("  Starts its own localhost server; no cluster configuration or hooks.\n")
        print("Endpoint mode: ./run.sh http://minio.internal:9000 [--timeout SECONDS]")
        print("  Uses a running service and fresh test buckets; no service control or hooks.\n")
        print("Full lab: configure config/lab.local.json, then explicitly run ./run.sh lab all.\n")
        for group, description in GROUPS.items():
            print(f"  {group:12} {description}")
        return 0
    unknown = set(args.groups) - set(GROUPS)
    if unknown:
        parser.error("unknown group(s): " + ", ".join(sorted(unknown)) + "; use --list")
    full = not args.groups and not args.recover_only
    groups = list(dict.fromkeys(args.groups)) if args.groups else list(GROUPS)
    unit_only = groups == ["unit"] and not args.recover_only
    if args.recover_only and (args.groups or pytest_args or not args.artifacts):
        parser.error("--recover-only requires --artifacts and cannot select tests")
    allowed = {"-v", "-vv", "-vvv", "-q", "-s", "--disable-warnings", "--tb=short", "--tb=long", "--tb=line", "--tb=native", "--tb=no"}
    if full and pytest_args:
        # A filtered invocation must be visibly a subset. Collection checks below
        # also reject deselection/skips from plugins or PYTEST_ADDOPTS.
        if any(arg not in allowed for arg in pytest_args):
            parser.error("pytest filtering/options require an explicit group; e.g. concurrency -- -k multipart -v")
    try:
        environment_args = shlex.split(os.getenv("PYTEST_ADDOPTS", ""))
    except ValueError:
        parser.error("PYTEST_ADDOPTS contains invalid quoting")
    if full and any(arg not in allowed | {"-ra", "-rA", "--strict-markers"} for arg in environment_args):
        parser.error("full run requires PYTEST_ADDOPTS without selection/plugin overrides; use an explicit group for filtering")
    if any(arg in ("-n", "--numprocesses") or arg.startswith(("--numprocesses=", "-n=")) or (arg.startswith("-n") and arg[2:].isdigit()) for arg in [*pytest_args, *environment_args]):
        parser.error("pytest-xdist is unsupported: run only one suite per dev cluster")
    # Keep pytest filtering/debugging compatibility without allowing extra test
    # paths, plugins or fixture overrides to escape the chosen group/preflight.
    value_options = {"-k", "-m", "--timeout", "--tb", "--maxfail", "--durations", "--durations-min", "--capture", "--log-cli-level"}
    flag_options = {"-v", "-vv", "-vvv", "-q", "-s", "-x", "--disable-warnings", "--full-trace", "--collect-only"}
    index = 0
    while index < len(pytest_args):
        option = pytest_args[index]
        if option in value_options:
            index += 1
            if index == len(pytest_args):
                parser.error(f"pytest option {option} requires a value")
        elif option not in flag_options and not ("=" in option and option.split("=", 1)[0] in value_options):
            parser.error("unsupported pytest passthrough option; select tests with groups, -k or -m")
        index += 1
    config_path = Path(args.config).resolve()
    try:
        # Unit mode must not execute diagnostic hooks from a nearby dev config.
        settings = {} if unit_only else json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("configuration must be a JSON object")
    except (OSError, ValueError) as error:
        parser.error(f"cannot load {config_path.name}: {type(error).__name__}. Copy config/lab.example.json to config/lab.local.json and complete the one-time setup.")
    artifacts = Path(args.artifacts or Path.cwd() / "test-reports" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    if args.recover_only:
        errors = recover(settings, artifacts)
        report = {"recovery_errors": errors, "production_release_qualified": False}
        (artifacts / "recovery.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
        return bool(errors)
    try:
        max_seconds = watchdog_seconds(settings, groups, args.max_seconds)
    except (ValueError, TypeError) as error:
        parser.error(str(error))
    # A killed parent can leave recovery intent before either final report is
    # written. Never mix a new suite with any previous evidence or credentials.
    if any(artifacts.iterdir()):
        parser.error("choose a new artifacts directory; use --recover-only for interrupted runs")
    validation = preflight(settings, groups, full=full)
    if validation:
        report = {"mode": "full" if full else "subset", "groups": groups,
                  "preflight_errors": validation, "production_release_qualified": False}
        (artifacts / "runner.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("Configuration incomplete; no tests or hooks were started:")
        for error in validation:
            print("  - " + error)
        print(f"Evidence: {artifacts}")
        return 2
    patterns = log_patterns(settings, groups)
    before = snapshot_logs(patterns)
    environment = os.environ.copy()
    environment["TMINIO_TEST_ARTIFACTS"] = str(artifacts)
    environment["PYTHONUNBUFFERED"] = "1"
    # User-facing groups enable their own required capabilities. Direct pytest
    # retains its opt-ins for debugging; callers need no opt-in flag stack here.
    command = [sys.executable, "-m", "pytest", *test_paths(directory, groups, full),
               "--require-complete",
               "--release-groups", ",".join(groups), "--artifacts", str(artifacts),
               "--junitxml", str(artifacts / "junit.xml")]
    if not unit_only:
        command.extend(["--config", str(config_path), "--integration"])
    if set(groups) & {"lifecycle", "admin", "movement"}:
        command.append("--run-admin")
    if set(groups) & {"faults", "replication"}:
        command.append("--run-faults")
    if full:
        command.append("--release-full")
    command.extend(pytest_args)
    returncode, timed_out, interrupted, process = 1, False, False, None
    startup_error, termination_error = None, None
    start = time.monotonic()
    try:
        # Inherit stdout for live progress. JUnit + JSON retain the exact test outcomes.
        process = start_test_process(command, environment)
        try:
            returncode = process.wait(timeout=max_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            print("Parent watchdog deadline reached; terminating pytest and attempting recovery.", flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("Interrupted; attempting recovery.", flush=True)
    except Exception as error:
        startup_error = type(error).__name__
    finally:
        close_hook_gate(artifacts)
        if process:
            try:
                terminate_process_tree(process)
            except Exception as error:
                termination_error = type(error).__name__
        # Fault hooks MUST be bounded/idempotent; remote services outlive pytest.
        if termination_error:
            recovery_errors = ["pytest tree termination failed; recovery deferred to avoid racing an active fault hook"]
        else:
            recovery_errors = recover(settings, artifacts)
    diagnostic = settings.get("diagnostics_hook")
    diagnostic_error = None
    if diagnostic:
        try:
            assert isinstance(diagnostic, list) and all(isinstance(x, str) for x in diagnostic)
            result = subprocess.run(diagnostic, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=environment, timeout=120)
            if result.returncode:
                diagnostic_error = "diagnostics hook failed"
        except Exception:
            diagnostic_error = "diagnostics hook timed out or could not start"
    log_result = scan_logs(patterns, before)
    gate_errors = log_coverage_errors(settings, groups, required=full, after=True)
    try:
        result = json.loads((artifacts / "results.json").read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("completion evidence must be a JSON object")
        if result.get("selected_tests_passed") is not True:
            gate_errors.append("selected tests did not all execute and pass; inspect results.json")
        if full and result.get("full_suite_completed") is not True:
            gate_errors.append("full suite completion was not verified")
    except (OSError, ValueError):
        gate_errors.append("pytest completion evidence is missing or unreadable")
    failed = (returncode != 0 or timed_out or interrupted or bool(recovery_errors) or bool(log_result["findings"]) or bool(log_result["errors"])
              or bool(diagnostic_error) or bool(startup_error) or bool(termination_error) or bool(gate_errors))
    report = {"mode": "full" if full else "subset", "groups": groups, "max_seconds": max_seconds,
              "gate_errors": gate_errors, "pytest_exitstatus": returncode, "parent_timeout": timed_out,
              "interrupted": interrupted,
              "seconds": time.monotonic() - start, "recovery_errors": recovery_errors,
              "diagnostics_error": diagnostic_error, "server_logs": log_result,
              "startup_error": startup_error, "termination_error": termination_error,
              "production_release_qualified": False}
    (artifacts / "runner.json").write_text(json.dumps(report, indent=2))
    print(f"Evidence: {artifacts}")
    print("FAILED" if failed else "Selected tests completed; inspect log coverage and the release matrix. This is not production certification.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
