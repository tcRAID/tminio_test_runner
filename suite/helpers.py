"""Bounded S3 assertions and lab hooks; never infer correctness from HTTP 200 alone."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from hook_process import run_controlled


def make_client(spec, timeout=20, workers=16):
    endpoint = spec["endpoint"]
    if not endpoint.startswith(("http://", "https://")):
        raise ValueError("endpoint must include http:// or https://")
    access = os.environ[spec.get("access_key_env", "TMINIO_TEST_ACCESS_KEY")]
    secret = os.environ[spec.get("secret_key_env", "TMINIO_TEST_SECRET_KEY")]
    client = boto3.session.Session().client(
        "s3", endpoint_url=endpoint, aws_access_key_id=access,
        aws_secret_access_key=secret, region_name=spec.get("region", "us-east-1"),
        verify=spec.get("verify_tls", True),
        config=Config(connect_timeout=min(5, timeout), read_timeout=timeout,
                      retries={"mode": "standard", "total_max_attempts": 1},
                      max_pool_connections=max(32, workers * 2),
                      s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"))
    def no_replica_proxy(request, **_):
        # Verify this cluster's own data; a source read must not silently borrow
        # a retained replica and hide a missing/deleted local version.
        request.headers["X-Minio-Source-Proxy-Request"] = "false"
    for operation in ("GetObject", "HeadObject"):
        client.meta.events.register("before-sign.s3." + operation, no_replica_proxy)
    return client


def digest(data):
    return hashlib.sha256(data).hexdigest()


def put_version(client, bucket, key, body, **kwargs):
    response = client.put_object(Bucket=bucket, Key=key, Body=body, **kwargs)
    version = response.get("VersionId")
    assert version and version != "null", f"PUT {key} did not return a version ID"
    return {"key": key, "version": version, "sha256": digest(body),
            "size": len(body), "metadata": kwargs.get("Metadata", {})}


def assert_version(client, bucket, record):
    response = client.get_object(Bucket=bucket, Key=record["key"], VersionId=record["version"])
    stream = response["Body"]
    try:
        checksum, size = hashlib.sha256(), 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
            size += len(chunk)
    finally:
        stream.close()
    assert response.get("VersionId") == record["version"]
    assert checksum.hexdigest() == record["sha256"], f"corrupt version: {record['key']} {record['version']}"
    assert size == record["size"]
    assert response.get("Metadata", {}) == record.get("metadata", {})
    head = client.head_object(Bucket=bucket, Key=record["key"], VersionId=record["version"])
    assert head["VersionId"] == record["version"] and head["ContentLength"] == size
    return head


def list_versions(client, bucket, prefix=""):
    result = []
    for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
        result.extend(dict(v, IsDeleteMarker=False) for v in page.get("Versions", []))
        result.extend(dict(v, IsDeleteMarker=True) for v in page.get("DeleteMarkers", []))
    return result


def assert_missing(client, bucket, key, version=None):
    args = dict(Bucket=bucket, Key=key)
    if version is not None:
        args["VersionId"] = version
    try:
        client.head_object(**args)
    except ClientError as error:
        assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 404, str(error)
        assert error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}, str(error)
    else:
        raise AssertionError(f"expected absent object: {key} version={version}")


def eventually(fn, timeout, interval=1):
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            return fn()
        except (AssertionError, ClientError, BotoCoreError) as error:
            last = error
        if time.monotonic() >= deadline:
            raise AssertionError(f"did not converge in {timeout}s: {last}") from last
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def run_parallel(functions, progress_timeout=90, progress=None):
    """Use one future per bounded round so stalled completions become visible."""
    jobs = list(functions)
    if not jobs:
        raise ValueError("no work scheduled")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        pending = {pool.submit(job) for job in jobs}
        result = []
        while pending:
            done, pending = concurrent.futures.wait(
                pending, timeout=progress_timeout,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                for future in pending:
                    future.cancel()
                raise AssertionError(f"no worker completed for {progress_timeout}s; possible stalled request/lock")
            for future in done:
                result.append(future.result())
            if progress:
                progress(len(result), len(jobs))
        return result


_hook_lock = threading.Lock()


def run_hook(settings, name, artifacts, *, recovery=False):
    hooks = settings.get("faults", {})
    command = hooks.get(name)
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ValueError(f"faults.{name} must be a nonempty argv array")
    artifacts = Path(artifacts)
    state_path = artifacts / "pending-recovery.json"
    recoveries = {"node_down": "node_up", "quorum_loss": "quorum_restore", "target_down": "target_up"}
    with _hook_lock:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if name in recoveries:
            recovery_name = recoveries[name]
            if not hooks.get(recovery_name):
                raise ValueError(f"missing recovery hook {recovery_name}")
            state[name] = recovery_name
            temporary = state_path.with_name(state_path.name + ".tmp")
            temporary.write_text(json.dumps(state))
            temporary.replace(state_path)
    # Persist recovery intent BEFORE invoking a possibly partially successful fault.
    try:
        completed = run_controlled(command, artifacts=artifacts, timeout=hooks.get("hook_timeout", 60),
                                   recovery=recovery, label=name)
    except (subprocess.TimeoutExpired, OSError):
        raise AssertionError(f"hook {name} timed out or could not start; recovery remains pending") from None
    # Do not store hook stdout: operators may accidentally print credentials.
    with (artifacts / "hooks.jsonl").open("a") as out:
        out.write(json.dumps({"hook": name, "returncode": completed.returncode, "time": time.time()}) + "\n")
    if completed.returncode:
        raise AssertionError(f"hook {name} exited {completed.returncode}; check the hook on the test host")
    with _hook_lock:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        for key, recovery_name in list(state.items()):
            if recovery_name == name:
                del state[key]
        temporary = state_path.with_name(state_path.name + ".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(state_path)


def put_backdated(client, bucket, key, body, when):
    # Dedicated client prevents a temporary request header from leaking to other threads.
    event = "before-sign.s3.PutObject"
    unique = f"backdate-{threading.get_ident()}"
    def header(request, **_):
        request.headers["x-minio-source-mtime"] = when.isoformat().replace("+00:00", "Z")
    client.meta.events.register(event, header, unique_id=unique)
    try:
        record = put_version(client, bucket, key, body)
    finally:
        client.meta.events.unregister(event, unique_id=unique)
    head = client.head_object(Bucket=bucket, Key=key, VersionId=record["version"])
    assert abs((head["LastModified"] - when).total_seconds()) < 2, "server did not accept backdated fixture; do not count ILM test as passed"
    return record


def admin_info(client):
    url = client.meta.endpoint_url.rstrip("/") + "/minio/admin/v3/info"
    request = AWSRequest(method="GET", url=url, headers={"X-Amz-Content-Sha256": hashlib.sha256(b"").hexdigest()})
    SigV4Auth(client._request_signer._credentials, "s3", client.meta.region_name).add_auth(request)
    response = client._endpoint.http_session.send(request.prepare())
    assert response.status_code == 200, f"admin info failed: HTTP {response.status_code}"
    return json.loads(response.content)
