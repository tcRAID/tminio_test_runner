#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import http.client
import importlib.util
import json
import os
import pathlib
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from typing import Any


ROOT_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_MINIO_DIR = ROOT_DIR.parent / "minio"
DEFAULT_REPORT_DIR = ROOT_DIR / "test-reports"
DEFAULT_LOCUSTFILE = ROOT_DIR / "locustfiles" / "minio_s3.py"
DEFAULT_ACCESS_KEY = ""
DEFAULT_SECRET_KEY = ""
SENSITIVE_ARG_NAMES = {"--access-key", "--secret-key"}


class StepError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def duration_seconds(value: str) -> int:
    value = value.strip().lower()
    if not value:
        raise argparse.ArgumentTypeError("duration cannot be empty")
    unit = value[-1]
    number = value[:-1] if unit in {"s", "m", "h"} else value
    try:
        amount = float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid duration: {value}") from exc
    if amount <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    if unit == "h":
        return int(amount * 3600)
    if unit == "m":
        return int(amount * 60)
    if unit == "s":
        return int(amount)
    return int(amount)


def format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


def redacted_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if arg in SENSITIVE_ARG_NAMES:
            redacted.append(arg)
            redact_next = True
            continue
        matched = next((name for name in SENSITIVE_ARG_NAMES if arg.startswith(f"{name}=")), None)
        if matched:
            redacted.append(f"{matched}=<redacted>")
        else:
            redacted.append(arg)
    return redacted


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise StepError(f"required tool not found: {name}")
    return path


def resolve_minio_dir(path: str | pathlib.Path) -> pathlib.Path:
    minio_dir = pathlib.Path(path).expanduser().resolve()
    if not (minio_dir / "go.mod").is_file():
        raise StepError(f"MinIO source directory does not contain go.mod: {minio_dir}")
    return minio_dir


def make_report_root(base: pathlib.Path, mode: str) -> pathlib.Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = base.expanduser().resolve()
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt:02d}"
        root = base / f"{stamp}-{safe_name(mode)}{suffix}"
        try:
            root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        (root / "logs").mkdir()
        (root / "work").mkdir()
        return root
    raise StepError(f"could not create unique report directory under {base}")


@dataclasses.dataclass
class Result:
    name: str
    status: str
    duration_seconds: float
    details: dict[str, Any]
    error: str | None = None
    log_path: str | None = None


class Report:
    def __init__(self, mode: str, root: pathlib.Path):
        self.mode = mode
        self.root = root
        self.started_at = utc_now()
        self.finished_at: str | None = None
        self.results: list[Result] = []
        self.metadata: dict[str, Any] = {
            "mode": mode,
            "host_platform": sys.platform,
            "python": sys.version,
            "argv": redacted_argv(sys.argv),
        }

    @property
    def log_dir(self) -> pathlib.Path:
        return self.root / "logs"

    @property
    def work_dir(self) -> pathlib.Path:
        return self.root / "work"

    def add(
        self,
        name: str,
        status: str,
        duration: float,
        details: dict[str, Any] | None = None,
        error: str | None = None,
        log_path: pathlib.Path | None = None,
    ) -> None:
        self.results.append(
            Result(
                name=name,
                status=status,
                duration_seconds=duration,
                details=details or {},
                error=error,
                log_path=str(log_path) if log_path else None,
            )
        )

    def passed(self) -> bool:
        return all(item.status == "passed" for item in self.results)

    def finish(self) -> None:
        self.finished_at = utc_now()
        self.write()

    def write(self) -> None:
        payload = {
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed(),
            "metadata": self.metadata,
            "results": [dataclasses.asdict(item) for item in self.results],
        }
        (self.root / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

        lines = [
            f"# MinIO validation report: {self.mode}",
            "",
            f"- Started: {self.started_at}",
            f"- Finished: {self.finished_at or ''}",
            f"- Result: {'PASS' if self.passed() else 'FAIL'}",
            "",
            "| Step | Status | Duration | Log |",
            "| --- | --- | ---: | --- |",
        ]
        for item in self.results:
            log = item.log_path or ""
            if log:
                try:
                    log = pathlib.Path(log).relative_to(self.root).as_posix()
                except ValueError:
                    pass
            lines.append(
                f"| {item.name} | {item.status} | {format_seconds(item.duration_seconds)} | {log} |"
            )
            if item.error:
                lines.append("")
                lines.append(f"Error in `{item.name}`:")
                lines.append("")
                lines.append("```text")
                lines.append(item.error.rstrip())
                lines.append("```")
                lines.append("")
        (self.root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def terminate_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=10)


def popen_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"preexec_fn": os.setsid}
    return {}


def run_command(
    report: Report,
    name: str,
    cmd: list[str],
    cwd: pathlib.Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    allow_failure: bool = False,
    display_cmd: list[str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    log_path = report.log_dir / f"{safe_name(name)}.log"
    logged_cmd = display_cmd or cmd
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"$ {' '.join(logged_cmd)}\n")
        log_file.write(f"# cwd: {cwd}\n\n")
        log_file.flush()
        print(f"[command] {name}: {' '.join(logged_cmd)}")
        proc = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed runner tool invocations without a shell.
            cmd,
            cwd=str(cwd),
            env=merged_env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **popen_kwargs(),
        )
        if input_text is not None and proc.stdin is not None:
            proc.stdin.write(input_text)
            proc.stdin.close()

        def reader() -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                print(line, end="")
                log_file.write(line)
                log_file.flush()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout if timeout else None
        timed_out = False
        while proc.poll() is None:
            if deadline and time.monotonic() > deadline:
                timed_out = True
                log_file.write(f"\n# timeout after {timeout} seconds\n")
                log_file.flush()
                terminate_process(proc)
                break
            time.sleep(0.25)
        thread.join(timeout=5)
        return_code = proc.returncode if proc.returncode is not None else 124

    elapsed = time.monotonic() - started
    status = "passed" if return_code == 0 and not timed_out else "failed"
    error = None
    if timed_out:
        error = f"command timed out after {timeout} seconds"
    elif return_code != 0:
        error = f"command exited with status {return_code}"
    report.add(
        name,
        status if not allow_failure else "passed",
        elapsed,
        details={"command": logged_cmd, "cwd": str(cwd), "exit_code": return_code},
        error=None if allow_failure else error,
        log_path=log_path,
    )
    if return_code != 0 and not allow_failure:
        raise StepError(error or f"command failed: {name}")
    return subprocess.CompletedProcess(cmd, return_code, "", "")


def go_ldflags(minio_dir: pathlib.Path) -> str:
    try:
        go_bin = require_tool("go")
        result = subprocess.run(  # noqa: S603  # nosec B603 - go path is resolved and executed without a shell.
            [go_bin, "run", "buildscripts/gen-ldflags.go"],
            cwd=str(minio_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def build_minio(
    report: Report,
    minio_dir: pathlib.Path,
    output: pathlib.Path,
    build_tags: str = "kqueue",
) -> pathlib.Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["go", "build", "-tags", build_tags, "-trimpath"]
    ldflags = go_ldflags(minio_dir)
    if ldflags:
        cmd.extend(["-ldflags", ldflags])
    cmd.extend(["-o", str(output), "."])
    run_command(
        report,
        "go-build-minio",
        cmd,
        cwd=minio_dir,
        env={"CGO_ENABLED": "0"},
        timeout=3600,
    )
    output.chmod(output.stat().st_mode | 0o111)
    return output


@dataclasses.dataclass
class LocalMinio:
    process: subprocess.Popen[Any]
    endpoint: str
    data_dir: pathlib.Path
    log_path: pathlib.Path
    api_port: int
    console_port: int
    drive_dirs: list[pathlib.Path]


def kill_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        return
    proc.wait(timeout=10)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_local_minio(api_port: int, proc: subprocess.Popen[Any], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise StepError(f"local MinIO exited before becoming healthy with status {proc.returncode}")
        conn: http.client.HTTPConnection | None = None
        try:
            conn = http.client.HTTPConnection("127.0.0.1", api_port, timeout=2)
            conn.request("GET", "/minio/health/ready")
            response = conn.getresponse()
            response.read()
            if response.status == 200:
                return
            last_error = f"readiness check returned HTTP {response.status}"
        except Exception as exc:
            last_error = str(exc)
        finally:
            if conn is not None:
                conn.close()
        time.sleep(0.5)
    raise StepError(f"local MinIO did not become ready within {timeout}s: {last_error}")


def start_local_minio(
    report: Report,
    binary: pathlib.Path,
    access_key: str,
    secret_key: str,
    startup_timeout: int,
    data_dir: pathlib.Path | None = None,
    api_port: int | None = None,
    console_port: int | None = None,
    step_name: str = "local-minio-start",
    cleanup_data_on_failure: bool = True,
    drive_dirs: list[pathlib.Path] | None = None,
) -> LocalMinio:
    started = time.monotonic()
    if data_dir is None:
        data_dir = report.work_dir / "local-minio-data"
    else:
        data_dir = data_dir
    if drive_dirs is None:
        data_dir.mkdir(parents=True, exist_ok=data_dir.exists())
        storage_dirs = [data_dir]
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
        storage_dirs = drive_dirs
        for drive_dir in storage_dirs:
            drive_dir.mkdir(parents=True, exist_ok=True)
    if api_port is None:
        api_port = find_free_port()
    if console_port is None:
        console_port = find_free_port()
    while console_port == api_port:
        console_port = find_free_port()
    endpoint = f"http://127.0.0.1:{api_port}"
    log_path = report.log_dir / f"{safe_name(step_name)}.log"
    cmd = [
        str(binary),
        "server",
        *[str(drive_dir) for drive_dir in storage_dirs],
        "--address",
        f"127.0.0.1:{api_port}",
        "--console-address",
        f"127.0.0.1:{console_port}",
    ]
    env = os.environ.copy()
    env.update(
        {
            "MINIO_ROOT_USER": access_key,
            "MINIO_ROOT_PASSWORD": secret_key,
            "MINIO_BROWSER": "off",
            # File-backed erasure drives in this runner live under the test
            # workspace, so they need the same CI override used by MinIO's own
            # healing scripts to bypass root-disk protection.
            "MINIO_CI_CD": "1",
        }
    )
    proc: subprocess.Popen[Any] | None = None
    try:
        log_file = log_path.open("w", encoding="utf-8", errors="replace")
        try:
            log_file.write(f"$ {' '.join(cmd)}\n")
            log_file.write(f"# cwd: {ROOT_DIR}\n\n")
            log_file.flush()
            print(f"[command] {step_name}: {' '.join(cmd)}")
            proc = subprocess.Popen(  # noqa: S603  # nosec B603 - runner-built binary executed without a shell.
                cmd,
                cwd=str(ROOT_DIR),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                **popen_kwargs(),
            )
        finally:
            log_file.close()

        if proc is None:
            raise StepError("local MinIO process was not started")
        wait_for_local_minio(api_port, proc, startup_timeout)
    except Exception as exc:
        if proc is not None:
            terminate_process(proc)
        if cleanup_data_on_failure:
            shutil.rmtree(data_dir, ignore_errors=True)
        report.add(
            step_name,
            "failed",
            time.monotonic() - started,
            details={
                "endpoint": endpoint,
                "api_port": api_port,
                "console_port": console_port,
                "drive_dirs": [str(drive_dir) for drive_dir in storage_dirs],
            },
            error="".join(traceback.format_exception_only(type(exc), exc)).strip(),
            log_path=log_path,
        )
        raise

    report.add(
        step_name,
        "passed",
        time.monotonic() - started,
        details={
            "endpoint": endpoint,
            "api_port": api_port,
            "console_port": console_port,
            "drive_dirs": [str(drive_dir) for drive_dir in storage_dirs],
        },
        log_path=log_path,
    )
    return LocalMinio(proc, endpoint, data_dir, log_path, api_port, console_port, storage_dirs)


def stop_local_minio(
    report: Report,
    local: LocalMinio,
    *,
    remove_data: bool = True,
    force: bool = False,
    step_name: str = "local-minio-stop",
) -> None:
    started = time.monotonic()
    error = None
    try:
        if force:
            kill_process(local.process)
        else:
            terminate_process(local.process)
    except Exception as exc:
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    if remove_data:
        shutil.rmtree(local.data_dir, ignore_errors=True)
    if remove_data and local.data_dir.exists():
        error = f"failed to remove local MinIO data directory: {local.data_dir}"
    report.add(
        step_name,
        "failed" if error else "passed",
        time.monotonic() - started,
        details={
            "endpoint": local.endpoint,
            "data_dir": str(local.data_dir),
            "drive_dirs": [str(drive_dir) for drive_dir in local.drive_dirs],
            "remove_data": remove_data,
            "force": force,
        },
        error=error,
        log_path=local.log_path,
    )


def run_source(args: argparse.Namespace) -> int:
    report_root = make_report_root(pathlib.Path(args.report_dir), "source")
    report = Report("source", report_root)
    try:
        minio_dir = resolve_minio_dir(args.minio_dir)
        report.metadata["minio_dir"] = str(minio_dir)
        require_tool("go")
        run_command(report, "go-env", ["go", "env"], cwd=minio_dir, timeout=120)
        if not args.skip_build:
            build_minio(
                report,
                minio_dir,
                report.work_dir / "bin" / "minio",
                build_tags=args.build_tags,
            )

        if not args.skip_tests:
            cmd = [
                "go",
                "test",
                "-count=1",
                "-timeout",
                args.timeout,
                "-tags",
                args.test_tags,
                "-v",
            ]
            if args.short:
                cmd.append("-short")
            if args.run:
                cmd.extend(["-run", args.run])
            cmd.extend(args.packages)
            run_command(
                report,
                "go-test",
                cmd,
                cwd=minio_dir,
                env={"MINIO_API_REQUESTS_MAX": "10000", "CGO_ENABLED": "0"},
                timeout=duration_seconds(args.timeout),
            )

        if args.race:
            cmd = [
                "go",
                "test",
                "-count=1",
                "-race",
                "-timeout",
                args.race_timeout,
                "-tags",
                args.test_tags,
                "-v",
            ]
            if args.run:
                cmd.extend(["-run", args.run])
            cmd.extend(args.packages)
            run_command(
                report,
                "go-test-race",
                cmd,
                cwd=minio_dir,
                env={"MINIO_API_REQUESTS_MAX": "10000", "CGO_ENABLED": "1", "GORACE": "history_size=7"},
                timeout=duration_seconds(args.race_timeout),
            )
    except Exception as exc:
        report.add(
            "runner",
            "failed",
            0,
            error="".join(traceback.format_exception_only(type(exc), exc)).strip(),
        )
    finally:
        if not args.keep_workdir and report.passed():
            shutil.rmtree(report.work_dir, ignore_errors=True)
        report.finish()
        print(f"\nReport: {report.root}")
    return 0 if report.passed() else 1


def require_python_module(name: str, install_hint: str) -> None:
    if importlib.util.find_spec(name) is None:
        raise StepError(f"required Python module not found: {name}. Install with: {install_hint}")


def locust_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if secs:
        return f"{seconds}s"
    if hours and minutes:
        return f"{hours}h{minutes}m"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def record_python_step(report: Report, name: str, func: Any) -> Any:
    started = time.monotonic()
    try:
        details = func()
    except Exception as exc:
        report.add(
            name,
            "failed",
            time.monotonic() - started,
            error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip(),
        )
        raise
    report.add(name, "passed", time.monotonic() - started, details=details if isinstance(details, dict) else {})
    return details


def new_s3_client(endpoint: str, access_key: str, secret_key: str) -> Any:
    require_python_module("boto3", "python3 -m pip install -r minio-test-requirements.txt")
    require_python_module("botocore", "python3 -m pip install -r minio-test-requirements.txt")
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 4, "mode": "standard"},
        ),
    )


def client_error_code(exc: Exception) -> tuple[str, str]:
    response = getattr(exc, "response", {})
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    return str(error.get("Code", "")), str(metadata.get("HTTPStatusCode", ""))


def expect_client_error(name: str, func: Any, expected: set[str]) -> None:
    try:
        func()
    except Exception as exc:
        code, status = client_error_code(exc)
        if code in expected or status in expected:
            return
        raise StepError(f"{name} returned unexpected error {code or status}: {exc}") from exc
    raise StepError(f"{name} succeeded; expected one of {sorted(expected)}")


def checked_delete_objects(client: Any, bucket: str, objects: list[dict[str, str]]) -> int:
    deleted = 0
    for index in range(0, len(objects), 1000):
        batch = objects[index : index + 1000]
        if not batch:
            continue
        response = client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        errors = response.get("Errors", [])
        if errors:
            raise StepError(f"DeleteObjects returned errors: {errors[:3]}")
        deleted += len(batch)
    return deleted


def ops_prepare_state(client: Any, bucket: str) -> dict[str, Any]:
    stable_body = b"ops persistent object\n"
    version_v1_body = b"version-one"
    version_v2_body = b"version-two"

    client.create_bucket(Bucket=bucket)
    client.head_bucket(Bucket=bucket)
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    time.sleep(1)
    client.put_bucket_tagging(
        Bucket=bucket,
        Tagging={"TagSet": [{"Key": "suite", "Value": "ops"}, {"Key": "restart", "Value": "true"}]},
    )
    client.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "ops-expire-tmp",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "tmp/"},
                    "Expiration": {"Days": 1},
                }
            ]
        },
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/public/*"],
            }
        ],
    }
    client.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    client.put_object(Bucket=bucket, Key="state/persist.txt", Body=stable_body, Metadata={"sha256": "ops-stable"})
    v1 = client.put_object(Bucket=bucket, Key="state/versioned.txt", Body=version_v1_body)["VersionId"]
    v2 = client.put_object(Bucket=bucket, Key="state/versioned.txt", Body=version_v2_body)["VersionId"]
    marker = client.delete_object(Bucket=bucket, Key="state/versioned.txt")

    return {
        "bucket": bucket,
        "stable_key": "state/persist.txt",
        "stable_sha256": hashlib_bytes(stable_body),
        "versioned_key": "state/versioned.txt",
        "version_v1": v1,
        "version_v1_sha256": hashlib_bytes(version_v1_body),
        "version_v2": v2,
        "delete_marker": marker.get("VersionId", ""),
    }


def hashlib_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def verify_ops_state(client: Any, state: dict[str, Any]) -> dict[str, Any]:
    bucket = state["bucket"]
    client.head_bucket(Bucket=bucket)

    stable = client.get_object(Bucket=bucket, Key=state["stable_key"])["Body"].read()
    if hashlib_bytes(stable) != state["stable_sha256"]:
        raise StepError("persistent object body changed across restart")

    v1 = client.get_object(Bucket=bucket, Key=state["versioned_key"], VersionId=state["version_v1"])["Body"].read()
    if hashlib_bytes(v1) != state["version_v1_sha256"]:
        raise StepError("versioned object body changed across restart")

    versioning = client.get_bucket_versioning(Bucket=bucket)
    if versioning.get("Status") != "Enabled":
        raise StepError(f"bucket versioning did not persist: {versioning}")

    tags = client.get_bucket_tagging(Bucket=bucket)["TagSet"]
    if {"Key": "suite", "Value": "ops"} not in tags:
        raise StepError(f"bucket tagging did not persist: {tags}")

    lifecycle = client.get_bucket_lifecycle_configuration(Bucket=bucket)
    rule_ids = {rule.get("ID") for rule in lifecycle.get("Rules", [])}
    if "ops-expire-tmp" not in rule_ids:
        raise StepError(f"lifecycle config did not persist: {lifecycle}")

    policy = json.loads(client.get_bucket_policy(Bucket=bucket)["Policy"])
    if not policy.get("Statement"):
        raise StepError("bucket policy did not persist")

    versions = client.list_object_versions(Bucket=bucket, Prefix=state["versioned_key"])
    version_ids = {item.get("VersionId") for item in versions.get("Versions", [])}
    marker_ids = {item.get("VersionId") for item in versions.get("DeleteMarkers", [])}
    if state["version_v1"] not in version_ids or state["version_v2"] not in version_ids:
        raise StepError(f"object versions missing after restart: {versions}")
    if state["delete_marker"] and state["delete_marker"] not in marker_ids:
        raise StepError(f"delete marker missing after restart: {versions}")

    return {"versions": len(version_ids), "delete_markers": len(marker_ids)}


def ops_write_crash_probe(client: Any, state: dict[str, Any]) -> dict[str, Any]:
    body = b"written before forced process stop\n"
    client.put_object(Bucket=state["bucket"], Key="state/crash-probe.txt", Body=body)
    state["crash_key"] = "state/crash-probe.txt"
    state["crash_sha256"] = hashlib_bytes(body)
    return {"crash_key": state["crash_key"]}


def verify_crash_probe(client: Any, state: dict[str, Any]) -> dict[str, Any]:
    body = client.get_object(Bucket=state["bucket"], Key=state["crash_key"])["Body"].read()
    if hashlib_bytes(body) != state["crash_sha256"]:
        raise StepError("crash probe object body changed after forced restart")
    return {"crash_key": state["crash_key"]}


def ensure_local_minio_running(local: LocalMinio, context: str) -> None:
    if local.process.poll() is not None:
        raise StepError(f"local MinIO exited during {context} with status {local.process.returncode}")


def read_object_body(client: Any, bucket: str, key: str) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def expect_request_failure(name: str, func: Any) -> dict[str, str]:
    try:
        func()
    except Exception as exc:
        code, status = client_error_code(exc)
        return {
            "operation": name,
            "exception": type(exc).__name__,
            "error_code": code,
            "http_status": status,
        }
    raise StepError(f"{name} succeeded during fault injection")


def wait_for_object_hash(client: Any, bucket: str, key: str, expected_sha256: str, timeout: int = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            body = read_object_body(client, bucket, key)
            got_sha256 = hashlib_bytes(body)
            if got_sha256 == expected_sha256:
                return {"key": key, "sha256": got_sha256}
            last_error = f"object hash mismatch: got {got_sha256}"
        except Exception as exc:
            last_error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        time.sleep(0.5)
    raise StepError(f"object {key} did not become readable within {timeout}s: {last_error}")


def fault_prepare_state(client: Any, bucket: str) -> dict[str, Any]:
    baseline_body = b"fault baseline object\n"
    corrupt_body = b"fault corrupt target\n" * 1024
    client.create_bucket(Bucket=bucket)
    client.put_object(Bucket=bucket, Key="fault/baseline.txt", Body=baseline_body)
    client.put_object(Bucket=bucket, Key="fault/corrupt-target.bin", Body=corrupt_body)
    return {
        "bucket": bucket,
        "baseline_key": "fault/baseline.txt",
        "baseline_sha256": hashlib_bytes(baseline_body),
        "corrupt_key": "fault/corrupt-target.bin",
        "corrupt_sha256": hashlib_bytes(corrupt_body),
    }


def restore_fault_drive(data_dir: pathlib.Path, removed_dir: pathlib.Path) -> dict[str, str]:
    details = {"restored_dir": str(data_dir), "removed_dir": str(removed_dir)}
    if not removed_dir.exists():
        raise StepError(f"removed drive directory is missing: {removed_dir}")
    if data_dir.exists():
        replacement_dir = data_dir.with_name(f"{data_dir.name}.created-during-fault")
        if replacement_dir.exists():
            shutil.rmtree(replacement_dir, ignore_errors=True)
        data_dir.rename(replacement_dir)
        details["created_during_fault_dir"] = str(replacement_dir)
    removed_dir.rename(data_dir)
    return details


def fault_disk_removed_put_get(client: Any, local: LocalMinio, state: dict[str, Any]) -> dict[str, Any]:
    data_dir = local.data_dir
    removed_dir = data_dir.with_name(f"{data_dir.name}.removed")
    if removed_dir.exists():
        shutil.rmtree(removed_dir, ignore_errors=True)
    if not data_dir.exists():
        raise StepError(f"local MinIO drive does not exist: {data_dir}")

    details: dict[str, Any] = {"data_dir": str(data_dir), "removed_dir": str(removed_dir)}
    data_dir.rename(removed_dir)
    try:
        time.sleep(0.5)
        details["put_error"] = expect_request_failure(
            "PutObject with removed drive",
            lambda: client.put_object(Bucket=state["bucket"], Key="fault/disk-removed-put.txt", Body=b"must fail"),
        )
        details["get_error"] = expect_request_failure(
            "GetObject with removed drive",
            lambda: read_object_body(client, state["bucket"], state["baseline_key"]),
        )
        ensure_local_minio_running(local, "removed-drive PUT/GET")
    finally:
        details["restore"] = restore_fault_drive(data_dir, removed_dir)
    return details


def path_contains_sequence(parts: tuple[str, ...], sequence: list[str]) -> bool:
    index = 0
    for part in parts:
        if part == sequence[index]:
            index += 1
            if index == len(sequence):
                return True
    return False


def find_object_files(data_dir: pathlib.Path, bucket: str, key: str) -> list[pathlib.Path]:
    key_parts = [part for part in pathlib.PurePosixPath(key).parts if part not in {"", "/"}]
    sequence = [bucket, *key_parts]
    candidates: list[pathlib.Path] = []
    for path in data_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(data_dir).parts
        except ValueError:
            continue
        if bucket not in rel_parts:
            continue
        if path_contains_sequence(rel_parts, sequence):
            candidates.append(path)
    return sorted(candidates, key=lambda item: str(item.relative_to(data_dir)))


def corrupt_object_files(data_dir: pathlib.Path, bucket: str, key: str) -> dict[str, Any]:
    candidates = find_object_files(data_dir, bucket, key)
    if not candidates:
        raise StepError(f"could not find on-disk object files for s3://{bucket}/{key}")

    corrupted: list[dict[str, Any]] = []
    for path in candidates[:8]:
        size = path.stat().st_size
        with path.open("r+b") as file_obj:
            chunk = file_obj.read(64)
            file_obj.seek(0)
            if chunk:
                file_obj.write(bytes(value ^ 0xFF for value in chunk))
                bytes_written = len(chunk)
            else:
                file_obj.write(b"corrupt")
                bytes_written = len(b"corrupt")
        corrupted.append(
            {
                "path": str(path.relative_to(data_dir)),
                "original_size": size,
                "bytes_written": bytes_written,
            }
        )

    return {"bucket": bucket, "key": key, "corrupted_files": corrupted}


def fault_corrupt_get_fails(client: Any, local: LocalMinio, state: dict[str, Any]) -> dict[str, Any]:
    error = expect_request_failure(
        "GetObject with corrupted object file",
        lambda: read_object_body(client, state["bucket"], state["corrupt_key"]),
    )
    ensure_local_minio_running(local, "corrupted-object GET")
    return {"get_error": error}


def fault_erasure_remove_drive_put(
    client: Any,
    local: LocalMinio,
    bucket: str,
    drive_dir: pathlib.Path,
) -> dict[str, Any]:
    removed_dir = drive_dir.with_name(f"{drive_dir.name}.removed")
    if removed_dir.exists():
        shutil.rmtree(removed_dir, ignore_errors=True)
    if not drive_dir.exists():
        raise StepError(f"erasure drive does not exist: {drive_dir}")

    body = (b"fault erasure heal target\n" * 65536)[:1024 * 1024]
    key = "fault/heal-after-readd.bin"
    details: dict[str, Any] = {
        "bucket": bucket,
        "heal_key": key,
        "drive_dir": str(drive_dir),
        "removed_dir": str(removed_dir),
        "heal_sha256": hashlib_bytes(body),
    }

    drive_dir.rename(removed_dir)
    try:
        time.sleep(1)
        client.put_object(Bucket=bucket, Key=key, Body=body)
        wait_for_object_hash(client, bucket, key, details["heal_sha256"])
        ensure_local_minio_running(local, "erasure removed-drive PUT")
        details["removed_drive_object_files_before_heal"] = [
            str(path.relative_to(removed_dir)) for path in find_object_files(removed_dir, bucket, key)
        ]
        if details["removed_drive_object_files_before_heal"]:
            raise StepError("heal target unexpectedly exists on removed drive before re-add")
        if drive_dir.exists():
            details["recreated_drive_object_files_before_heal"] = [
                str(path.relative_to(drive_dir)) for path in find_object_files(drive_dir, bucket, key)
            ]
            if details["recreated_drive_object_files_before_heal"]:
                raise StepError("heal target unexpectedly exists on recreated drive path before re-add")
    except Exception:
        if removed_dir.exists() and not drive_dir.exists():
            removed_dir.rename(drive_dir)
        raise
    return details


def fault_create_erasure_bucket(client: Any, bucket: str) -> dict[str, str]:
    client.create_bucket(Bucket=bucket)
    client.head_bucket(Bucket=bucket)
    return {"bucket": bucket}


def fault_restore_drive_and_wait(local: LocalMinio, drive_dir: pathlib.Path, removed_dir: pathlib.Path) -> dict[str, str]:
    details = restore_fault_drive(drive_dir, removed_dir)
    time.sleep(2)
    ensure_local_minio_running(local, "erasure drive re-add")
    return details


def fault_verify_readd_heal(client: Any, state: dict[str, Any], timeout: int) -> dict[str, Any]:
    bucket = state["bucket"]
    key = state["heal_key"]
    drive_dir = pathlib.Path(state["drive_dir"])

    readable = wait_for_object_hash(client, bucket, key, state["heal_sha256"], timeout=timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        healed_files = find_object_files(drive_dir, bucket, key)
        if healed_files and any(path.name == "xl.meta" for path in healed_files) and any(path.name.startswith("part.") for path in healed_files):
            return {
                "readable": readable,
                "healed_drive_dir": str(drive_dir),
                "healed_files": [str(path.relative_to(drive_dir)) for path in healed_files],
            }
        time.sleep(1)
    raise StepError(f"heal target was readable, but xl.meta and data parts did not appear on re-added drive: {drive_dir}")


def purge_bucket_strict(client: Any, bucket: str) -> dict[str, Any]:
    deleted_versions = 0
    paginator = client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objects: list[dict[str, str]] = []
        for item in page.get("Versions", []):
            objects.append({"Key": item["Key"], "VersionId": item["VersionId"]})
        for item in page.get("DeleteMarkers", []):
            objects.append({"Key": item["Key"], "VersionId": item["VersionId"]})
        deleted_versions += checked_delete_objects(client, bucket, objects)

    deleted_objects = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        deleted_objects += checked_delete_objects(client, bucket, objects)

    versions = client.list_object_versions(Bucket=bucket)
    if versions.get("Versions") or versions.get("DeleteMarkers"):
        raise StepError(f"bucket still has versions after purge: {versions}")
    listed = client.list_objects_v2(Bucket=bucket)
    if listed.get("Contents"):
        raise StepError(f"bucket still has current objects after purge: {listed}")

    for name, func, expected in (
        ("delete_bucket_policy", client.delete_bucket_policy, {"NoSuchBucketPolicy", "NoSuchBucket", "404"}),
        ("delete_bucket_lifecycle", client.delete_bucket_lifecycle, {"NoSuchLifecycleConfiguration", "NoSuchBucket", "404"}),
        ("delete_bucket_tagging", client.delete_bucket_tagging, {"NoSuchTagSet", "NoSuchBucket", "404"}),
    ):
        try:
            func(Bucket=bucket)
        except Exception as exc:
            code, status = client_error_code(exc)
            if code not in expected and status not in expected:
                raise StepError(f"{name} returned unexpected error {code or status}: {exc}") from exc

    client.delete_bucket(Bucket=bucket)
    expect_client_error("head deleted bucket", lambda: client.head_bucket(Bucket=bucket), {"NoSuchBucket", "404"})

    return {
        "deleted_versions": deleted_versions,
        "deleted_objects": deleted_objects,
    }


def run_smoke(args: argparse.Namespace) -> int:
    report_root = make_report_root(pathlib.Path(args.report_dir), "smoke")
    report = Report("smoke", report_root)
    locustfile = pathlib.Path(args.locustfile).expanduser().resolve()
    class_name = "MinioSmokeUser"
    duration = args.duration
    csv_prefix = report.root / "locust-smoke"
    html_path = report.root / "locust-smoke.html"
    locust_log_path = report.log_dir / "locust-smoke.internal.log"
    local_minio: LocalMinio | None = None
    try:
        if args.users != 1:
            raise StepError("smoke mode requires --users 1; use longrun/minio_long.py directly for concurrent workloads")
        minio_dir = resolve_minio_dir(args.minio_dir)
        access_key = args.access_key or f"tminio-{secrets.token_hex(8)}"
        secret_key = args.secret_key or secrets.token_urlsafe(32)
        report.metadata["minio_dir"] = str(minio_dir)
        require_tool("go")
        require_python_module(
            "locust",
            "python3 -m pip install -r minio-test-requirements.txt",
        )
        require_python_module(
            "boto3",
            "python3 -m pip install -r minio-test-requirements.txt",
        )
        if not locustfile.is_file():
            raise StepError(f"Locust file not found: {locustfile}")

        binary = build_minio(
            report,
            minio_dir,
            report.work_dir / "bin" / "minio",
            build_tags=args.build_tags,
        )
        local_minio = start_local_minio(report, binary, access_key, secret_key, args.startup_timeout)
        endpoint = local_minio.endpoint
        bucket_prefix = args.bucket_prefix or f"minio-test-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
        env = {
            "MINIO_ENDPOINT": endpoint,
            "MINIO_ACCESS_KEY": access_key,
            "MINIO_SECRET_KEY": secret_key,
            "MINIO_TEST_BUCKET_PREFIX": bucket_prefix,
            "MINIO_TEST_CLEANUP": "1",
        }
        report.metadata.update(
            {
                "endpoint": endpoint,
                "local_minio": True,
                "locustfile": str(locustfile),
                "locust_user_class": class_name,
                "users": args.users,
                "spawn_rate": args.spawn_rate,
                "duration_seconds": duration,
                "bucket_prefix": bucket_prefix,
                "cleanup": True,
            }
        )

        cmd = [
            sys.executable,
            "-m",
            "locust",
            "-f",
            str(locustfile),
            class_name,
            "--headless",
            "--host",
            endpoint,
            "--users",
            str(args.users),
            "--spawn-rate",
            str(args.spawn_rate),
            "--run-time",
            locust_duration(duration),
            "--csv",
            str(csv_prefix),
            "--html",
            str(html_path),
            "--logfile",
            str(locust_log_path),
            "--exit-code-on-error",
            "1",
        ]
        if args.stop_timeout:
            cmd.extend(["--stop-timeout", str(args.stop_timeout)])
        for extra_arg in args.locust_arg:
            cmd.append(extra_arg)

        run_command(
            report,
            "locust-smoke",
            cmd,
            cwd=ROOT_DIR,
            env=env,
            timeout=duration + args.command_timeout_padding,
        )
    except Exception as exc:
        report.add(
            "runner",
            "failed",
            0,
            error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip(),
        )
    finally:
        if local_minio is not None:
            stop_local_minio(report, local_minio)
        if not args.keep_workdir and report.passed():
            shutil.rmtree(report.work_dir, ignore_errors=True)
        report.finish()
        print(f"\nReport: {report.root}")
    return 0 if report.passed() else 1


def cleanup_data_dir_step(report: Report, data_dir: pathlib.Path | None) -> None:
    if data_dir is None or not data_dir.exists():
        return
    started = time.monotonic()
    error = None
    shutil.rmtree(data_dir, ignore_errors=True)
    if data_dir.exists():
        error = f"failed to remove local MinIO data directory: {data_dir}"
    report.add(
        "local-minio-cleanup-data",
        "failed" if error else "passed",
        time.monotonic() - started,
        details={"data_dir": str(data_dir)},
        error=error,
    )


def run_ops(args: argparse.Namespace) -> int:
    report_root = make_report_root(pathlib.Path(args.report_dir), "ops")
    report = Report("ops", report_root)
    local_minio: LocalMinio | None = None
    data_dir: pathlib.Path | None = None
    state: dict[str, Any] = {}
    try:
        minio_dir = resolve_minio_dir(args.minio_dir)
        access_key = args.access_key or f"tminio-{secrets.token_hex(8)}"
        secret_key = args.secret_key or secrets.token_urlsafe(32)
        bucket_prefix = args.bucket_prefix or f"minio-ops-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
        bucket = f"{bucket_prefix.strip('-').lower()}-{secrets.token_hex(6)}"
        data_dir = report.work_dir / "ops-minio-data"

        report.metadata.update(
            {
                "minio_dir": str(minio_dir),
                "bucket": bucket,
                "bucket_prefix": bucket_prefix,
                "local_minio": True,
                "coverage": [
                    "clean restart persistence",
                    "forced restart persistence",
                    "bucket lifecycle config persistence",
                    "strict version/object purge",
                ],
            }
        )
        require_tool("go")
        require_python_module("boto3", "python3 -m pip install -r minio-test-requirements.txt")
        require_python_module("botocore", "python3 -m pip install -r minio-test-requirements.txt")

        binary = build_minio(report, minio_dir, report.work_dir / "bin" / "minio", build_tags=args.build_tags)

        local_minio = start_local_minio(
            report,
            binary,
            access_key,
            secret_key,
            args.startup_timeout,
            data_dir=data_dir,
            step_name="ops-minio-start",
            cleanup_data_on_failure=False,
        )
        client = new_s3_client(local_minio.endpoint, access_key, secret_key)
        state = record_python_step(report, "ops-prepare-state", lambda: ops_prepare_state(client, bucket))

        stop_local_minio(report, local_minio, remove_data=False, step_name="ops-minio-stop-clean")
        local_minio = None

        local_minio = start_local_minio(
            report,
            binary,
            access_key,
            secret_key,
            args.startup_timeout,
            data_dir=data_dir,
            step_name="ops-minio-restart-clean",
            cleanup_data_on_failure=False,
        )
        client = new_s3_client(local_minio.endpoint, access_key, secret_key)
        record_python_step(report, "ops-verify-clean-restart", lambda: verify_ops_state(client, state))
        record_python_step(report, "ops-write-crash-probe", lambda: ops_write_crash_probe(client, state))

        stop_local_minio(report, local_minio, remove_data=False, force=True, step_name="ops-minio-stop-sigkill")
        local_minio = None

        local_minio = start_local_minio(
            report,
            binary,
            access_key,
            secret_key,
            args.startup_timeout,
            data_dir=data_dir,
            step_name="ops-minio-restart-after-sigkill",
            cleanup_data_on_failure=False,
        )
        client = new_s3_client(local_minio.endpoint, access_key, secret_key)
        record_python_step(report, "ops-verify-sigkill-restart", lambda: verify_crash_probe(client, state))
        record_python_step(report, "ops-purge-bucket", lambda: purge_bucket_strict(client, bucket))
    except Exception as exc:
        report.add(
            "runner",
            "failed",
            0,
            error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip(),
        )
    finally:
        if local_minio is not None:
            stop_local_minio(report, local_minio, remove_data=True, step_name="ops-minio-stop-final")
        else:
            cleanup_data_dir_step(report, data_dir)
        if not args.keep_workdir and report.passed():
            shutil.rmtree(report.work_dir, ignore_errors=True)
        report.finish()
        print(f"\nReport: {report.root}")
    return 0 if report.passed() else 1


def run_fault(args: argparse.Namespace) -> int:
    report_root = make_report_root(pathlib.Path(args.report_dir), "fault")
    report = Report("fault", report_root)
    local_minio: LocalMinio | None = None
    data_dir: pathlib.Path | None = None
    state: dict[str, Any] = {}
    try:
        minio_dir = resolve_minio_dir(args.minio_dir)
        access_key = args.access_key or f"tminio-{secrets.token_hex(8)}"
        secret_key = args.secret_key or secrets.token_urlsafe(32)
        bucket_prefix = args.bucket_prefix or f"minio-fault-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
        bucket = f"{bucket_prefix.strip('-').lower()}-{secrets.token_hex(6)}"
        if args.heal_drive_count < 4:
            raise StepError("--heal-drive-count must be at least 4")
        data_dir = report.work_dir / "fault-minio-data"
        single_drive_dir = data_dir / "single-drive"
        erasure_root = data_dir / "erasure"

        report.metadata.update(
            {
                "minio_dir": str(minio_dir),
                "bucket": bucket,
                "bucket_prefix": bucket_prefix,
                "local_minio": True,
                "heal_drive_count": args.heal_drive_count,
                "coverage": [
                    "PUT failure while local drive path is removed",
                    "GET failure while local drive path is removed",
                    "GET failure after object file corruption",
                    "write while one erasure drive is missing",
                    "re-add missing erasure drive and run admin heal",
                    "verify healed object appears on re-added drive",
                    "MinIO process remains running during injected request failures",
                ],
            }
        )
        require_tool("go")
        require_tool("mc")
        require_python_module("boto3", "python3 -m pip install -r minio-test-requirements.txt")
        require_python_module("botocore", "python3 -m pip install -r minio-test-requirements.txt")

        binary = build_minio(report, minio_dir, report.work_dir / "bin" / "minio", build_tags=args.build_tags)

        local_minio = start_local_minio(
            report,
            binary,
            access_key,
            secret_key,
            args.startup_timeout,
            data_dir=single_drive_dir,
            step_name="fault-minio-start",
            cleanup_data_on_failure=False,
        )
        client = new_s3_client(local_minio.endpoint, access_key, secret_key)
        state = record_python_step(report, "fault-prepare-state", lambda: fault_prepare_state(client, bucket))
        record_python_step(report, "fault-disk-removed-put-get", lambda: fault_disk_removed_put_get(client, local_minio, state))
        record_python_step(
            report,
            "fault-verify-drive-restored",
            lambda: wait_for_object_hash(client, state["bucket"], state["baseline_key"], state["baseline_sha256"]),
        )

        stop_local_minio(report, local_minio, remove_data=False, step_name="fault-minio-stop-before-corrupt")
        local_minio = None
        record_python_step(
            report,
            "fault-corrupt-object-file",
            lambda: corrupt_object_files(single_drive_dir, state["bucket"], state["corrupt_key"]),
        )

        local_minio = start_local_minio(
            report,
            binary,
            access_key,
            secret_key,
            args.startup_timeout,
            data_dir=single_drive_dir,
            step_name="fault-minio-restart-after-corrupt",
            cleanup_data_on_failure=False,
        )
        client = new_s3_client(local_minio.endpoint, access_key, secret_key)
        record_python_step(report, "fault-corrupt-get", lambda: fault_corrupt_get_fails(client, local_minio, state))

        stop_local_minio(report, local_minio, remove_data=False, step_name="fault-minio-stop-before-heal")
        local_minio = None

        drive_dirs = [erasure_root / f"drive-{index}" for index in range(1, args.heal_drive_count + 1)]
        heal_bucket = f"{bucket}-heal"
        local_minio = start_local_minio(
            report,
            binary,
            access_key,
            secret_key,
            args.startup_timeout,
            data_dir=erasure_root,
            drive_dirs=drive_dirs,
            step_name="fault-erasure-minio-start",
            cleanup_data_on_failure=False,
        )
        client = new_s3_client(local_minio.endpoint, access_key, secret_key)
        record_python_step(report, "fault-erasure-prepare-state", lambda: fault_create_erasure_bucket(client, heal_bucket))
        heal_state = record_python_step(
            report,
            "fault-erasure-remove-drive-put",
            lambda: fault_erasure_remove_drive_put(client, local_minio, heal_bucket, drive_dirs[0]),
        )
        record_python_step(
            report,
            "fault-erasure-readd-drive",
            lambda: fault_restore_drive_and_wait(
                local_minio,
                pathlib.Path(heal_state["drive_dir"]),
                pathlib.Path(heal_state["removed_dir"]),
            ),
        )

        mc_config_dir = report.work_dir / "mc-config"
        mc_config_dir.mkdir(parents=True, exist_ok=True)
        alias = "fault-local"
        run_command(
            report,
            "fault-mc-alias-set",
            [
                "mc",
                "--config-dir",
                str(mc_config_dir),
                "alias",
                "set",
                alias,
                local_minio.endpoint,
                "--api",
                "S3v4",
                "--path",
                "on",
            ],
            cwd=ROOT_DIR,
            timeout=60,
            input_text=f"{access_key}\n{secret_key}\n",
        )
        run_command(
            report,
            "fault-mc-admin-heal",
            [
                "mc",
                "--config-dir",
                str(mc_config_dir),
                "admin",
                "heal",
                "--json",
                "--force",
                "-r",
                f"{alias}/{heal_bucket}",
            ],
            cwd=ROOT_DIR,
            timeout=args.heal_timeout,
        )
        record_python_step(
            report,
            "fault-erasure-verify-heal",
            lambda: fault_verify_readd_heal(client, heal_state, args.heal_timeout),
        )
    except Exception as exc:
        report.add(
            "runner",
            "failed",
            0,
            error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip(),
        )
    finally:
        if local_minio is not None:
            stop_local_minio(report, local_minio, remove_data=True, step_name="fault-minio-stop-final")
        else:
            cleanup_data_dir_step(report, data_dir)
        if not args.keep_workdir and report.passed():
            shutil.rmtree(report.work_dir, ignore_errors=True)
        report.finish()
        print(f"\nReport: {report.root}")
    return 0 if report.passed() else 1


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--minio-dir",
        default=os.getenv("MINIO_DIR", str(DEFAULT_MINIO_DIR)),
        help="path to the MinIO source directory; defaults from MINIO_DIR or ../minio",
    )
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="directory for reports")
    parser.add_argument("--keep-workdir", action="store_true", help="preserve work files after successful runs")
    parser.add_argument("--build-tags", default="kqueue", help="Go build tags for building MinIO")


def add_smoke_args(parser: argparse.ArgumentParser) -> None:
    add_common_args(parser)
    default_access_key = os.getenv("MINIO_ROOT_USER", DEFAULT_ACCESS_KEY)
    default_secret_key = os.getenv("MINIO_ROOT_PASSWORD", DEFAULT_SECRET_KEY)
    parser.add_argument("--locustfile", default=str(DEFAULT_LOCUSTFILE), help="Locust file to execute")
    parser.add_argument("--access-key", default=default_access_key, help="local MinIO root access key")
    parser.add_argument("--secret-key", default=default_secret_key, help="local MinIO root secret key")
    parser.add_argument("--bucket-prefix", help="bucket prefix for validation-created buckets")
    parser.add_argument("--spawn-rate", type=float, default=1.0, help="Locust user spawn rate")
    parser.add_argument("--stop-timeout", type=int, default=30, help="Locust stop timeout in seconds")
    parser.add_argument("--startup-timeout", type=int, default=60, help="seconds to wait for local MinIO health")
    parser.add_argument(
        "--command-timeout-padding",
        type=int,
        default=300,
        help="extra seconds before the wrapper kills the Locust process",
    )
    parser.add_argument(
        "--locust-arg",
        action="append",
        default=[],
        help="extra raw argument passed to Locust; repeat for multiple args",
    )
    parser.add_argument("--duration", type=duration_seconds, default=10 * 60, help="smoke max duration, e.g. 10m")
    parser.add_argument("--users", type=int, default=1, help="Locust user count")


def add_ops_args(parser: argparse.ArgumentParser) -> None:
    add_common_args(parser)
    default_access_key = os.getenv("MINIO_ROOT_USER", DEFAULT_ACCESS_KEY)
    default_secret_key = os.getenv("MINIO_ROOT_PASSWORD", DEFAULT_SECRET_KEY)
    parser.add_argument("--access-key", default=default_access_key, help="local MinIO root access key")
    parser.add_argument("--secret-key", default=default_secret_key, help="local MinIO root secret key")
    parser.add_argument("--bucket-prefix", help="bucket prefix for validation-created buckets")
    parser.add_argument("--startup-timeout", type=int, default=60, help="seconds to wait for local MinIO health")


def add_fault_args(parser: argparse.ArgumentParser) -> None:
    add_common_args(parser)
    default_access_key = os.getenv("MINIO_ROOT_USER", DEFAULT_ACCESS_KEY)
    default_secret_key = os.getenv("MINIO_ROOT_PASSWORD", DEFAULT_SECRET_KEY)
    parser.add_argument("--access-key", default=default_access_key, help="local MinIO root access key")
    parser.add_argument("--secret-key", default=default_secret_key, help="local MinIO root secret key")
    parser.add_argument("--bucket-prefix", help="bucket prefix for validation-created buckets")
    parser.add_argument("--startup-timeout", type=int, default=60, help="seconds to wait for local MinIO health")
    parser.add_argument("--heal-drive-count", type=int, default=4, help="local erasure drive count for re-add heal validation")
    parser.add_argument("--heal-timeout", type=int, default=120, help="seconds to wait for admin heal and healed file verification")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MinIO source and local validation runner")
    sub = parser.add_subparsers(dest="command", required=True)

    source = sub.add_parser("source", help="validate source build and Go tests")
    add_common_args(source)
    source.add_argument("--packages", nargs="+", default=["./..."], help="Go packages to test")
    source.add_argument("--timeout", default="60m", help="go test timeout")
    source.add_argument("--test-tags", default="kqueue,dev", help="Go test tags")
    source.add_argument("--run", help="go test -run expression")
    source.add_argument("--short", action="store_true", help="pass -short to go test")
    source.add_argument("--race", action="store_true", help="also run go test -race")
    source.add_argument("--race-timeout", default="100m", help="go test -race timeout")
    source.add_argument("--skip-build", action="store_true", help="skip go build")
    source.add_argument("--skip-tests", action="store_true", help="skip go test")

    smoke = sub.add_parser("smoke", help="build local MinIO and run functional smoke validation")
    add_smoke_args(smoke)

    ops = sub.add_parser("ops", help="build local MinIO and run restart, persistence, ILM config, and purge validation")
    add_ops_args(ops)

    fault = sub.add_parser("fault", help="build local MinIO and run local drive fault injection validation")
    add_fault_args(fault)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "source":
            return run_source(args)
        if args.command == "smoke":
            return run_smoke(args)
        if args.command == "ops":
            return run_ops(args)
        if args.command == "fault":
            return run_fault(args)
    except StepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
