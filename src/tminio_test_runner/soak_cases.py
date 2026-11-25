"""Sustained endpoint correctness with bounded data, keys and recent request logs."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import os
import signal
import threading
import time

from .binary_check import check

DEFAULT_WORKERS = 32
KEY_SLOTS = 32
OBJECT_SIZES = (64 * 1024, 1024 * 1024, 16 * 1024 * 1024)
PROGRESS_SECONDS = 30
REQUEST_LOG_BYTES = 64 * 1024 * 1024


def _drain_workers(executor, stop):
    """Finish started cycles before teardown, even after repeated interrupts."""
    handlers = {}
    try:
        if threading.current_thread() is threading.main_thread():
            # A second interrupt during Thread.join can escape shutdown and
            # leave requests racing global teardown. The first failure is
            # already being propagated; let bounded SDK calls and cycle cleanup
            # finish, then restore the caller's handlers for its own teardown.
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
                handlers[signum] = signal.signal(signum, signal.SIG_IGN)
        stop.set()
        executor.shutdown(wait=True, cancel_futures=True)
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def configure_soak(suite, seconds, workers):
    suite.soak_seconds, suite.soak_workers, suite.soak_key_slots = seconds, workers, KEY_SLOTS
    suite.request_log_limit = REQUEST_LOG_BYTES
    suite.report.update(mode="soak", object_sizes=list(OBJECT_SIZES),
                        scope="Mixed-size sustained S3 correctness on one running cluster; automatic setup and teardown.")
    for name in ("multipart_bytes", "multipart_part_bytes", "listing_keys", "overwrite_cycles"):
        suite.report.pop(name, None)
    suite.report["not_covered"] = [
        "Every pool/erasure set/drive handling workload data; key placement is not forced",
        "Performance targets, server resource monitoring, node outages and service restarts",
        "Large multi-part uploads, deliberate multipart aborts and lifecycle expiry; use the normal endpoint suite for these",
        "Replication, external integrations and pre-upgrade application-data integrity",
    ]
    suite.report["request_log"] = {"max_file_bytes": REQUEST_LOG_BYTES, "backups": 3,
                                   "rotations": 0, "coverage": "recent requests only"}
    suite.report["soak"] = {
        "seconds": seconds, "workers": workers, "key_slots_per_worker": KEY_SLOTS,
        "object_sizes": list(OBJECT_SIZES), "cycles_per_worker": [0] * workers,
        "cycles_per_size": {str(size): 0 for size in OBJECT_SIZES},
        "unique_keys_used": 0, "bytes_verified": 0, "elapsed_seconds": 0,
        "max_keys": workers * KEY_SLOTS * 2, "max_live_versions_and_markers": workers * 4,
        "max_pending_uploads": workers, "state": "not started",
    }


def bounded_soak(suite):
    """Mixed-size clients verify original bytes and retire every cycle's versions, markers and uploads."""
    bucket = suite.extra_bucket("soak")
    suite.enable_versioning(bucket)
    clients = [suite.make_client() for _ in range(suite.soak_workers)]
    stop = threading.Event()
    start = time.monotonic()
    until = start + suite.soak_seconds
    stats = suite.report["soak"]
    stats["state"] = "running"
    used_keys = set()

    def snapshot(state="running", display=True):
        with suite.state_lock:
            stats["elapsed_seconds"] = round(time.monotonic() - start, 3)
            stats["state"] = state
            suite.save()
            if display:
                print(f"  SOAK {stats['elapsed_seconds']:.0f}s / {suite.soak_seconds}s: "
                      f"{sum(stats['cycles_per_worker'])} cycles, "
                      f"{stats['bytes_verified'] / (1024 ** 3):.2f} GiB verified, "
                      f"{stats['unique_keys_used']}/{stats['max_keys']} keys used", flush=True)

    def worker(index, client):
        count = 0
        def call(method, **kwargs):
            if method in ("put_object", "copy_object"):
                with suite.state_lock:
                    used_keys.add(kwargs["Key"])
                    stats["unique_keys_used"] = len(used_keys)
            return suite.call(method, client=client, Bucket=bucket, **kwargs)
        def cleanup(method, **kwargs):
            return suite.call(method, client=client, Bucket=bucket, **kwargs)
        def verify(name, vid, size, digest):
            response = call("get_object", Key=name, VersionId=vid)
            actual, length = hashlib.sha256(), 0
            with response["Body"] as body:
                while True:
                    block = body.read(1024 * 1024)
                    if not block:
                        break
                    actual.update(block)
                    length += len(block)
            check(length == size and actual.hexdigest() == digest, "soak data checksum or length differs")
            head = call("head_object", Key=name, VersionId=vid)
            check(head["ContentLength"] == size, "soak HEAD size differs")
            check(head.get("Metadata", {}).get("cycle") == str(count), "soak metadata differs")
        try:
            # Stop only between cycles. Once a PUT starts, finish its requests,
            # multipart completion and verification before retiring its data.
            while not stop.is_set() and time.monotonic() < until:
                size = OBJECT_SIZES[(index + count) % len(OBJECT_SIZES)]
                # Avoid a repeated, highly compressible block that understates storage traffic.
                data = os.urandom(size)
                digest = hashlib.sha256(data).hexdigest()
                versions, upload = [], None
                slot = count % suite.soak_key_slots
                key, copy = f"worker-{index}/slot-{slot}/data", f"worker-{index}/slot-{slot}/copy"
                try:
                    result = call("put_object", Key=key, Body=data, Metadata={"cycle": str(count)})
                    version = result["VersionId"]
                    versions.append((key, version))
                    call("put_object_tagging", Key=key, VersionId=version, Tagging={"TagSet": [{"Key": "cycle", "Value": str(count)}]})
                    check(call("get_object_tagging", Key=key, VersionId=version)["TagSet"] == [{"Key": "cycle", "Value": str(count)}], "soak tags differ")
                    copied = call("copy_object", Key=copy, CopySource={"Bucket": bucket, "Key": key, "VersionId": version})
                    versions.append((copy, copied["VersionId"]))
                    for name, vid in versions:
                        verify(name, vid, size, digest)
                    marker = call("delete_object", Key=key)["VersionId"]
                    versions.append((key, marker))
                    suite.missing(bucket, key, client=client)
                    upload = call("create_multipart_upload", Key=key, Metadata={"cycle": str(count)})["UploadId"]
                    part = call("upload_part", Key=key, UploadId=upload, PartNumber=1, Body=data)
                    completed_upload = call("complete_multipart_upload", Key=key, UploadId=upload,
                                            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"]}]})
                    versions.append((key, completed_upload["VersionId"]))
                    with suite.state_lock:
                        suite.known_uploads.discard((bucket, key, upload))
                    upload = None
                    verify(key, completed_upload["VersionId"], size, digest)
                except BaseException:
                    # Cleanup may wait on the server. Stop new sibling cycles
                    # immediately while allowing already-started ones to finish.
                    stop.set()
                    raise
                finally:
                    # Always try every known resource. Global teardown reconciles lost responses.
                    failures = []
                    if upload:
                        try:
                            cleanup("abort_multipart_upload", Key=key, UploadId=upload)
                            with suite.state_lock:
                                suite.known_uploads.discard((bucket, key, upload))
                        except Exception as error:
                            failures.append(error)
                    for name, vid in versions:
                        try:
                            cleanup("delete_object", Key=name, VersionId=vid)
                            with suite.state_lock:
                                suite.known_versions.discard((bucket, name, vid))
                        except Exception as error:
                            failures.append(error)
                    data = None
                    if failures:
                        raise AssertionError(f"soak cycle cleanup failed for {len(failures)} resources") from failures[0]
                count += 1
                with suite.state_lock:
                    stats["cycles_per_worker"][index] = count
                    stats["cycles_per_size"][str(size)] += 1
                    stats["bytes_verified"] += 3 * size
            return count
        except BaseException:
            stop.set()
            raise

    executor = ThreadPoolExecutor(max_workers=suite.soak_workers)
    completed = False
    try:
        futures = [executor.submit(worker, i, client) for i, client in enumerate(clients)]
        pending = set(futures)
        snapshot()
        next_progress = start + PROGRESS_SECONDS
        while pending:
            done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
            if time.monotonic() >= next_progress:
                snapshot()
                next_progress = time.monotonic() + PROGRESS_SECONDS
        check(all(stats["cycles_per_worker"]), "a soak worker did not complete a cycle")
        check(not suite.history(bucket), "soak left versions or delete markers after its cycles")
        check(not suite.cleanup_uploads(bucket), "soak left incomplete uploads after its cycles")
        suite.health()
        completed = True
    finally:
        _drain_workers(executor, stop)
        snapshot("finished" if completed else "interrupted or failed")
