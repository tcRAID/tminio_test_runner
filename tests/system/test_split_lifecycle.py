"""Retained multi-pool protections and the explicit merged-cleanup rollback."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from tminio_test_runner.helpers import assert_missing, assert_version, digest, eventually, list_versions, make_client, put_backdated, put_version
from tminio_test_runner.pool_layout import changed_pool, metadata_snapshot, newest_pool_min_free, pool_roots

pytestmark = [pytest.mark.integration, pytest.mark.admin]


def _multipart(client, bucket, key):
    body = b"multipart-placement" * 1024
    upload = client.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    try:
        part = client.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=1, Body=body)
        result = client.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload,
                                                 MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"]}]})
        assert result.get("VersionId") and result["VersionId"] != "null"
        return {"key": key, "version": result["VersionId"], "sha256": digest(body), "size": len(body), "metadata": {}}
    except BaseException:
        try:
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
        except Exception:
            pass
        raise


def _old(settings, bucket, key, days):
    dedicated = make_client(settings["source"], settings.get("request_timeout", 20))
    try:
        return put_backdated(dedicated, bucket, key, f"{key}-{days}".encode(),
                             datetime.now(timezone.utc) - timedelta(days=days))
    finally:
        dedicated.close()


def _sole_marker(src, bucket, key):
    data = put_version(src, bucket, key, b"marker-control")
    marker = src.delete_object(Bucket=bucket, Key=key)
    assert marker.get("DeleteMarker") and marker.get("VersionId")
    src.delete_object(Bucket=bucket, Key=key, VersionId=data["version"])
    return marker["VersionId"]


def _control_gone(src, bucket, key):
    assert not list_versions(src, bucket, key), "control expiry has not run"


def test_put_and_multipart_follow_newest_pool_min_free(src, settings, bucket_factory, artifacts, mc):
    roots, bucket, key = pool_roots(settings), bucket_factory(), "placement/object"
    records, evidence = [], []
    with newest_pool_min_free(mc, artifacts, 1):
        records.append(put_version(src, bucket, key, b"first"))
        before = metadata_snapshot(roots, bucket, key)
        assert len(before) == 1, "new-object fixture is not visible in exactly one pool"
        newest = next(iter(before))
        for operation in (lambda: put_version(src, bucket, key, b"overwrite"), lambda: _multipart(src, bucket, key)):
            records.append(operation())
            after = metadata_snapshot(roots, bucket, key)
            actual = changed_pool(before, after)
            assert actual == newest and set(after) == {newest}, "PUT/multipart left a pool with available space"
            evidence.append({"min_free": 1, "pool": actual})
            before = after
    with newest_pool_min_free(mc, artifacts, 99):
        for operation in (lambda: put_version(src, bucket, key, b"spill"), lambda: _multipart(src, bucket, key)):
            records.append(operation())
            after = metadata_snapshot(roots, bucket, key)
            actual = changed_pool(before, after)
            assert actual != newest, "fixture did not spill; verify free space and complete pool-root coverage"
            evidence.append({"min_free": 99, "previous_pool": newest, "pool": actual})
            newest, before = actual, after
    for record in records:
        assert_version(src, bucket, record)
    assert src.head_object(Bucket=bucket, Key=key)["VersionId"] == records[-1]["version"]
    (artifacts / "placement-put-multipart.json").write_text(json.dumps({"bucket": bucket, "writes": evidence}, indent=2))


def test_split_current_expiry_and_version_reads(src, settings, bucket_factory, artifacts, mc):
    roots, bucket, key = pool_roots(settings), bucket_factory(), "split-current"
    with newest_pool_min_free(mc, artifacts, 99):
        old = _old(settings, bucket, key, 40)
        fresh = put_version(src, bucket, key, b"current-must-survive")
        layout = metadata_snapshot(roots, bucket, key)
        assert len(layout) == 2, "fixture must span two pools"
        _old(settings, bucket, "control", 40)
    src.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
        {"ID": "current", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 30}}]})
    eventually(lambda: assert_missing(src, bucket, "control"), settings.get("convergence_timeout", 1800), interval=2)
    assert src.head_object(Bucket=bucket, Key=key)["VersionId"] == fresh["version"]
    for record in (old, fresh):
        assert_version(src, bucket, record)
    assert {v["VersionId"] for v in list_versions(src, bucket, key)} == {old["version"], fresh["version"]}
    (artifacts / "split-current.json").write_text(json.dumps({"bucket": bucket, "data_pools": sorted(layout), "old_version": old["version"], "current": fresh["version"]}, indent=2))


def test_split_single_delete_marker_stays_in_newest_pool(src, settings, bucket_factory, artifacts, mc):
    roots, bucket, key = pool_roots(settings), bucket_factory(), "split-marker"
    with newest_pool_min_free(mc, artifacts, 99):
        old = put_version(src, bucket, key, b"old-still-needed")
        before = metadata_snapshot(roots, bucket, key)
        newest = put_version(src, bucket, key, b"newest")
        after = metadata_snapshot(roots, bucket, key)
        newest_pool = changed_pool(before, after)
        assert len(after) == 2, "fixture must span two pools"
        response = src.delete_object(Bucket=bucket, Key=key)
        assert response.get("DeleteMarker") and response.get("VersionId")
        marker = response["VersionId"]
        assert changed_pool(after, metadata_snapshot(roots, bucket, key)) == newest_pool
        src.delete_object(Bucket=bucket, Key=key, VersionId=newest["version"])
        _sole_marker(src, bucket, "control")
    src.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
        {"ID": "eodm", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"ExpiredObjectDeleteMarker": True}}]})
    eventually(lambda: _control_gone(src, bucket, "control"), settings.get("convergence_timeout", 1800), interval=2)
    assert_missing(src, bucket, key)
    assert {v["VersionId"] for v in list_versions(src, bucket, key)} == {old["version"], marker}
    assert_version(src, bucket, old)
    (artifacts / "split-marker.json").write_text(json.dumps({"bucket": bucket, "newest_pool": newest_pool, "marker": marker, "retained": old["version"]}, indent=2))


def test_split_bulk_delete_preserves_versions_after_merged_cleanup_withdrawal(src, settings, bucket_factory, artifacts, mc):
    roots, bucket, key = pool_roots(settings), bucket_factory(), "split-deleted"
    with newest_pool_min_free(mc, artifacts, 99):
        old, per_pool, before = [], {}, {}
        previous = None
        for index in range(12):
            old.append(_old(settings, bucket, key, 49 - index))
            after = metadata_snapshot(roots, bucket, key)
            placed = changed_pool(before, after)
            assert placed != previous, "fixture did not leave the newest pool"
            per_pool[placed] = per_pool.get(placed, 0) + 1
            previous, before = placed, after
        assert len(per_pool) >= 2 and max(per_pool.values()) < 10
        response = src.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key}]})
        assert not response.get("Errors")
        versions = list_versions(src, bucket, key)
        markers = {v["VersionId"] for v in versions if v["IsDeleteMarker"]}
        # Bulk DeleteVersions only creates markers where xl.meta already exists.
        # Every observed writer pool therefore needs its own retained marker.
        assert len(markers) == len(per_pool)
        expected = {r["version"] for r in old} | markers
        assert {v["VersionId"] for v in versions} == expected
        _sole_marker(src, bucket, "control")
    src.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
        {"ID": "nc", "Status": "Enabled", "Filter": {"Prefix": ""}, "NoncurrentVersionExpiration": {"NoncurrentDays": 1, "NewerNoncurrentVersions": 10}},
        {"ID": "eodm", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"ExpiredObjectDeleteMarker": True}}]})
    eventually(lambda: _control_gone(src, bucket, "control"), settings.get("convergence_timeout", 1800), interval=2)
    assert_missing(src, bucket, key)
    assert {v["VersionId"] for v in list_versions(src, bucket, key)} == expected
    for record in old:
        assert_version(src, bucket, record)
    (artifacts / "split-lifecycle-rollback.json").write_text(json.dumps({"bucket": bucket, "data_versions_per_pool": per_pool, "retained_ids": sorted(expected), "merged_reclamation_supported": False}, indent=2))
