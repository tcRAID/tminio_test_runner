"""Two-cluster replication, outage recovery and replay from a verified replica.

These tests create fresh versioned buckets. They never overwrite a configured
bucket, change cluster-wide ILM settings, or delete the resulting evidence.
EODM is enabled without TTL/NVE. General integrity fixtures keep backing data
versions; the focused grace fixture instead proves a fresh replica-only marker.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from botocore.exceptions import (
    ClientError, ConnectTimeoutError, ConnectionClosedError,
    EndpointConnectionError, ReadTimeoutError,
)

from tminio_test_runner.helpers import admin_info, eventually, list_versions, run_hook, run_parallel
from tminio_test_runner.admin_config import effective_value


pytestmark = [pytest.mark.integration, pytest.mark.replication]


def _replica_marker_headers(client, bucket, key, marker_id):
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 404
        headers = error.response["ResponseMetadata"].get("HTTPHeaders", {})
        assert headers.get("x-amz-delete-marker") == "true"
        assert headers.get("x-amz-version-id") == marker_id
        assert headers.get("x-minio-replication-deletemarker-status") == "REPLICA", (
            "marker is not a confirmed incoming replica"
        )
        return headers
    raise AssertionError("replicated marker did not hide the key")


def test_replica_marker_grace_survives_eodm_control(src, dst, mc, replication_pair, settings, artifacts):
    """A fresh replica-only marker survives after an ordinary sole marker expires."""
    source, target = replication_pair
    days = int(effective_value(mc.run("admin", "config", "get", "dst", "ilm"), "ilm",
                               "replica_delete_marker_grace_days", "MINIO_ILM_REPLICA_DELETE_MARKER_GRACE_DAYS"))
    assert 1 <= days <= 106751, "this gate needs a positive, representable replica grace configuration"
    key = "grace/replica-only"
    response = src.delete_object(Bucket=source, Key=key)
    assert response.get("DeleteMarker") and response.get("VersionId")
    marker = response["VersionId"]
    timeout = settings.get("convergence_timeout", 1800)
    eventually(lambda: _replica_marker_headers(dst, target, key, marker), timeout)
    versions = list_versions(dst, target, key)
    assert len(versions) == 1 and versions[0]["VersionId"] == marker and versions[0]["IsDeleteMarker"]
    age = datetime.now(timezone.utc) - versions[0]["LastModified"]
    assert 0 <= age.total_seconds() < days * 86400 - timeout - 60, "fixture may leave grace during the test"
    control = "grace/local-control"
    data = dst.put_object(Bucket=target, Key=control, Body=b"control")
    local_marker = dst.delete_object(Bucket=target, Key=control)
    assert data.get("VersionId") and local_marker.get("VersionId")
    dst.delete_object(Bucket=target, Key=control, VersionId=data["VersionId"])
    dst.put_bucket_lifecycle_configuration(Bucket=target, LifecycleConfiguration={"Rules": [
        {"ID": "eodm-grace", "Status": "Enabled", "Filter": {"Prefix": "grace/"},
         "Expiration": {"ExpiredObjectDeleteMarker": True}}]})
    def expired_control():
        _replica_marker_headers(dst, target, key, marker)
        assert not list_versions(dst, target, control), "ordinary sole-marker control has not expired"
    eventually(expired_control, timeout, interval=2)
    _replica_marker_headers(dst, target, key, marker)
    assert {v["VersionId"] for v in list_versions(dst, target, key)} == {marker}
    (artifacts / "replica-marker-grace.json").write_text(json.dumps({
        "source_bucket": source, "target_bucket": target, "marker": marker,
        "grace_days": days, "control_expired": True, "replica_marker_retained": True,
        "grace_expiry_boundary_tested": False}, indent=2))


@dataclass
class Version:
    key: str
    version: str
    marker: bool = False
    removed: bool = False
    sha256: str = ""
    size: int = 0
    metadata: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)
    expires: datetime | None = None


@dataclass
class Oracle:
    versions: list[Version] = field(default_factory=list)
    # A UUID predicts the current version exactly. "marker" is used only for a
    # shared key whose final operation must be DELETE, but concurrent order is
    # intentionally unspecified. Destination must then match the source UUID.
    current: dict[str, str] = field(default_factory=dict)
    bulk_calls: dict[str, int] = field(default_factory=dict)

    def active(self):
        return [v for v in self.versions if not v.removed]

    def extend(self, other):
        self.versions.extend(other.versions)
        self.current.update(other.current)
        for key, count in other.bulk_calls.items():
            self.bulk_calls[key] = self.bulk_calls.get(key, 0) + count


@pytest.fixture
def replication_pair(src, dst, mc, bucket_factory):
    return _new_pair(src, dst, mc, bucket_factory)


def _new_pair(src, dst, mc, bucket_factory):
    assert src.meta.endpoint_url != dst.meta.endpoint_url, "replication requires independent clusters"
    source_id, target_id = admin_info(src).get("deploymentID"), admin_info(dst).get("deploymentID")
    assert source_id and target_id and source_id != target_id, "replication requires distinct deployment IDs"
    source_bucket = bucket_factory(src)
    target_bucket = bucket_factory(dst)
    mc.replicate(source_bucket, target_bucket)
    config = src.get_bucket_replication(Bucket=source_bucket)["ReplicationConfiguration"]
    assert len(config["Rules"]) == 1
    rule = config["Rules"][0]
    assert rule["Status"] == "Enabled"
    assert rule["DeleteMarkerReplication"]["Status"] == "Enabled"
    return source_bucket, target_bucket


def _put(client, bucket, key, body, run_id, token, *, size=None, checksum=None):
    if size is None:
        size = len(body)
    checksum = checksum or hashlib.sha256(body).hexdigest()
    metadata = {"release-run": run_id, "token": token, "sha256": checksum}
    tags = {"suite": "replication", "token": token}
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0)
    result = client.put_object(
        Bucket=bucket, Key=key, Body=body, ContentLength=size,
        Metadata=metadata, Tagging=urlencode(tags), Expires=expires,
        ContentType="application/octet-stream",
    )
    version = result.get("VersionId")
    assert version and version != "null", f"PUT {key} has no unique version ID"
    return Version(key, version, sha256=checksum, size=size, metadata=metadata,
                   tags=tags, expires=expires)


def _single_marker(client, bucket, key):
    response = client.delete_object(Bucket=bucket, Key=key)
    version = response.get("VersionId")
    assert response.get("DeleteMarker") and version and version != "null", (
        f"DELETE {key} did not return a marker identity"
    )
    return Version(key, version, marker=True)


def _bulk_remove_versions(client, bucket, *records):
    response = client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": v.key, "VersionId": v.version}
                                          for v in records], "Quiet": False}
    )
    assert not response.get("Errors"), f"bulk version DELETE errors: {response.get('Errors')}"
    results = response.get("Deleted", [])
    expected = {(v.key, v.version) for v in records}
    assert len(results) == len(expected)
    assert {(v["Key"], v.get("VersionId")) for v in results} == expected
    for record in records:
        record.removed = True


def _remove_version(client, bucket, record):
    response = client.delete_object(Bucket=bucket, Key=record.key, VersionId=record.version)
    assert response["ResponseMetadata"]["HTTPStatusCode"] == 204
    record.removed = True


def _worker(client, bucket, run_id, phase, worker, rounds):
    oracle = Oracle()
    for iteration in range(rounds):
        key = f"{phase}/private-{worker:02d}-{iteration:02d}"
        prefix = f"{phase}-{worker}-{iteration}"
        first = _put(client, bucket, key, (prefix + "-one").encode() * 128,
                     run_id, prefix + "-one")
        second = _put(client, bucket, key, (prefix + "-two").encode() * 256,
                      run_id, prefix + "-two")
        oracle.versions.extend((first, second))
        third = _put(client, bucket, key, (prefix + "-three").encode() * 512,
                     run_id, prefix + "-three")
        oracle.versions.append(third)
        marker = _single_marker(client, bucket, key)
        oracle.versions.append(marker)
        if iteration % 2 == 0:
            _remove_version(client, bucket, marker)
            oracle.current[key] = third.version  # explicit rollback must reveal v3
        else:
            oracle.current[key] = marker.version

        shared = f"{phase}/shared-{iteration % 2}"
        shared_data = _put(client, bucket, shared, (prefix + "shared").encode() * 1024,
                           run_id, prefix + "-shared")
        shared_marker = _single_marker(client, bucket, shared)
        oracle.versions.extend((shared_data, shared_marker))
        oracle.current[shared] = "marker"

        bulk = f"{phase}/bulk-{worker:02d}-{iteration:02d}"
        bulk_old = _put(client, bucket, bulk, (prefix + "bulk-old").encode() * 256,
                        run_id, prefix + "-bulk-old")
        bulk_new = _put(client, bucket, bulk, (prefix + "bulk-new").encode() * 1024,
                        run_id, prefix + "-bulk-new")
        oracle.versions.extend((bulk_old, bulk_new))
        # Explicit UUIDs give an independent expected history even when listing
        # briefly lags a completed bulk request. Unversioned fan-out has its own test.
        _bulk_remove_versions(client, bucket, first, bulk_old)
        oracle.bulk_calls[key] = 1
        oracle.bulk_calls[bulk] = 1
        marker = _single_marker(client, bucket, bulk)
        oracle.versions.append(marker)
        oracle.current[bulk] = marker.version
    return oracle


def _traffic(client, bucket, settings, phase):
    workers = int(settings.get("replication_workers", min(int(settings.get("workers", 4)), 4)))
    rounds = int(settings.get("replication_rounds", 3))
    assert 1 <= workers <= 32 and 1 <= rounds <= 100
    assert not list_versions(client, bucket, phase + "/"), "traffic phase must use fresh keys"
    results = run_parallel(
        [lambda worker=worker: _worker(client, bucket, settings["_run_id"], phase,
                                      worker, rounds)
         for worker in range(workers)],
        progress_timeout=float(settings.get("convergence_timeout", 1800)),
    )
    oracle = Oracle()
    for result in results:
        oracle.extend(result)
    _validate_oracle(oracle)
    return oracle


def _validate_oracle(oracle):
    unique = {}
    for record in oracle.versions:
        identity = (record.key, record.version)
        previous = unique.get(identity)
        if previous is not None:
            # Accept identical repeated marker acknowledgements from concurrent
            # DELETEs; reject duplicated PUT identities or conflicting state.
            assert previous.marker and record.marker and previous == record, (
                f"duplicate data identity or conflicting marker acknowledgement: {identity}"
            )
        else:
            unique[identity] = record
    oracle.versions = list(unique.values())
    active = oracle.active()
    assert active, "an empty fixture must never pass replication verification"
    identities = [(v.key, v.version) for v in oracle.versions]
    assert len(set(identities)) == len(identities), "oracle contains duplicate version IDs"
    assert set(oracle.current) == {v.key for v in active}, "oracle current-key coverage is incomplete"
    for record in active:
        assert record.version and record.version != "null"
        if not record.marker:
            assert record.sha256 and record.metadata and record.tags and record.expires
        else:
            assert any(v.key == record.key and not v.marker for v in active), (
                "EODM fixture must keep a data version beneath every marker"
            )


def _check_deadline(deadline):
    if deadline is not None:
        assert time.monotonic() < deadline, "replication verification exceeded its overall deadline"


def _assert_data(client, bucket, record, status, deadline=None):
    _check_deadline(deadline)
    response = client.get_object(Bucket=bucket, Key=record.key, VersionId=record.version)
    stream = response["Body"]
    checksum, size = hashlib.sha256(), 0
    try:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            _check_deadline(deadline)
            checksum.update(chunk)
            size += len(chunk)
    finally:
        stream.close()
    assert size == record.size and checksum.hexdigest() == record.sha256, (
        f"payload mismatch {record.key} version={record.version}"
    )
    assert response.get("VersionId") == record.version
    assert response.get("Metadata", {}) == record.metadata
    assert response.get("ContentType") == "application/octet-stream"
    assert response.get("Expires") == record.expires
    _check_deadline(deadline)
    head = client.head_object(Bucket=bucket, Key=record.key, VersionId=record.version)
    assert head["VersionId"] == record.version and head["ContentLength"] == record.size
    assert head.get("Metadata", {}) == record.metadata
    assert head.get("Expires") == record.expires
    if status is not None:
        assert head.get("ReplicationStatus") == status, (
            f"{record.key}/{record.version} replication={head.get('ReplicationStatus')} expected={status}"
        )
    _check_deadline(deadline)
    tags = client.get_object_tagging(Bucket=bucket, Key=record.key, VersionId=record.version)
    assert {tag["Key"]: tag["Value"] for tag in tags["TagSet"]} == record.tags


def _assert_deleted_current(client, bucket, key, marker_id):
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        response = error.response
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 404
        assert response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound")
        headers = response["ResponseMetadata"].get("HTTPHeaders", {})
        assert headers.get("x-amz-delete-marker") == "true", "404 must be the expected delete marker"
        assert headers.get("x-amz-version-id") == marker_id
    else:
        raise AssertionError(f"deleted key became visible: {key}")


def _inventory(client, bucket, oracle, *, status=None, source_layout=None, deadline=None):
    _check_deadline(deadline)
    expected = {(v.key, v.version): v for v in oracle.active()}
    listed = list_versions(client, bucket)
    actual = {(v["Key"], v["VersionId"]): v for v in listed}
    assert len(actual) == len(listed), "listing contains duplicate versions"
    assert set(actual) == set(expected), (
        f"version history mismatch: missing={sorted(set(expected) - set(actual))[:8]} "
        f"extra={sorted(set(actual) - set(expected))[:8]}"
    )
    current = {}
    for identity, entry in actual.items():
        record = expected[identity]
        assert entry["IsDeleteMarker"] == record.marker, f"version type changed: {identity}"
        if not record.marker:
            assert entry["Size"] == record.size
        if entry["IsLatest"]:
            assert record.key not in current, f"multiple current versions: {record.key}"
            current[record.key] = record
        if source_layout is not None:
            source_entry = source_layout[identity]
            assert entry["LastModified"] == source_entry["LastModified"], f"replication changed ModTime: {identity}"
            assert entry["IsLatest"] == source_entry["IsLatest"], f"replication changed current version: {identity}"
    assert set(current) == set(oracle.current), "one current version is required for every key"
    for key, expected_current in oracle.current.items():
        _check_deadline(deadline)
        record = current[key]
        if expected_current == "marker":
            assert record.marker, f"shared key's final DELETE was lost: {key}"
        else:
            assert record.version == expected_current, f"rollback/current mismatch: {key}"
        if record.marker:
            _assert_deleted_current(client, bucket, key, record.version)
        else:
            head = client.head_object(Bucket=bucket, Key=key)
            assert head["VersionId"] == record.version
    for record in oracle.active():
        if not record.marker:
            _assert_data(client, bucket, record, status, deadline)
    return actual


def _converge(src, dst, pair, oracle, settings):
    _validate_oracle(oracle)
    timeout = float(settings.get("convergence_timeout", 1800))
    deadline = time.monotonic() + timeout
    last_observation = None
    def check():
        nonlocal last_observation
        try:
            source_layout = _inventory(src, pair[0], oracle, status="COMPLETED", deadline=deadline)
            _inventory(dst, pair[1], oracle, status="REPLICA", source_layout=source_layout, deadline=deadline)
            return source_layout
        except AssertionError as error:
            if "exceeded its overall deadline" not in str(error):
                last_observation = str(error).splitlines()[0]
            raise
    try:
        return eventually(check, timeout)
    except AssertionError as error:
        if last_observation is not None:
            raise AssertionError(f"replication did not converge in {timeout}s; "
                                 f"last observed mismatch: {last_observation}") from error
        raise


def _enable_eod(src, dst, pair):
    config = {"Rules": [{
        "ID": "dev-release-eod-only", "Status": "Enabled", "Filter": {"Prefix": ""},
        "Expiration": {"ExpiredObjectDeleteMarker": True},
    }]}
    for client, bucket in ((src, pair[0]), (dst, pair[1])):
        client.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration=config)
        rules = client.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
        assert len(rules) == 1 and rules[0]["Expiration"].get("ExpiredObjectDeleteMarker") is True
        assert "Days" not in rules[0]["Expiration"] and "NoncurrentVersionExpiration" not in rules[0]


def _evidence(artifacts, bucket, stage, oracle, **extra):
    payload = {"stage": stage, "source_bucket": bucket,
               "versions": [asdict(v) for v in oracle.versions],
               "expected_current": oracle.current, "bulk_delete_calls": oracle.bulk_calls,
               **extra}
    path = artifacts / f"replication-{bucket}-{stage}.json"
    path.write_text(json.dumps(payload, indent=2, default=lambda value: value.isoformat()), encoding="utf-8")


def test_replication_concurrent_histories(src, dst, replication_pair, settings, artifacts):
    """All acknowledged versions, permanent deletes and marker rollbacks converge."""
    oracle = _traffic(src, replication_pair[0], settings, "online")
    _evidence(artifacts, replication_pair[0], "online-expected", oracle)
    _converge(src, dst, replication_pair, oracle, settings)
    # Seed first: policy setup cannot consume a partially constructed history.
    _enable_eod(src, dst, replication_pair)
    _converge(src, dst, replication_pair, oracle, settings)
    _evidence(artifacts, replication_pair[0], "online-verified", oracle)


def test_replication_unversioned_bulk_fanout(src, dst, replication_pair, settings, artifacts):
    """A single bulk request must replicate every marker created in occupied pools."""
    oracle = Oracle()
    keys = ("bulk-fanout/one-version", "bulk-fanout/multiple-versions")
    for key, count in zip(keys, (1, 4)):
        for index in range(count):
            record = _put(src, replication_pair[0], key,
                          (f"{key}-{index}").encode() * 512,
                          settings["_run_id"], f"bulk-{count}-{index}")
            oracle.versions.append(record)
            oracle.current[key] = record.version
    # No lifecycle is enabled while recording the complete source history.
    _converge(src, dst, replication_pair, oracle, settings)
    response = src.delete_objects(
        Bucket=replication_pair[0],
        Delete={"Objects": [{"Key": key} for key in keys], "Quiet": False},
    )
    assert not response.get("Errors"), f"bulk DELETE errors: {response.get('Errors')}"
    results = response.get("Deleted", [])
    assert len(results) == len(keys) and {v["Key"] for v in results} == set(keys)
    acknowledged = {}
    for result in results:
        assert result.get("DeleteMarker") and result.get("DeleteMarkerVersionId")
        acknowledged[result["Key"]] = result["DeleteMarkerVersionId"]
    pool_count = len(admin_info(src)["backend"]["totalSets"])
    assert pool_count > 0
    expected_data = {(v.key, v.version) for v in oracle.versions}
    stable = {"signature": None, "polls": 0}

    def stable_source():
        previous = stable.copy()
        stable.update(signature=None, polls=0)  # Any invalid observation breaks the stable sequence.
        listed = list_versions(src, replication_pair[0])
        actual = {(v["Key"], v["VersionId"]): v for v in listed}
        assert len(actual) == len(listed), "source contains duplicate version entries"
        assert {identity for identity, v in actual.items() if not v["IsDeleteMarker"]} == expected_data
        assert {v["Key"] for v in listed} == set(keys)
        for key in keys:
            markers = [v for v in listed if v["Key"] == key and v["IsDeleteMarker"]]
            assert 1 <= len(markers) <= pool_count, "one bulk call exceeded occupied-pool marker bounds"
            assert acknowledged[key] in {v["VersionId"] for v in markers}
            current = [v for v in listed if v["Key"] == key and v["IsLatest"]]
            assert len(current) == 1 and current[0]["IsDeleteMarker"]
        signature = tuple(sorted((key, version, v["IsLatest"], v["LastModified"])
                                 for (key, version), v in actual.items()))
        if signature == previous["signature"]:
            stable.update(signature=signature, polls=previous["polls"] + 1)
        else:
            stable.update(signature=signature, polls=1)
        assert stable["polls"] >= 3, "source inventory has not stabilized over three polls"
        return listed

    listed = eventually(stable_source, float(settings.get("convergence_timeout", 1800)))
    for entry in listed:
        if entry["IsDeleteMarker"]:
            oracle.versions.append(Version(entry["Key"], entry["VersionId"], marker=True))
        if entry["IsLatest"]:
            oracle.current[entry["Key"]] = entry["VersionId"]
    _evidence(artifacts, replication_pair[0], "bulk-fanout-expected", oracle,
              acknowledged_markers=acknowledged, source_pool_count=pool_count,
              stable_source_polls=stable["polls"])
    _converge(src, dst, replication_pair, oracle, settings)
    _enable_eod(src, dst, replication_pair)
    _converge(src, dst, replication_pair, oracle, settings)
    _evidence(artifacts, replication_pair[0], "bulk-fanout-verified", oracle)


def test_replica_permanent_delete_of_noncurrent_split_version(
    src, dst, mc, bucket_factory, settings, artifacts
):
    """Permanent deletion must reach an older version in a different target pool."""
    roots = settings.get("target_pool_roots")
    if not roots:
        pytest.skip("requires target_pool_roots and an operator-controlled split target")
    assert isinstance(roots, list) and len(roots) >= 2
    assert all(isinstance(drives, list) and drives for drives in roots)
    assert all(Path(root).is_dir() for drives in roots for root in drives)
    pair = _new_pair(src, dst, mc, bucket_factory)
    old = _put(src, pair[0], "split-version", b"retained-old-version" * 512,
               settings["_run_id"], "split-old")
    oracle = Oracle([old], {old.key: old.version})
    _converge(src, dst, pair, oracle, settings)
    new = _put(src, pair[0], old.key, b"current-new-version" * 512,
               settings["_run_id"], "split-new")
    oracle.versions.append(new)
    oracle.current[new.key] = new.version
    _converge(src, dst, pair, oracle, settings)
    # Read-only proof uses exact acknowledged UUID bytes in xl.meta. Require
    # the configured drives in each holding pool to agree, then prove that the
    # two versions occupy disjoint pools. A normal unsplit run is not coverage.
    copies = {}
    for index, drives in enumerate(roots):
        contents = []
        for drive in drives:
            path = Path(drive) / pair[1] / old.key / "xl.meta"
            contents.append(path.read_bytes() if path.exists() else b"")
        copies[str(index)] = {
            record.version: sum(uuid.UUID(record.version).bytes in raw for raw in contents)
            for record in (old, new)
        }
        for count in copies[str(index)].values():
            assert count in (0, len(drives)), "configured target drives disagree on version placement"
    old_pools = {index for index, counts in copies.items() if counts[old.version]}
    new_pools = {index for index, counts in copies.items() if counts[new.version]}
    _evidence(artifacts, pair[0], "split-permanent-before", oracle,
              target_bucket=pair[1], target_version_copies=copies,
              old_target_pools=sorted(old_pools), new_target_pools=sorted(new_pools))
    assert old_pools and new_pools and old_pools.isdisjoint(new_pools), (
        "target versions did not split: configure isolated target placement before running; "
        "this is not verified split-version replication coverage"
    )
    _bulk_remove_versions(src, pair[0], old)
    _evidence(artifacts, pair[0], "split-permanent-expected", oracle,
              target_bucket=pair[1], target_version_copies=copies)
    _converge(src, dst, pair, oracle, settings)
    _evidence(artifacts, pair[0], "split-permanent-verified", oracle,
              target_bucket=pair[1], target_version_copies=copies)


def test_replication_large_put_then_delete(src, dst, replication_pair, settings, artifacts):
    """Exercise the >128MiB body route against its immediate delete marker."""
    mib = int(settings.get("replication_large_mib", 129))
    if mib == 0:
        pytest.skip("large replication fixture explicitly disabled; this is not a passed release check")
    assert 128 < mib <= 1024, "replication_large_mib must cross the 128MiB routing boundary"
    control = _put(src, replication_pair[0], "large/control", b"large-route-control",
                   settings["_run_id"], "large-control")
    oracle = Oracle([control], {control.key: control.version})
    _converge(src, dst, replication_pair, oracle, settings)
    _enable_eod(src, dst, replication_pair)
    pattern = hashlib.sha256(settings["_run_id"].encode()).digest() * (1024 * 1024 // 32)
    checksum = hashlib.sha256()
    with tempfile.TemporaryFile(dir=artifacts) as body:
        for _ in range(mib):
            body.write(pattern)
            checksum.update(pattern)
        body.seek(0)
        large = _put(src, replication_pair[0], "large/body", body, settings["_run_id"], "large-body",
                     size=mib * 1024 * 1024, checksum=checksum.hexdigest())
    oracle.versions.append(large)
    marker = _single_marker(src, replication_pair[0], large.key)
    oracle.versions.append(marker)
    oracle.current[large.key] = marker.version
    _evidence(artifacts, replication_pair[0], "large-expected", oracle, object_mib=mib)
    _converge(src, dst, replication_pair, oracle, settings)
    _evidence(artifacts, replication_pair[0], "large-verified", oracle, object_mib=mib)


def _target_unavailable(dst, bucket):
    try:
        dst.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except (ConnectTimeoutError, ConnectionClosedError, EndpointConnectionError, ReadTimeoutError) as error:
        return {"unavailable": True, "error_type": type(error).__name__}
    except ClientError as error:
        code = error.response["ResponseMetadata"]["HTTPStatusCode"]
        assert 500 <= code < 600, "auth/missing-bucket errors do not prove a target outage"
        return {"unavailable": True, "http_status": code}
    raise AssertionError("target still responds successfully after target_down")


def _backlog(src, bucket, oracle, prefix):
    pending = []
    for record in oracle.active():
        if record.marker or not record.key.startswith(prefix + "/"):
            continue
        response = src.head_object(Bucket=bucket, Key=record.key, VersionId=record.version)
        if response.get("ReplicationStatus") in ("PENDING", "FAILED"):
            pending.append((record.key, record.version, response["ReplicationStatus"]))
    assert pending, "no PENDING/FAILED versions were observed; outage did not establish a backlog"
    return pending


def _restore_from_replica(src, dst, pair, oracle, source_layout, bucket_factory, artifacts, settings):
    timeout = float(settings.get("convergence_timeout", 1800))
    deadline = time.monotonic() + timeout
    restored_bucket = bucket_factory(src)
    restored = Oracle()
    mapping = {}
    # Ordinary S3 replay creates new version IDs and times. Preserve the full
    # retained order per key, including intermediate markers, and record the map.
    items = sorted(oracle.active(), key=lambda v: (v.key, source_layout[(v.key, v.version)]["LastModified"]))
    per_key_times = {}
    for record in items:
        _check_deadline(deadline)
        stamp = source_layout[(record.key, record.version)]["LastModified"]
        seen = per_key_times.setdefault(record.key, set())
        assert stamp not in seen, "timestamp tie makes replica replay order ambiguous; keep evidence and investigate"
        seen.add(stamp)
        if record.marker:
            copied = _single_marker(src, restored_bucket, record.key)
        else:
            # Keep request bodies seekable for botocore signing/retries. Verify
            # the replica bytes again while spooling, before making them current.
            response = dst.get_object(Bucket=pair[1], Key=record.key, VersionId=record.version)
            stream = response["Body"]
            checksum, size = hashlib.sha256(), 0
            with tempfile.TemporaryFile(dir=artifacts) as body:
                try:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        _check_deadline(deadline)
                        body.write(chunk)
                        checksum.update(chunk)
                        size += len(chunk)
                finally:
                    stream.close()
                assert size == record.size and checksum.hexdigest() == record.sha256
                body.seek(0)
                result = src.put_object(
                    Bucket=restored_bucket, Key=record.key, Body=body, ContentLength=record.size,
                    Metadata=record.metadata, Tagging=urlencode(record.tags), Expires=record.expires,
                    ContentType="application/octet-stream",
                )
            assert result.get("VersionId") and result["VersionId"] != "null"
            copied = replace(record, version=result["VersionId"], removed=False)
        restored.versions.append(copied)
        restored.current[record.key] = copied.version
        mapping[f"{record.key}@{record.version}"] = copied.version
    assert len(mapping) == len(oracle.active())
    # Exact UUIDs differ intentionally; the expected current data/marker must
    # still correspond to the original verified replica's current version.
    for key in oracle.current:
        source_current = next(v for identity, v in source_layout.items()
                              if identity[0] == key and v["IsLatest"])
        assert restored.current[key] == mapping[f"{key}@{source_current['VersionId']}"]
    eventually(lambda: _inventory(src, restored_bucket, restored, deadline=deadline),
               max(0, deadline - time.monotonic()))
    _evidence(artifacts, pair[0], "restore-verified", restored,
              restore_bucket=restored_bucket, original_to_restored_version=mapping)


@pytest.mark.fault
def test_replication_outage_backlog_recovery_and_restore(
    src, dst, mc, settings, artifacts, bucket_factory
):
    """Prove a real outage/backlog, restart target, then restore retained history."""
    hooks = settings.get("faults", {})
    if not hooks.get("target_down") or not hooks.get("target_up"):
        pytest.skip("target_down and target_up argv hooks are required for replication fault recovery")
    replication_pair = _new_pair(src, dst, mc, bucket_factory)
    baseline = _put(src, replication_pair[0], "baseline/control", b"before-target-outage",
                    settings["_run_id"], "baseline-control")
    oracle = Oracle([baseline], {baseline.key: baseline.version})
    _converge(src, dst, replication_pair, oracle, settings)
    _enable_eod(src, dst, replication_pair)
    # Recovery is attempted even when target_down partially succeeds or a
    # traffic assertion raises. run_hook also persists recovery intent on disk.
    try:
        run_hook(settings, "target_down", artifacts)
        before = _target_unavailable(dst, replication_pair[1])
        oracle.extend(_traffic(src, replication_pair[0], settings, "outage"))
        after = _target_unavailable(dst, replication_pair[1])
        pending = _backlog(src, replication_pair[0], oracle, "outage")
        _evidence(artifacts, replication_pair[0], "outage-proven", oracle,
                  target_before=before, target_after=after, pending_versions=pending)
    finally:
        run_hook(settings, "target_up", artifacts)
    _converge(src, dst, replication_pair, oracle, settings)
    # New traffic after restart catches a recovery that only drains old tasks
    # but leaves live replication workers unusable.
    oracle.extend(_traffic(src, replication_pair[0], settings, "recovered"))
    layout = _converge(src, dst, replication_pair, oracle, settings)
    _evidence(artifacts, replication_pair[0], "recovery-verified", oracle)
    _restore_from_replica(src, dst, replication_pair, oracle, layout,
                          bucket_factory, artifacts, settings)


@pytest.mark.fault
def test_replication_repeated_pending_delete_preserves_marker(
    src, dst, mc, settings, artifacts, bucket_factory
):
    """Repeated DELETE must keep pending markers and must never reveal old data."""
    hooks = settings.get("faults", {})
    if not hooks.get("target_down") or not hooks.get("target_up"):
        pytest.skip("target_down and target_up hooks are required for pending-marker coverage")
    pair = _new_pair(src, dst, mc, bucket_factory)
    record = _put(src, pair[0], "pending-marker", b"must-remain-hidden-after-delete" * 256,
                  settings["_run_id"], "pending-marker-original")
    oracle = Oracle([record], {record.key: record.version})
    _converge(src, dst, pair, oracle, settings)

    # Deliberately leave lifecycle disabled: losing this marker cannot be EODM.
    observations = []
    acknowledgements = []

    def local_head(version=None):
        arguments = {"Bucket": pair[0], "Key": record.key}
        if version is not None:
            arguments["VersionId"] = version
        try:
            response = src.head_object(**arguments)
        except ClientError as error:
            response = error.response
        return {
            "status": response["ResponseMetadata"]["HTTPStatusCode"],
            "headers": {key: value for key, value in
                        response["ResponseMetadata"].get("HTTPHeaders", {}).items()
                        if any(word in key for word in ("version", "replication", "delete-marker"))},
        }

    try:
        run_hook(settings, "target_down", artifacts)
        unavailable = _target_unavailable(dst, pair[1])
        barrier = threading.Barrier(2)

        def remove():
            barrier.wait(timeout=10)
            return _single_marker(src, pair[0], record.key)

        markers = run_parallel([remove, remove], progress_timeout=30)
        acknowledgements = [marker.version for marker in markers]
        oracle.versions.extend(markers)
        oracle.current[record.key] = "marker"
        _validate_oracle(oracle)
        until = time.monotonic() + 5
        while True:
            current = local_head()
            marker_states = {version: local_head(version) for version in set(acknowledgements)}
            observation = {"current": current, "markers": marker_states}
            observations.append(observation)
            if current["status"] == 200 and current["headers"].get("x-amz-version-id") == record.version:
                _assert_data(src, pair[0], record, "COMPLETED")
                observation["original_data_resurrection_hash_verified"] = True
            _evidence(artifacts, pair[0], "repeated-pending-marker", oracle,
                      target_bucket=pair[1], lifecycle_configured=False,
                      target_outage=unavailable, acknowledged_marker_ids=acknowledgements,
                      observations=observations)
            assert current["status"] == 404 and current["headers"].get("x-amz-delete-marker") == "true", (
                f"repeated DELETE revealed data: current={current} acknowledged_markers={acknowledgements}"
            )
            assert current["headers"].get("x-amz-version-id") in acknowledgements
            for version, state in marker_states.items():
                assert state["status"] == 405 and state["headers"].get("x-amz-delete-marker") == "true", (
                    f"acknowledged pending marker disappeared: {version} state={state}"
                )
                assert state["headers"].get("x-amz-version-id") == version
            if time.monotonic() >= until:
                break
            time.sleep(0.25)
    finally:
        run_hook(settings, "target_up", artifacts)
    _converge(src, dst, pair, oracle, settings)


@contextmanager
def _source_request(client, operation, timestamp, *, version=None, marker=False, replica=False):
    """Use MinIO's signed replication fields only on fresh, test-owned buckets."""
    event = "before-sign.s3." + operation
    identity = "release-version-order-" + uuid.uuid4().hex

    def inject(request, **_):
        request.headers["x-minio-source-mtime"] = timestamp
        if version is not None:
            parts = urlsplit(request.url)
            query = [(key, value) for key, value in parse_qsl(parts.query)
                     if key != "versionId"]
            query.append(("versionId", version))
            request.url = urlunsplit(parts._replace(query=urlencode(query)))
        if marker:
            request.headers["x-minio-source-deletemarker"] = "true"
        if marker or replica:
            request.headers["x-minio-source-replication-request"] = "true"
            request.headers["x-amz-replication-status"] = "REPLICA"

    client.meta.events.register(event, inject, unique_id=identity)
    try:
        yield
    finally:
        client.meta.events.unregister(event, unique_id=identity)


def test_replication_equal_time_data_versions(src, dst, replication_pair, settings, artifacts):
    """Exact clock ties must survive asynchronous metadata and replica arrival order."""
    # ListObjectVersions serializes milliseconds; use an exactly representable
    # timestamp while still giving every write the identical source time.
    timestamp = "2026-06-23T12:34:56.123000Z"
    # Highest UUID arrives first, so arrival order cannot accidentally pass.
    versions = ("ffffffff-ffff-4fff-bfff-ffffffffffff",
                "11111111-1111-4111-8111-111111111111",
                "88888888-8888-4888-8888-888888888888")
    key = "equal-time/data"
    oracle = Oracle(current={key: versions[0]})
    for index, version in enumerate(versions):
        with _source_request(src, "PutObject", timestamp, version=version):
            record = _put(src, replication_pair[0], key, (f"tie-{index}").encode() * 1024,
                          settings["_run_id"], f"equal-time-{index}")
        assert record.version == version, "server did not accept explicit test version"
        oracle.versions.append(record)
    layout = _converge(src, dst, replication_pair, oracle, settings)
    expected_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert all(entry["LastModified"] == expected_time for entry in layout.values()), (
        "server did not retain the exact timestamp tie"
    )
    for client, bucket in ((src, replication_pair[0]), (dst, replication_pair[1])):
        response = client.get_object(Bucket=bucket, Key=key)
        try:
            assert response["VersionId"] == versions[0], "GET and version listing disagree"
            assert hashlib.sha256(response["Body"].read()).hexdigest() == oracle.versions[0].sha256
        finally:
            response["Body"].close()
    _evidence(artifacts, replication_pair[0], "equal-time-data-verified", oracle,
              exact_timestamp=timestamp, arrival_order=list(versions))


def test_replication_equal_time_marker_replay(src, dst, bucket_factory, artifacts):
    """Replay the same marker UUIDs in opposite orders on two independent clusters."""
    # No rule or lifecycle is installed: this exercises incoming replica marker
    # creation directly, without a background worker supplying a favourable order.
    pair = (bucket_factory(src), bucket_factory(dst))
    timestamp = "2026-06-23T12:34:56.123000Z"
    low = "11111111-1111-4111-8111-111111111111"
    high = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    key = "equal-time/markers"
    oracle = Oracle([Version(key, low, marker=True), Version(key, high, marker=True)], {key: high})
    layouts = []
    for client, bucket, order in ((src, pair[0], (high, low)), (dst, pair[1], (low, high))):
        for version in order:
            with _source_request(client, "DeleteObject", timestamp, marker=True):
                result = client.delete_object(Bucket=bucket, Key=key, VersionId=version)
            assert result.get("DeleteMarker") and result.get("VersionId") == version, (
                "incoming marker fixture was not created"
            )
        layout = _inventory(client, bucket, oracle)
        assert all(entry["LastModified"] == datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                   for entry in layout.values()), "server changed the tied timestamp"
        layouts.append(layout)
    assert {identity: entry["IsLatest"] for identity, entry in layouts[0].items()} == {
        identity: entry["IsLatest"] for identity, entry in layouts[1].items()
    }
    _evidence(artifacts, pair[0], "equal-time-markers-verified", oracle,
              target_bucket=pair[1], exact_timestamp=timestamp,
              source_arrival=[high, low], target_arrival=[low, high])


def test_replication_marker_retry_is_idempotent(src, dst, bucket_factory, settings, artifacts):
    """Retrying an incoming marker must never toggle it into a permanent delete."""
    for site, client in (("source", src), ("target", dst)):
        bucket = bucket_factory(client)
        key = "replica-marker-retry"
        data = _put(client, bucket, key, b"retained-before-replica-marker" * 1024,
                    settings["_run_id"], "retry-" + site)
        marker = Version(key, str(uuid.uuid4()), marker=True)
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        oracle = Oracle([data, marker], {key: marker.version})

        def receive_marker():
            result = client.delete_object(Bucket=bucket, Key=key, VersionId=marker.version)
            assert result.get("DeleteMarker") and result.get("VersionId") == marker.version
            return result

        # The second call models a committed request whose HTTP response was
        # lost. Concurrent repetitions model duplicate queued/retried delivery.
        with _source_request(client, "DeleteObject", timestamp, marker=True):
            receive_marker()
            _inventory(client, bucket, oracle)
            receive_marker()
            _inventory(client, bucket, oracle)
            run_parallel([receive_marker] * 8, progress_timeout=float(settings["progress_timeout"]))
        _inventory(client, bucket, oracle)
        _evidence(artifacts, bucket, "replica-marker-retries-verified", oracle,
                  site=site, sequential_deliveries=2, concurrent_deliveries=8)

        # A real incoming explicit version purge is a different operation and
        # must still remove the marker, including an idempotent purge retry.
        with _source_request(client, "DeleteObject", timestamp, replica=True):
            for _ in range(2):
                client.delete_object(Bucket=bucket, Key=key, VersionId=marker.version)
        marker.removed = True
        oracle.current[key] = data.version
        _inventory(client, bucket, oracle)
        _evidence(artifacts, bucket, "replica-marker-purge-verified", oracle, site=site)


def test_replication_resync_restores_missing_completed_marker(
        src, dst, mc, replication_pair, settings, artifacts):
    """Explicit resync repairs historical target loss despite source COMPLETED."""
    pair = replication_pair
    key = "resync/historical-marker-loss"
    data = _put(src, pair[0], key, b"data-must-remain-hidden-after-resync" * 1024,
                settings["_run_id"], "resync-retained-data")
    marker = _single_marker(src, pair[0], key)
    oracle = Oracle([data, marker], {key: marker.version})
    before = _converge(src, dst, pair, oracle, settings)

    def completed_source_marker():
        try:
            src.head_object(Bucket=pair[0], Key=key, VersionId=marker.version)
        except ClientError as error:
            response = error.response
            assert response["ResponseMetadata"]["HTTPStatusCode"] == 405
            headers = response["ResponseMetadata"].get("HTTPHeaders", {})
            assert headers.get("x-amz-delete-marker") == "true"
            assert headers.get("x-amz-version-id") == marker.version
            assert headers.get("x-minio-replication-deletemarker-status") == "COMPLETED", (
                "fixture must have a completed source marker before simulating historical loss"
            )
            return {name: headers[name] for name in (
                "x-amz-delete-marker", "x-amz-version-id",
                "x-minio-replication-deletemarker-status")}
        raise AssertionError("expected an explicit delete marker, not a readable source version")

    source_state = eventually(completed_source_marker, float(settings["convergence_timeout"]))
    rule = src.get_bucket_replication(Bucket=pair[0])["ReplicationConfiguration"]["Rules"]
    assert len(rule) == 1 and rule[0]["Status"] == "Enabled"
    arn = rule[0]["Destination"]["Bucket"]
    assert arn.startswith("arn:minio:replication:"), "resync must target the test-owned replication ARN"
    _evidence(artifacts, pair[0], "resync-before-loss", oracle,
              target_bucket=pair[1], source_marker_state=source_state, target_arn=arn)

    # Only this fresh target bucket is modified. A signed replica purge with
    # the marker-creation flag absent (false) models loss caused by older code;
    # it must not enqueue a new source-side user DELETE or alter the oracle.
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _source_request(dst, "DeleteObject", timestamp, replica=True):
        result = dst.delete_object(Bucket=pair[1], Key=key, VersionId=marker.version)
    assert result["ResponseMetadata"]["HTTPStatusCode"] == 204
    damaged = Oracle([data], {key: data.version})
    _inventory(dst, pair[1], damaged, status="REPLICA")
    try:
        dst.head_object(Bucket=pair[1], Key=key, VersionId=marker.version)
    except ClientError as error:
        assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 404, (
            "historical-loss fixture must prove marker absence, not a storage or auth error"
        )
    else:
        raise AssertionError("target marker purge did not remove the explicit version")
    response = dst.get_object(Bucket=pair[1], Key=key)
    try:
        assert response["VersionId"] == data.version
        assert hashlib.sha256(response["Body"].read()).hexdigest() == data.sha256
    finally:
        response["Body"].close()
    source_state = completed_source_marker()
    _inventory(src, pair[0], oracle, status="COMPLETED", source_layout=before)
    _evidence(artifacts, pair[0], "resync-historical-loss-proven", oracle,
              target_bucket=pair[1], missing_target_marker=marker.version,
              exposed_target_data=data.version, source_marker_state=source_state)

    def command(operation):
        payload = json.loads(mc.run("--json", "replicate", "resync", operation,
                                    "src/" + pair[0], "--remote-bucket", arn, timeout=60))
        assert payload.get("status") == "success"
        assert payload.get("op") == operation and payload.get("url") == "src/" + pair[0]
        targets = payload.get("resyncInfo", {}).get("target", [])
        assert len(targets) == 1 and targets[0].get("arn") == arn, (
            "resync response does not identify the single test-owned target"
        )
        return targets[0]

    started = command("start")
    reset_id = started.get("resetid")
    assert reset_id and str(uuid.UUID(reset_id)) == reset_id, "resync returned no valid run identity"
    _evidence(artifacts, pair[0], "resync-started", oracle,
              target_bucket=pair[1], target_arn=arn, resync=started)

    def completed_resync():
        state = command("status")
        assert state.get("resetid") == reset_id, "resync status belongs to another run"
        assert state.get("resyncStatus") == "Completed", f"resync did not complete: {state}"
        assert state.get("failedReplicationCount", 0) == 0
        assert state.get("failedReplicationSize", 0) == 0
        assert state.get("replicationCount", 0) >= len(oracle.active()), (
            "resync completed without accounting for the original data and marker"
        )
        return state

    finished = eventually(completed_resync, float(settings["convergence_timeout"]))
    _converge(src, dst, pair, oracle, settings)
    # Resync must restore the original identities and timestamps, not replace
    # either version with a newly generated marker or replayed data version.
    for client, bucket, status in ((src, pair[0], "COMPLETED"), (dst, pair[1], "REPLICA")):
        _inventory(client, bucket, oracle, status=status, source_layout=before)
    completed_source_marker()
    _evidence(artifacts, pair[0], "resync-original-history-restored", oracle,
              target_bucket=pair[1], target_arn=arn, resync=finished,
              explicit_resync_required_by_test=True, automatic_repair_proven=False)
