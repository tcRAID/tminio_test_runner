#!/usr/bin/env python3
"""One-command, isolated localhost MinIO binary check for Ubuntu migrations.

Only a new child process and a newly allocated work directory are mutated.
No existing endpoint, bucket, system service or data path can be selected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import resource
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import boto3
import botocore
from botocore.config import Config
from botocore.exceptions import ClientError

MIB = 1024**2
GIB = 1024**3
SIZES = (0, 1024, MIB, 64 * MIB, GIB)
PART_BYTES = 16 * MIB
BUCKET = "migration-objects"
VERSIONED = "migration-versions"
LIFECYCLE = "migration-lifecycle"
NOT_COVERED = [
    "Existing production data or its compatibility across different MinIO releases",
    "Your systemd service, service account, reboot behavior and production configuration",
    "Production disks/mounts unless --work-dir selects the intended test filesystem",
    "Distributed pools, quorum, healing, replication, gateways and TLS/DNS configuration",
    "Age-based lifecycle expiration (this quick run checks rule configuration only)",
    "Console login, external IAM/KMS/LDAP/OIDC, application workflows and performance/soak",
    "OS rollback, backups, production deployment, monitoring and recovery procedures",
]
CHECK_NAMES = ["Binary and host", "Startup and health", "Object operations", "Overwrite and batch delete",
               "Pagination and key names", "Multipart uploads", "Access restrictions", "Presigned URLs",
               "Versioning", "Lifecycle configuration", "Restart and data persistence",
               "Shutdown and server logs", "Remove isolated test data"]


class Deadline(BaseException):
    # SDKs wrap ordinary Exceptions as transport failures. A run deadline must
    # escape that wrapping so the runner stops, rather than continuing to the
    # next case after its alarm has already fired.
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def file_hash(path):
    with open(path, "rb") as stream:
        return stream_hash(stream)[0]


def stream_hash(stream):
    digest, size = hashlib.sha256(), 0
    for chunk in iter(lambda: stream.read(MIB), b""):
        size += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), size


def atomic_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as output:
        json.dump(value, output, indent=2, sort_keys=True, ensure_ascii=True)
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)


def fixture(directory, size, seed):
    """Generate up to 1 GiB with at most a 1 MiB generation buffer."""
    stream = tempfile.TemporaryFile(dir=directory)
    block = hashlib.sha256(seed.encode()).digest() * (MIB // 32)
    digest = hashlib.sha256()
    left = size
    while left:
        data = block[:min(left, len(block))]
        stream.write(data)
        digest.update(data)
        left -= len(data)
    stream.seek(0)
    return stream, digest.hexdigest()


def dataset_keys():
    keys = ([f"flat/{n:04d}" for n in range(1000)] +
            [f"nested/{n // 100:02d}/{n:04d}" for n in range(1000)] +
            [f"unicode/空 白-{n:03d}-é-☃" for n in range(100)] +
            ["special/a+b %?#&=<>", "special/quotes'\"", "special/trailing/",
             "special/line\nbreak", "special/emoji-😀"])
    check(len(keys) == len(set(keys)) == 2105, "invalid listing dataset")
    return keys


def elf_info(path):
    """Read architecture and interpreter without executing ldd on the supplied file."""
    with open(path, "rb") as stream:
        header = stream.read(64)
        check(len(header) == 64 and header[:4] == b"\x7fELF", "binary is not a Linux ELF executable")
        check(header[4] == 2 and header[5] in (1, 2), "expected a 64-bit ELF binary")
        endian = "<" if header[5] == 1 else ">"
        machine = struct.unpack_from(endian + "H", header, 18)[0]
        offset = struct.unpack_from(endian + "Q", header, 32)[0]
        entry_size, count = struct.unpack_from(endian + "HH", header, 54)
        check(entry_size >= 56 and count < 10000, "invalid ELF program headers")
        interpreter = None
        for n in range(count):
            stream.seek(offset + n * entry_size)
            entry = stream.read(56)
            check(len(entry) == 56, "truncated ELF program header")
            if struct.unpack_from(endian + "I", entry)[0] == 3:  # PT_INTERP
                start = struct.unpack_from(endian + "Q", entry, 8)[0]
                length = struct.unpack_from(endian + "Q", entry, 32)[0]
                check(length <= 4096, "invalid ELF interpreter")
                stream.seek(start)
                interpreter = stream.read(length).rstrip(b"\0").decode()
        return {"machine": machine, "architecture": {62: "x86_64", 183: "aarch64"}.get(machine, str(machine)),
                "interpreter": interpreter, "linkage": "dynamic" if interpreter else "no dynamic interpreter"}


def server_environment(access, secret):
    # Drop inherited MINIO_*, CI, proxy, AWS and external integration settings.
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ"}}
    env.update(MINIO_ROOT_USER=access, MINIO_ROOT_PASSWORD=secret, MINIO_BROWSER="off", MINIO_UPDATE="off")
    return env


def client_config(*, read_timeout=60):
    return Config(signature_version="s3v4", connect_timeout=5, read_timeout=read_timeout,
                  retries={"mode": "standard", "total_max_attempts": 1},
                  proxies={}, s3={"addressing_style": "path"},
                  request_checksum_calculation="when_required", response_checksum_validation="when_required")


class Suite:
    bucket, versioned_bucket, lifecycle_bucket = BUCKET, VERSIONED, LIFECYCLE
    check_names = CHECK_NAMES
    region = "us-east-1"
    verify_tls = True
    session_token = None

    def __init__(self, binary, parent, timeout=1800):
        self.binary = Path(binary).resolve(strict=True)
        check(self.binary.is_file() and os.access(self.binary, os.X_OK), "binary must be an executable file")
        self.init_work(parent, timeout)

    def init_work(self, parent, timeout):
        """Allocate reports and local payload space shared by the two runners."""
        parent = Path(parent).resolve(strict=True)
        self.directory = Path(tempfile.mkdtemp(prefix="minio-check-" + time.strftime("%Y%m%d-%H%M%S-") , dir=parent))
        self.owner_token = secrets.token_hex(16)
        (self.directory / ".owned-by-check").write_text(self.owner_token)
        self.data = self.directory / "data"
        self.data.mkdir()
        self.payloads = self.directory / "payloads"
        self.payloads.mkdir()
        for name in ("config", "certs"):
            (self.directory / name).mkdir()
        self.access, self.secret = "check" + secrets.token_hex(10), secrets.token_hex(24)
        self.secrets = [self.access, self.secret]
        self.process = None
        self.log_stream = None
        self.endpoint = None
        self.clients = []
        self.client = None
        self.expected = {}
        self.deleted = set()
        self.version_history = None
        self.timeout = timeout
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.report = {"schema": 1, "mode": "binary", "production_release_qualified": False,
            "started": utcnow(), "binary": str(self.binary),
            "platform": platform.platform(), "os_release": self.os_release(),
            "python": platform.python_version(), "boto3": boto3.__version__, "botocore": botocore.__version__,
            "object_sizes": list(SIZES), "multipart_bytes": GIB, "multipart_part_bytes": PART_BYTES,
            "listing_keys": 2105, "overwrite_cycles": 100, "results": [], "not_covered": NOT_COVERED,
            "scope": "One isolated localhost instance with fresh test data; runtime migration check only.",
            "work_directory": str(self.directory), "timeout_seconds": timeout}
        self.case = "initialization"
        self.total_requests = 0
        self.save()

    @staticmethod
    def os_release():
        path = Path("/etc/os-release")
        return path.read_text() if path.exists() else "unavailable"

    def redact(self, text):
        text = str(text)
        for value in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(value, "[REDACTED]")
        return re.sub(r'(https?://[^\s?"<>]+)\?[^\s"<>]+', r"\1?[REDACTED]", text)

    def safe(self, value):
        if isinstance(value, dict):
            return {k: self.safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.safe(v) for v in value]
        return self.redact(value) if isinstance(value, str) else value

    def event(self, kind, **fields):
        with (self.directory / "requests.jsonl").open("a") as output:
            output.write(json.dumps(self.safe({"time": utcnow(), "check": self.case, "kind": kind, **fields}), default=str) + "\n")

    def save(self):
        atomic_json(self.directory / "results.json", self.safe(self.report))
        atomic_json(self.directory / "objects.json", {"objects": list(self.expected.values()), "deleted": sorted(self.deleted),
                                                     "version_history": self.version_history})

    def make_client(self, credentials=None, service="s3"):
        credentials = credentials or {"AccessKeyId": self.access, "SecretAccessKey": self.secret,
                                      "SessionToken": self.session_token}
        client = boto3.session.Session().client(service, endpoint_url=self.endpoint, region_name=self.region, verify=self.verify_tls,
            aws_access_key_id=credentials["AccessKeyId"], aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials.get("SessionToken"),
            config=client_config(read_timeout=getattr(self, "read_timeout", 60)))
        self.clients.append(client)
        return client

    def call(self, method, *, client=None, retry_expected_status=None, **kwargs):
        if getattr(self, "http503_retry_limit", 0):
            from .request_retry import call_with_503_retries
            return call_with_503_retries(self, method, kwargs,
                                        lambda: self.call_once(method, client=client, **kwargs),
                                        expected_status=retry_expected_status)
        return self.call_once(method, client=client, **kwargs)

    def call_once(self, method, *, client=None, **kwargs):
        if method in {"put_object", "copy_object", "delete_object", "delete_objects", "create_multipart_upload",
                      "upload_part", "complete_multipart_upload", "abort_multipart_upload"}:
            # Durable intent before sending the request. Never records request bodies/secrets.
            self.event("mutation", operation=method, bucket=kwargs.get("Bucket"), key=kwargs.get("Key"),
                       version_id=kwargs.get("VersionId"), upload_id=kwargs.get("UploadId"),
                       objects=kwargs.get("Delete", {}).get("Objects"))
        try:
            result = getattr(client or self.client, method)(**kwargs)
        except ClientError as error:
            self.record_response(method, error.response, kwargs)
            raise
        self.record_response(method, result, kwargs)
        return result

    def record_response(self, operation, response, args):
        m = response.get("ResponseMetadata", {})
        self.total_requests += 1
        self.event("response", operation=operation, bucket=args.get("Bucket"), key=args.get("Key"),
                   status=m.get("HTTPStatusCode"), request_id=m.get("RequestId"),
                   retries=m.get("RetryAttempts"), error_code=response.get("Error", {}).get("Code"),
                   version_id=response.get("VersionId"), upload_id=response.get("UploadId"))
        check(m.get("RetryAttempts", 0) == 0, f"{operation}: SDK retry would hide initial failure")

    def missing(self, bucket, key, version=None, client=None):
        args = {"Bucket": bucket, "Key": key}
        if version is not None:
            args["VersionId"] = version
        self.error("get_object", {"NoSuchKey", "NoSuchVersion"}, status=404, client=client, **args)

    def error(self, method, codes, *, status=None, client=None, **args):
        try:
            result = self.call(method, client=client, retry_expected_status=status, **args)
        except ClientError as error:
            actual = error.response["Error"]["Code"]
            check(actual in codes, f"{method}: expected {sorted(codes)}, got {actual}")
            if status is not None:
                check(error.response["ResponseMetadata"]["HTTPStatusCode"] == status, f"{method}: wrong error HTTP status")
        else:
            if "Body" in result:
                result["Body"].close()
            raise AssertionError(f"{method}: expected rejection {sorted(codes)}")

    def put(self, bucket, key, size, seed=None, *, client=None):
        stream, digest = fixture(self.payloads, size, seed or key)
        record = {"bucket": bucket, "key": key, "size": size, "sha256": digest,
                  "metadata": {"sha256": digest, "test": "ubuntu26-release"}, "content_type": "application/octet-stream"}
        try:
            response = self.call("put_object", client=client, Bucket=bucket, Key=key, Body=stream, ContentLength=size,
                                 Metadata=record["metadata"], ContentType=record["content_type"])
        finally:
            stream.close()
        record["version"] = response.get("VersionId")
        self.remember(record)
        return record

    def remember(self, record):
        self.event("object_expected", **record)
        self.expected[(record["bucket"], record["key"], record.get("version"))] = record
        self.deleted.discard((record["bucket"], record["key"]))
        return record

    def verify(self, record, client=None):
        args = {"Bucket": record["bucket"], "Key": record["key"]}
        if record.get("version"):
            args["VersionId"] = record["version"]
        result = self.call("get_object", client=client, **args)
        try:
            digest, size = stream_hash(result["Body"])
        finally:
            result["Body"].close()
        check((digest, size) == (record["sha256"], record["size"]), f"GET checksum/size mismatch: {record['key']}")
        for response in (result, self.call("head_object", client=client, **args)):
            check(response["ContentLength"] == record["size"], f"size mismatch: {record['key']}")
            check(response.get("Metadata", {}) == record["metadata"], f"metadata mismatch: {record['key']}")
            check(response.get("ContentType") == record["content_type"], f"content type mismatch: {record['key']}")
            if record.get("version"):
                # MinIO omits x-amz-version-id for its null slot (api-headers.go).
                # The request still explicitly selects VersionId=null; UUID
                # versions must always return their exact ID.
                actual_version = response.get("VersionId")
                if record["version"] == "null" and actual_version is None:
                    actual_version = "null"
                check(actual_version == record["version"], "wrong version returned")

    def delete(self, record):
        args = {"Bucket": record["bucket"], "Key": record["key"]}
        if record.get("version"):
            args["VersionId"] = record["version"]
        self.call("delete_object", **args)
        self.missing(record["bucket"], record["key"], record.get("version"))
        self.expected.pop((record["bucket"], record["key"], record.get("version")), None)
        if not record.get("version"):
            self.deleted.add((record["bucket"], record["key"]))

    def pages(self, method, **args):
        seen = set()
        for _ in range(10000):
            page = self.call(method, **args)
            yield page
            if not page.get("IsTruncated"):
                return
            if method == "list_objects_v2":
                token = page.get("NextContinuationToken")
                check(bool(token) and token not in seen, "missing/repeated continuation token")
                args["ContinuationToken"] = token
            elif method == "list_object_versions":
                token = page.get("NextKeyMarker"), page.get("NextVersionIdMarker")
                check(token[0] is not None and token not in seen, "missing/repeated version marker")
                args["KeyMarker"] = token[0]
                if token[1] is not None:
                    args["VersionIdMarker"] = token[1]
                else:
                    args.pop("VersionIdMarker", None)
            else:
                raise ValueError("unsupported pagination method")
            seen.add(token)
        raise AssertionError("pagination did not terminate")

    def http_request(self, url, data=None, method=None):
        request = urllib.request.Request(url, data=data, method=method)
        try:
            response = self.http.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            self.event("http", path=urllib.parse.urlsplit(url).path, status=response.code,
                       request_id=response.headers.get("x-amz-request-id"))
            return response.code, body

    def health(self):
        for path in ("live", "ready", "cluster", "cluster/read"):
            status, _ = self.http_request(self.endpoint + "/minio/health/" + path)
            check(status == 200, f"health/{path}: HTTP {status}")
        check(self.process is not None and self.process.poll() is None, "owned MinIO process exited")

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{port}"
        self.report["endpoint"] = self.endpoint
        command = [str(self.binary), "--config-dir", str(self.directory / "config"),
                   "--certs-dir", str(self.directory / "certs"), "server", "--address", f"127.0.0.1:{port}", str(self.data)]
        self.log_stream = (self.directory / "server.private.log").open("ab", buffering=0)
        self.process = subprocess.Popen(command, env=server_environment(self.access, self.secret),
            cwd=self.directory, stdin=subprocess.DEVNULL, stdout=self.log_stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.event("process_start", pid=self.process.pid, endpoint=self.endpoint, data=str(self.data))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            check(self.process.poll() is None, "MinIO exited during startup; see server.log")
            try:
                self.health()
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError, AssertionError):
                pass  # Startup readiness only; S3 consistency assertions never retry.
            time.sleep(0.25)
        else:
            raise AssertionError("MinIO did not become ready in 90 seconds")
        self.client = self.make_client()
        # Authentication binds this probe to OUR random credentials; a port race cannot
        # redirect mutations to an unrelated service that happened to bind the port.
        self.call("list_buckets")
        self.health()

    def stop(self, *, require_clean=False):
        process = self.process
        if process is None:
            return
        forced = False
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    forced = True
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
            code = process.returncode
            self.event("process_stop", pid=process.pid, returncode=code, forced=forced)
            if require_clean:
                check(not forced and code == 0, f"MinIO did not stop cleanly: exit {code}, forced={forced}")
        finally:
            if process.poll() is not None:
                self.process = None
            if self.log_stream:
                self.log_stream.close()
                self.log_stream = None

    def run_case(self, title, function):
        self.case = title
        start, stamp = time.monotonic(), utcnow()
        print(f"  RUN  {title}", flush=True)
        result = {"name": title, "started": stamp, "expected": function.__doc__ or title,
                  "status": "FAIL", "actual": "Check did not complete"}
        try:
            function()
            result.update(status="PASS", actual="All assertions passed")
        except (KeyboardInterrupt, Deadline) as error:
            result.update(status="FAIL", actual="Interrupted" if isinstance(error, KeyboardInterrupt) else str(error))
            raise
        except Exception as error:
            result.update(status="FAIL", actual=self.redact(f"{type(error).__name__}: {error}"))
        finally:
            result.update(finished=utcnow(), seconds=round(time.monotonic() - start, 3))
            self.report["results"].append(result)
            self.save()
            print(f"  {result['status']:4} {title} ({result['seconds']}s)" +
                  (" — " + result["actual"] if result["status"] == "FAIL" else ""), flush=True)
        return result["status"] == "PASS"

    def artifact(self):
        """Binary runs on this host; record its hash, ELF architecture, linkage and build identity."""
        info = elf_info(self.binary)
        check(info["architecture"] == platform.machine(), f"binary architecture {info['architecture']} != host {platform.machine()}")
        self.report["artifact_sha256"] = file_hash(self.binary)
        self.report["elf"] = info
        result = subprocess.run([str(self.binary), "--version"], capture_output=True, text=True, timeout=30,
                                env=server_environment(self.access, self.secret))
        check(result.returncode == 0 and "minio" in result.stdout.lower(), "minio --version failed")
        self.report["minio_version"] = self.redact(result.stdout.strip())
        go = shutil.which("go")
        if go:
            result = subprocess.run([go, "version", "-m", str(self.binary)], capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "GOTOOLCHAIN": "local"})
            self.report["go_build_info"] = result.stdout.strip() if result.returncode == 0 else "unavailable: go version -m failed"
        else:
            self.report["go_build_info"] = "unavailable: Go is not installed (not needed to run MinIO)"
        free = shutil.disk_usage(self.directory).free
        self.report["disk_free_bytes_before"] = free
        self.report["runner_limits"] = {"open_files": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
                                         "processes": list(resource.getrlimit(resource.RLIMIT_NPROC))}
        self.report["runner_identity"] = {"uid": os.getuid(), "gid": os.getgid()}
        check(free >= 4 * GIB, "need at least 4 GiB free on work filesystem")
        check(resource.getrlimit(resource.RLIMIT_NOFILE)[0] >= 1024, "open-file limit is below 1024")

    def startup(self):
        """Owned localhost server starts, all four health probes and authenticated requests succeed."""
        self.start()
        for bucket in (self.bucket, self.versioned_bucket, self.lifecycle_bucket):
            self.call("create_bucket", Bucket=bucket)
        self.call("put_bucket_versioning", Bucket=self.versioned_bucket, VersioningConfiguration={"Status": "Enabled"})
        check(self.call("get_bucket_versioning", Bucket=self.versioned_bucket).get("Status") == "Enabled", "versioning did not enable")
        self.verify(self.put(self.bucket, "startup/persist", 1024))

    def objects(self):
        """0 B–1 GiB PUT/GET/HEAD/COPY/Range/tags preserve bytes and metadata; deletion is immediately visible."""
        for size in SIZES:
            record = self.put(self.bucket, f"sizes/{size}", size)
            self.verify(record)
            self.call("put_object_tagging", Bucket=self.bucket, Key=record["key"], Tagging={"TagSet": [{"Key": "purpose", "Value": "migration"}]})
            check(self.call("get_object_tagging", Bucket=self.bucket, Key=record["key"])["TagSet"] == [{"Key": "purpose", "Value": "migration"}], "tag mismatch")
            copy_key = f"copies/{size}"
            self.call("copy_object", Bucket=self.bucket, Key=copy_key, CopySource={"Bucket": self.bucket, "Key": record["key"]}, MetadataDirective="COPY")
            copy = self.remember({**record, "key": copy_key})
            self.verify(copy)
            check(self.call("get_object_tagging", Bucket=self.bucket, Key=copy_key)["TagSet"] == [{"Key": "purpose", "Value": "migration"}], "copy lost tags")
            if size:
                start, end = (min(13, size - 1), min(999, size - 1))
                response = self.call("get_object", Bucket=self.bucket, Key=record["key"], Range=f"bytes={start}-{end}")
                with response["Body"] as body:
                    actual = body.read()
                original, _ = fixture(self.payloads, end + 1, record["key"])
                with original:
                    original.seek(start)
                    expected = original.read(end - start + 1)
                check(response["ResponseMetadata"]["HTTPStatusCode"] == 206, "Range GET must return 206")
                check(response["ContentRange"] == f"bytes {start}-{end}/{size}" and actual == expected, "range mismatch")
                self.error("get_object", {"InvalidRange"}, status=416, Bucket=self.bucket, Key=record["key"], Range=f"bytes={size}-")
            self.delete(copy)
            self.delete(record)

    def cycles(self):
        """100 PUT A/overwrite B/DELETE/recreate C cycles have no stale reads; batch delete checks every entry."""
        for n in range(100):
            a = self.put(self.bucket, "cycles/key", 1024, f"A-{n}")
            self.verify(a)
            b = self.put(self.bucket, "cycles/key", 2048, f"B-{n}")
            self.verify(b)
            self.delete(b)
            c = self.put(self.bucket, "cycles/key", 512, f"C-{n}")
            self.verify(c)
        present = self.put(self.bucket, "batch/present", 1024)
        keys = [present["key"], "batch/missing"]
        self.batch_delete(self.bucket, keys)
        self.expected.pop((self.bucket, present["key"], None))

    def batch_delete(self, bucket, keys):
        response = self.call("delete_objects", Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys], "Quiet": False})
        check(not response.get("Errors"), "batch delete returned per-object Errors")
        deleted = [v["Key"] for v in response.get("Deleted", [])]
        check(len(deleted) == len(keys) and set(deleted) == set(keys), "batch delete response omitted/duplicated keys")
        for key in keys:
            self.missing(bucket, key)
            self.deleted.add((bucket, key))

    def listing(self):
        """Exactly 2,105 keys round-trip; all pages, prefixes and delimiters agree with the key manifest."""
        records = [self.put(self.bucket, "listing/" + key, 128) for key in dataset_keys()]
        wanted = {r["key"] for r in records}
        pages = list(self.pages("list_objects_v2", Bucket=self.bucket, Prefix="listing/", MaxKeys=1000))
        got = [entry["Key"] for page in pages for entry in page.get("Contents", [])]
        check(len(pages) == 3 and len(got) == 2105 and set(got) == wanted, "listing pagination differs from manifest")
        sizes = {e["Key"]: e["Size"] for page in pages for e in page.get("Contents", [])}
        check(all(sizes[r["key"]] == r["size"] for r in records), "listing sizes differ")
        grouped = list(self.pages("list_objects_v2", Bucket=self.bucket, Prefix="listing/", Delimiter="/", MaxKeys=2))
        groups = [p["Prefix"] for page in grouped for p in page.get("CommonPrefixes", [])]
        check(len(groups) == 4 and set(groups) == {"listing/flat/", "listing/nested/", "listing/unicode/", "listing/special/"}, "delimiter grouping mismatch")
        check(not any(page.get("Contents") for page in grouped), "delimiter returned unexpected keys")
        prefix = "listing/nested/03/"
        prefixed = [e["Key"] for page in self.pages("list_objects_v2", Bucket=self.bucket, Prefix=prefix, MaxKeys=37) for e in page.get("Contents", [])]
        check(len(prefixed) == 100 and set(prefixed) == {k for k in wanted if k.startswith(prefix)}, "prefix listing mismatch")
        for record in records:
            self.verify(record)
        # Delete the special-character objects individually to exercise URL/XML paths.
        for record in records[-105:]:
            self.delete(record)

    def multipart_one(self, name, cross_client):
        key = "multipart/" + name
        stream, digest = fixture(self.payloads, GIB, key)
        record = {"bucket": self.bucket, "key": key, "size": GIB, "sha256": digest, "version": None,
                  "metadata": {"sha256": digest, "test": "multipart"}, "content_type": "application/octet-stream"}
        upload = self.call("create_multipart_upload", Bucket=self.bucket, Key=key, Metadata=record["metadata"], ContentType=record["content_type"])["UploadId"]
        other = self.assume_client({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": f"arn:aws:s3:::{self.bucket}/{key}"}]}) if cross_client else self.client
        parts = []
        try:
            number = 0
            for body in iter(lambda: stream.read(PART_BYTES), b""):
                number += 1
                if number == 2:
                    # A failed/replaced part must not leak old bytes into the completed object.
                    self.call("upload_part", client=other, Bucket=self.bucket, Key=key, UploadId=upload, PartNumber=number, Body=b"x" * len(body))
                response = self.call("upload_part", client=other, Bucket=self.bucket, Key=key, UploadId=upload, PartNumber=number, Body=body)
                parts.append({"PartNumber": number, "ETag": response["ETag"]})
            listed = self.call("list_parts", Bucket=self.bucket, Key=key, UploadId=upload)["Parts"]
            check([(p["PartNumber"], p["ETag"]) for p in listed] == [(p["PartNumber"], p["ETag"]) for p in parts], "multipart part list mismatch")
            self.call("complete_multipart_upload", Bucket=self.bucket, Key=key, UploadId=upload, MultipartUpload={"Parts": parts})
        finally:
            stream.close()
        self.remember(record)
        self.verify(record)

    def multipart(self):
        """Two 1 GiB/16 MiB-part uploads, replacement part and separate STS client match SHA-256; abort is final."""
        self.multipart_one("single-client", False)
        self.multipart_one("cross-client", True)
        key = "multipart/aborted"
        upload = self.call("create_multipart_upload", Bucket=self.bucket, Key=key)["UploadId"]
        part = self.call("upload_part", Bucket=self.bucket, Key=key, UploadId=upload, PartNumber=1, Body=b"unfinished")
        self.call("abort_multipart_upload", Bucket=self.bucket, Key=key, UploadId=upload)
        self.error("complete_multipart_upload", {"NoSuchUpload"}, status=404, Bucket=self.bucket, Key=key, UploadId=upload,
                   MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"]}]})
        active = self.call("list_multipart_uploads", Bucket=self.bucket)
        check(not active.get("IsTruncated") and not active.get("Uploads"), "incomplete multipart uploads remain")
        self.missing(self.bucket, key)
        self.deleted.add((self.bucket, key))

    def assume_client(self, policy):
        sts = self.make_client(service="sts")
        credentials = self.call("assume_role", client=sts, RoleArn="arn:aws:iam::123456789012:role/release-check",
                                RoleSessionName="release-check", DurationSeconds=900, Policy=json.dumps(policy))["Credentials"]
        self.secrets.extend(credentials[k] for k in ("AccessKeyId", "SecretAccessKey", "SessionToken"))
        return self.make_client(credentials)

    def access_control(self):
        """Read-only/prefix STS sessions allow correct PUT/GET/DELETE/LIST and reject forbidden or invalid credentials."""
        record = self.put(self.bucket, "access/allowed/read", 128)
        forbidden = self.put(self.bucket, "access/other/read", 128)
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{self.bucket}/access/allowed/*"},
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{self.bucket}",
             "Condition": {"StringLike": {"s3:prefix": ["access/allowed/*"]}}}]}
        readonly = self.assume_client(policy)
        self.verify(record, readonly)
        check(self.call("list_objects_v2", client=readonly, Bucket=self.bucket, Prefix="access/allowed/")["KeyCount"] == 1, "allowed listing failed")
        for method, args in [("put_object", {"Key": record["key"], "Body": b"forbidden"}),
                             ("delete_object", {"Key": record["key"]}),
                             ("get_object", {"Key": forbidden["key"]}),
                             ("list_objects_v2", {"Prefix": "access/other/"})]:
            self.error(method, {"AccessDenied"}, status=403, client=readonly, Bucket=self.bucket, **args)
        policy["Statement"][0]["Action"] = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        restricted = self.assume_client(policy)
        allowed = self.put(self.bucket, "access/allowed/write", 128, client=restricted)
        self.verify(allowed, restricted)
        self.call("delete_object", client=restricted, Bucket=self.bucket, Key=allowed["key"])
        self.expected.pop((self.bucket, allowed["key"], None))
        self.missing(self.bucket, allowed["key"])
        self.deleted.add((self.bucket, allowed["key"]))
        for method, args in [("put_object", {"Key": forbidden["key"], "Body": b"forbidden"}),
                             ("get_object", {"Key": forbidden["key"]}), ("delete_object", {"Key": forbidden["key"]}),
                             ("list_objects_v2", {"Prefix": "access/other/"})]:
            self.error(method, {"AccessDenied"}, status=403, client=restricted, Bucket=self.bucket, **args)
        self.error("list_objects_v2", {"AccessDenied"}, status=403, client=restricted, Bucket=self.versioned_bucket)
        bad = self.make_client({"AccessKeyId": "invalid-migration-key", "SecretAccessKey": "invalid-migration-secret"})
        self.error("get_object", {"InvalidAccessKeyId"}, status=403, client=bad, Bucket=self.bucket, Key=record["key"])
        self.verify(record)
        self.verify(forbidden)
        status, body = self.http_request(self.endpoint + f"/{self.bucket}/{record['key']}")
        check(status == 403 and ET.fromstring(body).findtext("Code") == "AccessDenied", "unsigned private object read was not denied")

    def presigned(self):
        """Presigned GET/PUT preserve content; altered signatures and expired URLs are rejected."""
        record = self.put(self.bucket, "presigned/get", 1024)
        url = self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": record["key"]}, ExpiresIn=60)
        status, body = self.http_request(url)
        check(status == 200 and hashlib.sha256(body).hexdigest() == record["sha256"], "presigned GET content mismatch")
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        query["X-Amz-Signature"] = ["0" * 64]
        bad = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))
        status, body = self.http_request(bad)
        check(status == 403 and ET.fromstring(body).findtext("Code") == "SignatureDoesNotMatch", "modified signature accepted")
        key, payload = "presigned/put", b"presigned-upload"
        url = self.client.generate_presigned_url("put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": "application/octet-stream"}, ExpiresIn=60)
        request = urllib.request.Request(url, data=payload, method="PUT", headers={"Content-Type": "application/octet-stream"})
        self.event("mutation", operation="presigned_put", bucket=self.bucket, key=key)
        with self.http.open(request, timeout=15) as response:
            check(response.code == 200, "presigned PUT failed")
        put = self.remember({"bucket": self.bucket, "key": key, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                             "version": None, "metadata": {}, "content_type": "application/octet-stream"})
        self.verify(put)
        expired = self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": record["key"]}, ExpiresIn=1)
        time.sleep(3)
        status, body = self.http_request(expired)
        check(status == 403 and ET.fromstring(body).findtext("Code") == "AccessDenied", "expired signature accepted")

    def versions(self):
        """Versions retain original bytes; a delete marker hides latest, and deleting v1 preserves v2 and marker."""
        key = "history/key"
        a = self.put(self.versioned_bucket, key, 1024, "version-1")
        b = self.put(self.versioned_bucket, key, 2048, "version-2")
        check(a["version"] and b["version"] and a["version"] != b["version"], "missing/duplicate version IDs")
        self.verify(a)
        self.verify(b)
        response = self.call("delete_object", Bucket=self.versioned_bucket, Key=key)
        marker = response.get("VersionId")
        check(response.get("DeleteMarker") and marker, "DELETE did not create a marker")
        self.missing(self.versioned_bucket, key)
        self.verify(a)
        self.verify(b)
        self.delete(a)
        self.version_history = {"key": key, "version": b["version"], "marker": marker}
        self.check_history()

    def check_history(self):
        check(self.version_history is not None, "version history fixture is missing")
        h = self.version_history
        pages = list(self.pages("list_object_versions", Bucket=self.versioned_bucket, Prefix=h["key"], MaxKeys=1))
        versions = [(v["Key"], v["VersionId"], v["IsLatest"]) for page in pages for v in page.get("Versions", [])]
        markers = [(v["Key"], v["VersionId"], v["IsLatest"]) for page in pages for v in page.get("DeleteMarkers", [])]
        check(versions == [(h["key"], h["version"], False)] and markers == [(h["key"], h["marker"], True)], "version/delete marker history mismatch")
        self.missing(self.versioned_bucket, h["key"])

    def lifecycle(self):
        """Versioned lifecycle rules round-trip; long-term expiration is explicitly outside this short test."""
        self.call("put_bucket_versioning", Bucket=self.lifecycle_bucket, VersioningConfiguration={"Status": "Enabled"})
        rules = [
            {"ID": "current", "Status": "Enabled", "Filter": {"Prefix": "expire/"}, "Expiration": {"Days": 1}},
            {"ID": "noncurrent", "Status": "Enabled", "Filter": {"Prefix": "versions/"},
             "NoncurrentVersionExpiration": {"NoncurrentDays": 1, "NewerNoncurrentVersions": 2}},
            {"ID": "markers", "Status": "Enabled", "Filter": {"Prefix": "markers/"},
             "Expiration": {"ExpiredObjectDeleteMarker": True}},
        ]
        self.call("put_bucket_lifecycle_configuration", Bucket=self.lifecycle_bucket, LifecycleConfiguration={"Rules": rules})
        returned = self.call("get_bucket_lifecycle_configuration", Bucket=self.lifecycle_bucket)["Rules"]
        check(sorted(returned, key=lambda r: r["ID"]) == sorted(rules, key=lambda r: r["ID"]), "lifecycle configuration mismatch")
        self.verify(self.put(self.lifecycle_bucket, "ineligible/retained", 1024))
        self.report["lifecycle_rules"] = rules

    def restart(self):
        """Clean shutdown and restart preserve every retained checksum, metadata, version ID, deletion and lifecycle rule."""
        # Verify before stopping, so restart cannot take credit for already-wrong state.
        self.check_history()
        self.health()
        self.save()
        self.stop(require_clean=True)
        self.start()
        for record in self.expected.values():
            self.verify(record)
        for bucket, key in self.deleted:
            self.missing(bucket, key)
        self.check_history()
        rules = self.call("get_bucket_lifecycle_configuration", Bucket=self.lifecycle_bucket)["Rules"]
        check(sorted(rules, key=lambda r: r["ID"]) == sorted(self.report["lifecycle_rules"], key=lambda r: r["ID"]), "restart lost lifecycle config")
        self.verify(self.put(self.bucket, "restart/new-write", 4096))
        self.health()

    def log_check(self):
        """The owned process stops cleanly and its logs contain no panic, fatal, I/O or permission error signatures."""
        self.stop(require_clean=True)
        raw = self.directory / "server.private.log"
        suspicious = []
        if raw.exists():
            with raw.open(errors="replace") as source, (self.directory / "server.log").open("w") as output:
                for line in source:
                    safe = self.redact(line)
                    output.write(safe)
                    if re.search(r"(?i)panic:|fatal error:|segmentation fault|permission denied|read-only file system|input/output error|corrupted|drive.*offline", line):
                        suspicious.append(safe.strip())
            raw.unlink()
        self.report["suspicious_log_lines"] = suspicious[:30]
        check(not suspicious, "runtime/storage error signatures found; see server.log")

    def cleanup_data(self):
        # Never take a user-supplied data path. Reject symlink replacement and foreign
        # directories even inside our work directory before any recursive removal.
        check(self.process is None, "cannot remove data while server is running")
        check(not self.directory.is_symlink() and (self.directory / ".owned-by-check").read_text() == self.owner_token,
              "work directory ownership check failed")
        for name in ("data", "payloads", "config", "certs"):
            path = self.directory / name
            check(not path.is_symlink() and path.resolve().parent == self.directory, "cleanup path escaped work directory")
        for name in ("data", "payloads", "config", "certs"):
            shutil.rmtree(self.directory / name)

    def data_retained(self):
        return (self.directory / "data").exists()

    def finish(self):
        retry = self.report.get("http_503_retry", {})
        if (retry.get("exhausted_requests", 0) or retry.get("unreplayable_requests", 0)) and all(
                result["status"] == "PASS" for result in self.report["results"]):
            self.report["results"].append({"name": "HTTP 503 retry policy", "status": "FAIL", "seconds": 0,
                "actual": "A request exhausted or could not use its HTTP 503 retries; later cleanup recovery does not erase this failure."})
        completed = {r["name"] for r in self.report["results"]}
        for name in self.check_names:
            if name not in completed:
                self.report["results"].append({"name": name, "status": "NOT RUN", "seconds": 0,
                    "actual": "Failed-run evidence preserved" if name == "Remove isolated test data" else
                              "Prerequisite failed or execution stopped"})
        self.report["finished"] = utcnow()
        self.report["requests"] = self.total_requests
        success = bool(self.report["results"]) and all(r["status"] == "PASS" for r in self.report["results"])
        self.report["status"] = "PASS" if success else "FAIL"
        self.report["selected_tests_passed"] = success
        self.report["full_suite_completed"] = False  # This is not the configured cluster suite.
        self.report["test_data_retained"] = self.data_retained()
        self.save()
        lines = [f"MinIO {self.report['mode']} check: {self.report['status']}",
                 f"Runner host: {self.report['platform']}", f"Endpoint: {self.endpoint}", ""]
        if self.binary:
            lines[1:1] = [f"Binary: {self.binary}", f"SHA-256: {self.report.get('artifact_sha256', 'not available')}"]
        if retry:
            lines += [f"HTTP 503 responses: {retry['responses']}; retries: {retry['retries']}; "
                      f"recovered requests: {retry['recovered_requests']}; exhausted requests: {retry['exhausted_requests']}; "
                      f"failed requests: {retry['failed_requests']}",
                      "Full retry history, when present: retry-events.jsonl", ""]
        suite = ET.Element("testsuite", name="minio-ubuntu-runtime", tests=str(len(self.report["results"])),
                           failures=str(sum(r["status"] != "PASS" for r in self.report["results"])))
        for result in self.report["results"]:
            lines.append(f"{result['status']:4} {result['name']}: {result['actual']}")
            case = ET.SubElement(suite, "testcase", name=result["name"], time=str(result["seconds"]))
            if result["status"] != "PASS":
                ET.SubElement(case, "failure", message=result["actual"])
        lines += ["", "Not covered by this test:", *["- " + item for item in self.report["not_covered"]], "",
                  "Test data retained: " + str(self.report["test_data_retained"]), f"Reports: {self.directory}"]
        (self.directory / "summary.txt").write_text(self.redact("\n".join(lines) + "\n"))
        ET.ElementTree(suite).write(self.directory / "junit.xml", encoding="utf-8", xml_declaration=True)
        print(f"\n{self.report['status']}: {self.report['mode']} checks. Reports: {self.directory}", flush=True)
        print("Scope: " + self.report["scope"], flush=True)
        return 0 if success else 1


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", help="path to the MinIO binary to test")
    parser.add_argument("--work-dir", default=".", help="existing parent directory on the test filesystem (default: current directory)")
    parser.add_argument("--timeout", type=int, default=1800, help="overall test deadline in seconds (default: 1800)")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if sys.platform != "linux":
        parser.error("run this check on Linux, on the host you want to validate")
    os.umask(0o077)
    suite = None
    previous = {}
    def deadline(*_):
        raise Deadline("overall test deadline exceeded")
    def interrupted(*_):
        raise KeyboardInterrupt
    try:
        suite = Suite(args.binary, args.work_dir, args.timeout)
        print(f"Reports: {suite.directory}\nTesting only a new localhost instance; approximately 4 GiB of free disk required.", flush=True)
        for signum, handler in ((signal.SIGALRM, deadline), (signal.SIGTERM, interrupted), (signal.SIGINT, interrupted)):
            previous[signum] = signal.signal(signum, handler)
        signal.alarm(args.timeout)
        ready = suite.run_case("Binary and host", suite.artifact) and suite.run_case("Startup and health", suite.startup)
        if ready:
            for title, function in [("Object operations", suite.objects), ("Overwrite and batch delete", suite.cycles),
                                    ("Pagination and key names", suite.listing), ("Multipart uploads", suite.multipart),
                                    ("Access restrictions", suite.access_control), ("Presigned URLs", suite.presigned),
                                    ("Versioning", suite.versions), ("Lifecycle configuration", suite.lifecycle),
                                    ("Restart and data persistence", suite.restart)]:
                suite.run_case(title, function)
    except (KeyboardInterrupt, Deadline):
        # run_case records an interrupted test; interrupts between cases need their own failure.
        if suite and (not suite.report["results"] or suite.report["results"][-1]["status"] == "PASS"):
            suite.report["results"].append({"name": "Run interrupted", "status": "FAIL", "seconds": 0, "actual": "Interrupted or deadline exceeded"})
    except Exception as error:
        if not suite:
            print(f"Cannot start check: {type(error).__name__}: {error}", file=sys.stderr)
            return 2
        suite.report["results"].append({"name": "Runner", "status": "FAIL", "seconds": 0, "actual": suite.redact(error)})
    finally:
        signal.alarm(0)
        if suite:
            # Finish independently of the overall deadline, using bounded process stop.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            suite.run_case("Shutdown and server logs", suite.log_check)
            if all(r["status"] == "PASS" for r in suite.report["results"]):
                suite.run_case("Remove isolated test data", suite.cleanup_data)
            for client in suite.clients:
                client.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return suite.finish()


if __name__ == "__main__":
    raise SystemExit(main())
