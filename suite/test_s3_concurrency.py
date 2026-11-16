"""API correctness under threads, lifecycle, multipart and version rollback."""
import json
import time
from functools import partial

import pytest

from helpers import assert_missing, assert_version, list_versions, put_version, run_parallel

pytestmark = pytest.mark.integration


def _round(client, bucket, worker, round_number):
    private = f"private/{worker}"
    body = f"worker={worker};round={round_number}".encode()
    old = put_version(client, bucket, private, body, Metadata={"round": str(round_number)})
    latest = put_version(client, bucket, private, body + b"-new")
    assert_version(client, bucket, old)
    client.delete_object(Bucket=bucket, Key=private, VersionId=latest["version"])
    assert client.head_object(Bucket=bucket, Key=private)["VersionId"] == old["version"]
    assert_missing(client, bucket, private, latest["version"])
    client.delete_object(Bucket=bucket, Key=private, VersionId=old["version"])
    assert_missing(client, bucket, private)
    shared = f"shared/{round_number % 4}"
    record = put_version(client, bucket, shared, body, Metadata={"worker": str(worker)})
    assert_version(client, bucket, record)
    if worker % 2:
        client.delete_object(Bucket=bucket, Key=shared)
    else:
        response = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": shared}]})
        assert not response.get("Errors"), response.get("Errors")
    return record


def _exercise(src, bucket, settings, artifacts, *, seconds=None):
    workers, rounds = settings.get("workers", 16), settings.get("rounds", 32)
    retained = []
    verified_and_removed = 0
    start, round_number = time.monotonic(), 0
    # Markers above retained versions must not be expired by EODM.
    src.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
        {"ID": "markers", "Status": "Enabled", "Filter": {"Prefix": ""},
         "Expiration": {"ExpiredObjectDeleteMarker": True}}]})
    while round_number < rounds if seconds is None else time.monotonic() - start < seconds:
        before = time.monotonic()
        retained.extend(run_parallel(
            [partial(_round, src, bucket, w, round_number) for w in range(workers)],
            progress_timeout=settings.get("progress_timeout", 90)))
        with (artifacts / "progress.jsonl").open("a") as out:
            out.write(json.dumps({"bucket": bucket, "round": round_number,
                                  "completed": verified_and_removed + len(retained), "seconds": time.monotonic() - before}) + "\n")
        round_number += 1
        if seconds is not None and round_number % int(settings.get("soak_verify_every", 25)) == 0:
            # Bound controller memory and disk growth during 24-48h runs. Every
            # acknowledged data version is verified before explicit retirement.
            for offset in range(0, len(retained), workers):
                run_parallel([partial(assert_version, src, bucket, r) for r in retained[offset:offset + workers]],
                             progress_timeout=settings.get("progress_timeout", 90))
            with (artifacts / "soak-verified.jsonl").open("a") as out:
                out.write(json.dumps({"bucket": bucket, "round": round_number, "versions": retained}) + "\n")
            for offset in range(0, len(retained), 1000):
                response = src.delete_objects(Bucket=bucket, Delete={"Objects": [
                    {"Key": r["key"], "VersionId": r["version"]} for r in retained[offset:offset + 1000]]})
                assert not response.get("Errors")
            verified_and_removed += len(retained)
            retained = []
    assert retained or verified_and_removed, "workload completed no operations"
    current = [put_version(src, bucket, f"shared/{i}", f"final-{i}".encode()) for i in range(4)]
    actual = {(v["Key"], v["VersionId"]) for v in list_versions(src, bucket, "shared/") if not v["IsDeleteMarker"]}
    assert actual == {(r["key"], r["version"]) for r in retained + current}
    # Read every acknowledged version, not a random sample.
    for offset in range(0, len(retained), workers):
        run_parallel([partial(assert_version, src, bucket, r) for r in retained[offset:offset + workers]],
                     progress_timeout=settings.get("progress_timeout", 90))
    for record in current:
        assert_version(src, bucket, record)
        assert src.head_object(Bucket=bucket, Key=record["key"])["VersionId"] == record["version"]
    (artifacts / f"versions-{bucket}.json").write_text(json.dumps(retained + current, indent=2))
    return retained


def test_concurrent_version_rollback_put_delete(src, bucket_factory, settings, artifacts):
    _exercise(src, bucket_factory(), settings, artifacts)


@pytest.mark.soak
@pytest.mark.timeout(0)
def test_soak(src, bucket_factory, settings, artifacts):
    seconds = float(settings.get("soak_seconds", 300))
    assert seconds > 0
    assert 1 <= int(settings.get("soak_verify_every", 25)) <= 1000
    # run.py supplies a parent-process deadline; every S3 request also has a timeout.
    _exercise(src, bucket_factory(), settings, artifacts, seconds=seconds)


def test_multipart_complete_and_abort(src, bucket_factory, settings, artifacts):
    bucket = bucket_factory()
    def worker(index):
        key = f"multipart/{index % 2}"
        body = bytes([index % 251]) * (5 * 1024 * 1024) + b"final-part"
        upload = src.create_multipart_upload(Bucket=bucket, Key=key, Metadata={"worker": str(index)})["UploadId"]
        try:
            parts = []
            for number, chunk in enumerate((body[:5 * 1024 * 1024], body[5 * 1024 * 1024:]), 1):
                response = src.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=number, Body=chunk)
                parts.append({"PartNumber": number, "ETag": response["ETag"]})
            response = src.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload, MultipartUpload={"Parts": parts})
            from helpers import digest
            record = {"key": key, "version": response["VersionId"], "size": len(body),
                      "sha256": digest(body), "metadata": {"worker": str(index)}}
            assert_version(src, bucket, record)
            src.delete_object(Bucket=bucket, Key=key)
            return record
        except BaseException:
            try:
                src.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
            except Exception:
                pass
            raise
    records = run_parallel([partial(worker, i) for i in range(min(settings.get("workers", 16), 8))],
                           progress_timeout=settings.get("progress_timeout", 90))
    for record in records:
        assert_version(src, bucket, record)
    upload = src.create_multipart_upload(Bucket=bucket, Key="aborted")["UploadId"]
    src.upload_part(Bucket=bucket, Key="aborted", UploadId=upload, PartNumber=1, Body=b"incomplete")
    src.abort_multipart_upload(Bucket=bucket, Key="aborted", UploadId=upload)
    assert_missing(src, bucket, "aborted")
    assert not src.list_multipart_uploads(Bucket=bucket).get("Uploads")


def test_legal_hold_rejects_permanent_delete(src, bucket_factory):
    from botocore.exceptions import ClientError
    bucket = bucket_factory(object_lock=True)
    record = put_version(src, bucket, "held", b"must-survive", ObjectLockLegalHoldStatus="ON")
    try:
        with pytest.raises(ClientError) as error:
            src.delete_object(Bucket=bucket, Key="held", VersionId=record["version"])
        status = error.value.response["ResponseMetadata"]["HTTPStatusCode"]
        detail = error.value.response["Error"]
        assert (status == 403 and detail["Code"] == "AccessDenied") or (
            status == 400 and detail["Code"] == "InvalidRequest" and "WORM protected" in detail.get("Message", ""))
        assert_version(src, bucket, record)
    finally:
        src.put_object_legal_hold(Bucket=bucket, Key="held", VersionId=record["version"], LegalHold={"Status": "OFF"})
    src.delete_object(Bucket=bucket, Key="held", VersionId=record["version"])
    assert_missing(src, bucket, "held", record["version"])


def test_governance_retention_and_bypass(src, bucket_factory):
    """Explicit lab only: short GOVERNANCE retention is preserved and can be bypassed for teardown."""
    from datetime import datetime, timedelta, timezone
    from botocore.exceptions import ClientError
    bucket = bucket_factory(object_lock=True)
    until = datetime.now(timezone.utc) + timedelta(seconds=60)
    record = put_version(src, bucket, "governed", b"retained-test-data",
                         ObjectLockMode="GOVERNANCE", ObjectLockRetainUntilDate=until)
    args = dict(Bucket=bucket, Key=record["key"], VersionId=record["version"])
    try:
        retention = src.get_object_retention(**args)["Retention"]
        assert retention["Mode"] == "GOVERNANCE"
        assert abs((retention["RetainUntilDate"] - until).total_seconds()) < 1
        with pytest.raises(ClientError) as error:
            src.delete_object(**args)
        assert error.value.response["ResponseMetadata"]["HTTPStatusCode"] in (400, 403)
        assert error.value.response["Error"]["Code"] in ("InvalidRequest", "AccessDenied")
        assert_version(src, bucket, record)
    finally:
        src.delete_object(**args, BypassGovernanceRetention=True)
    assert_missing(src, bucket, record["key"], record["version"])
