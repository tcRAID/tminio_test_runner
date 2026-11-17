from datetime import datetime, timedelta, timezone
import json

import pytest

from tminio_test_runner.helpers import assert_missing, assert_version, eventually, list_versions, make_client, put_backdated, put_version
from tminio_test_runner.pool_layout import metadata_snapshot, newest_pool_min_free, pool_roots

pytestmark = pytest.mark.integration


@pytest.mark.admin
def test_noncurrent_expiry_preserves_exact_versions(src, bucket_factory, settings, artifacts, mc):
    bucket = bucket_factory()
    count = int(settings.get("history_versions", 31))
    assert count >= 12
    with newest_pool_min_free(mc, artifacts, 1):
        old_client = make_client(settings["source"], settings.get("request_timeout", 20))
        try:
            old = [put_backdated(old_client, bucket, "history", f"old-{i}".encode(),
                                 datetime.now(timezone.utc) - timedelta(days=count + 20 - i)) for i in range(count)]
        finally:
            old_client.close()
        current = put_version(src, bucket, "history", b"current-must-survive")
        assert len(metadata_snapshot(pool_roots(settings), bucket, "history")) == 1, (
            "ordinary noncurrent expiry requires a proven single-pool object; merged cleanup was withdrawn"
        )
    # Enable only after all fixtures exist, otherwise construction itself races expiry.
    src.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
        {"ID": "keep-ten", "Status": "Enabled", "Filter": {"Prefix": "history"},
         "NoncurrentVersionExpiration": {"NoncurrentDays": 1, "NewerNoncurrentVersions": 10}}]})
    expected = {r["version"] for r in old[-10:] + [current]}
    def converged():
        versions = list_versions(src, bucket)
        assert not any(v["IsDeleteMarker"] for v in versions), "ILM hid current data with a marker"
        assert {v["VersionId"] for v in versions} == expected
    eventually(converged, settings.get("convergence_timeout", 1800), interval=2)
    for record in old[-10:] + [current]:
        assert_version(src, bucket, record)
    for record in old[:-10]:
        assert_missing(src, bucket, record["key"], record["version"])
    assert src.head_object(Bucket=bucket, Key="history")["VersionId"] == current["version"]
    (artifacts / f"ilm-{bucket}.json").write_text(json.dumps({"expected": sorted(expected), "seeded": count + 1}))


def test_eodm_does_not_resurrect_old_data(src, bucket_factory, settings):
    bucket = bucket_factory()
    data = [put_version(src, bucket, "deleted", f"v{i}".encode()) for i in range(12)]
    response = src.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": "deleted"}]})
    assert not response.get("Errors")
    markers = {v["VersionId"] for v in list_versions(src, bucket) if v["IsDeleteMarker"]}
    assert markers
    # A sole marker control establishes that expiry made progress.
    control = put_version(src, bucket, "control", b"old")
    src.delete_object(Bucket=bucket, Key="control")
    src.delete_object(Bucket=bucket, Key="control", VersionId=control["version"])
    src.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
        {"ID": "eodm", "Status": "Enabled", "Filter": {"Prefix": ""},
         "Expiration": {"ExpiredObjectDeleteMarker": True}}]})
    def control_expired():
        assert_missing(src, bucket, "deleted")
        assert not list_versions(src, bucket, "control")
    eventually(control_expired, settings.get("convergence_timeout", 1800), interval=2)
    assert_missing(src, bucket, "deleted")
    actual = list_versions(src, bucket, "deleted")
    assert {v["VersionId"] for v in actual if not v["IsDeleteMarker"]} == {r["version"] for r in data}
    assert {v["VersionId"] for v in actual if v["IsDeleteMarker"]} == markers
    for record in data:
        assert_version(src, bucket, record)
