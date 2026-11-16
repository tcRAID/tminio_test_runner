"""Bounded replacement for the original growing Locust long-run workload."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time

from binary_check import check


def bounded_soak(suite):
    """Eight clients verify original checksums and remove each cycle's versions, markers and uploads."""
    bucket = suite.extra_bucket("soak")
    suite.enable_versioning(bucket)
    clients = [suite.make_client() for _ in range(8)]
    stop, until = threading.Event(), time.monotonic() + suite.soak_seconds

    def worker(index, client):
        count = 0
        call = lambda method, **kw: suite.call(method, client=client, Bucket=bucket, **kw)
        try:
            while not stop.is_set() and time.monotonic() < until:
                data = hashlib.sha256(f"{index}:{count}".encode()).digest() * 2048
                digest = hashlib.sha256(data).hexdigest()
                versions, upload = [], None
                key, copy = f"worker-{index}/data", f"worker-{index}/copy"
                try:
                    result = call("put_object", Key=key, Body=data, Metadata={"cycle": str(count)})
                    version = result["VersionId"]
                    versions.append((key, version))
                    call("put_object_tagging", Key=key, VersionId=version, Tagging={"TagSet": [{"Key": "cycle", "Value": str(count)}]})
                    check(call("get_object_tagging", Key=key, VersionId=version)["TagSet"] == [{"Key": "cycle", "Value": str(count)}], "soak tags differ")
                    copied = call("copy_object", Key=copy, CopySource={"Bucket": bucket, "Key": key, "VersionId": version})
                    versions.append((copy, copied["VersionId"]))
                    for name, vid in versions:
                        response = call("get_object", Key=name, VersionId=vid)
                        with response["Body"] as body:
                            check(hashlib.sha256(body.read()).hexdigest() == digest, "soak data checksum differs")
                        check(call("head_object", Key=name, VersionId=vid)["ContentLength"] == len(data), "soak HEAD size differs")
                    marker = call("delete_object", Key=key)["VersionId"]
                    versions.append((key, marker))
                    suite.missing(bucket, key, client=client)
                    # Upload one part then abort: bounded pending-upload coverage.
                    upload = call("create_multipart_upload", Key=key)["UploadId"]
                    call("upload_part", Key=key, UploadId=upload, PartNumber=1, Body=data)
                    call("abort_multipart_upload", Key=key, UploadId=upload)
                    with suite.state_lock:
                        suite.known_uploads.discard((bucket, key, upload))
                    upload = None
                finally:
                    if upload:
                        call("abort_multipart_upload", Key=key, UploadId=upload)
                    for name, vid in versions:
                        call("delete_object", Key=name, VersionId=vid)
                        with suite.state_lock:
                            suite.known_versions.discard((bucket, name, vid))
                count += 1
            return count
        except BaseException:
            stop.set()
            raise

    executor = ThreadPoolExecutor(max_workers=8)
    try:
        futures = [executor.submit(worker, i, client) for i, client in enumerate(clients)]
        counts = [future.result() for future in futures]
    finally:
        stop.set()
        executor.shutdown(wait=True, cancel_futures=True)
    check(all(counts), "a soak worker did not complete a cycle")
    check(not suite.history(bucket), "soak left versions or delete markers after its cycles")
    check(not suite.cleanup_uploads(bucket), "soak left incomplete uploads after its cycles")
    suite.report["soak"] = {"seconds": suite.soak_seconds, "workers": 8, "cycles_per_worker": counts,
                            "max_keys": 16, "max_live_versions_and_markers": 24, "max_pending_uploads": 8}
    suite.health()
