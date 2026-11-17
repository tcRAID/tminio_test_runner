"""Only for an isolated dev cluster: restores the complete ILM configuration."""
import json
import threading
from functools import partial

import pytest

from tminio_test_runner.admin_config import parse_config
from tminio_test_runner.helpers import assert_missing, assert_version, eventually, list_versions, put_version, run_parallel

pytestmark = [pytest.mark.integration, pytest.mark.admin]


def test_worker_resize_during_concurrent_s3(src, bucket_factory, settings, artifacts, mc):
    assert 5 <= int(settings.get("resize_updates", 30)) <= 10000
    assert 1 <= int(settings.get("resize_rounds", 20)) <= 10000
    bucket = bucket_factory()
    original = parse_config(mc.run("admin", "config", "get", "src", "ilm"), "ilm",
                            mutable_env={"MINIO_ILM_EXPIRATION_WORKERS": None})
    # lab_runner retries this restoration if pytest is killed or times out.
    pending = artifacts / "pending-admin-restore.json"
    pending.write_text(json.dumps({"subsystem": "ilm", "arguments": original[1:]}))
    stop = threading.Event()
    records, updates = [], []
    def resize():
        for i in range(int(settings.get("resize_updates", 30))):
            if stop.is_set():
                break
            size = (1, 8, 2, 16, 4)[i % 5]
            mc.run("admin", "config", "set", "src", "ilm", f"expiration_workers={size}", timeout=45)
            updates.append(size)
        return True
    def traffic(worker):
        own = []
        for i in range(int(settings.get("resize_rounds", 20))):
            record = put_version(src, bucket, f"shared/{i % 3}", f"{worker}-{i}".encode())
            assert_version(src, bucket, record)
            src.delete_object(Bucket=bucket, Key=record["key"])
            own.append(record)
            # Produce genuinely eligible expiry work while workers are resized.
            # Merely placing a marker above retained data would not exercise the queue.
            key = f"expiry-control/{worker}/{i}"
            control = put_version(src, bucket, key, b"expire-this-marker")
            src.delete_object(Bucket=bucket, Key=key)
            src.delete_object(Bucket=bucket, Key=key, VersionId=control["version"])
            assert_missing(src, bucket, key)
        return own
    src.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [
        {"ID": "eodm", "Status": "Enabled", "Filter": {"Prefix": ""},
         "Expiration": {"ExpiredObjectDeleteMarker": True}}]})
    try:
        results = run_parallel([resize] + [partial(traffic, w) for w in range(min(settings.get("workers", 16), 8))],
                               progress_timeout=settings.get("admin_progress_timeout", 300))
        records = [r for result in results if isinstance(result, list) for r in result]
        assert len(updates) == int(settings.get("resize_updates", 30))
        assert records
        for record in records:
            assert_version(src, bucket, record)
        def controls_expired():
            assert not list_versions(src, bucket, "expiry-control/"), "eligible markers stopped draining after resize"
        eventually(controls_expired, settings.get("convergence_timeout", 1800), interval=2)
    finally:
        stop.set()
        mc.run("admin", "config", "set", "src", *original, timeout=120)
        restored = parse_config(mc.run("admin", "config", "get", "src", "ilm"), "ilm")
        assert sorted(restored) == sorted(original), "ILM configuration restoration differs"
        pending.unlink()
    (artifacts / "worker-resize.json").write_text(json.dumps({"updates": updates, "verified_versions": len(records)}))
