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
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    log_path = report.log_dir / f"{safe_name(name)}.log"
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write(f"$ {' '.join(cmd)}\n")
        log_file.write(f"# cwd: {cwd}\n\n")
        log_file.flush()
        print(f"[command] {name}: {' '.join(cmd)}")
        proc = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed runner tool invocations without a shell.
            cmd,
            cwd=str(cwd),
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **popen_kwargs(),
        )

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
        details={"command": cmd, "cwd": str(cwd), "exit_code": return_code},
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
            conn.request("GET", "/minio/health/live")
            response = conn.getresponse()
            response.read()
            if response.status == 200:
                return
            last_error = f"health check returned HTTP {response.status}"
        except Exception as exc:
            last_error = str(exc)
        finally:
            if conn is not None:
                conn.close()
        time.sleep(0.5)
    raise StepError(f"local MinIO did not become healthy within {timeout}s: {last_error}")


def start_local_minio(
    report: Report,
    binary: pathlib.Path,
    access_key: str,
    secret_key: str,
    startup_timeout: int,
) -> LocalMinio:
    started = time.monotonic()
    data_dir = report.work_dir / "local-minio-data"
    data_dir.mkdir(parents=True, exist_ok=False)
    api_port = find_free_port()
    console_port = find_free_port()
    while console_port == api_port:
        console_port = find_free_port()
    endpoint = f"http://127.0.0.1:{api_port}"
    log_path = report.log_dir / "local-minio.log"
    cmd = [
        str(binary),
        "server",
        str(data_dir),
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
        }
    )
    proc: subprocess.Popen[Any] | None = None
    try:
        log_file = log_path.open("w", encoding="utf-8", errors="replace")
        try:
            log_file.write(f"$ {' '.join(cmd)}\n")
            log_file.write(f"# cwd: {ROOT_DIR}\n\n")
            log_file.flush()
            print(f"[command] local-minio-start: {' '.join(cmd)}")
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
        shutil.rmtree(data_dir, ignore_errors=True)
        report.add(
            "local-minio-start",
            "failed",
            time.monotonic() - started,
            details={"endpoint": endpoint, "api_port": api_port, "console_port": console_port},
            error="".join(traceback.format_exception_only(type(exc), exc)).strip(),
            log_path=log_path,
        )
        raise

    report.add(
        "local-minio-start",
        "passed",
        time.monotonic() - started,
        details={"endpoint": endpoint, "api_port": api_port, "console_port": console_port},
        log_path=log_path,
    )
    return LocalMinio(proc, endpoint, data_dir, log_path, api_port, console_port)


def stop_local_minio(report: Report, local: LocalMinio) -> None:
    started = time.monotonic()
    error = None
    try:
        terminate_process(local.process)
    except Exception as exc:
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    shutil.rmtree(local.data_dir, ignore_errors=True)
    if local.data_dir.exists():
        error = f"failed to remove local MinIO data directory: {local.data_dir}"
    report.add(
        "local-minio-stop",
        "failed" if error else "passed",
        time.monotonic() - started,
        details={"endpoint": local.endpoint, "data_dir": str(local.data_dir)},
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MinIO source and local smoke validation runner")
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

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "source":
            return run_source(args)
        if args.command == "smoke":
            return run_smoke(args)
    except StepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
