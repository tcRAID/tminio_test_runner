"""Single-cluster checks that use only this run's buckets and read-only admin info."""
import concurrent.futures
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import time

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from binary_check import MIB, check


def cluster_shape(info):
    servers, backend = info.get("servers", []), info.get("backend", {})
    check(servers and backend.get("backendType") == "Erasure", "admin info did not return an erasure cluster")
    check(all(s.get("state") == "online" for s in servers), "a MinIO server is offline")
    check(backend.get("offlineDisks") == 0, "offline or unknown disk count")
    disks = [d for s in servers for d in s.get("drives", [])]
    check(disks and all(d.get("state") == "ok" for d in disks), "a drive is unavailable or drive details are missing")
    builds = {(s.get("version"), s.get("commitID")) for s in servers}
    check(len(builds) == 1, "cluster contains mixed MinIO builds")
    check(info.get("deploymentID"), "admin info lacks deployment identity")
    return {"deployment_id": info["deploymentID"], "nodes": len(servers), "drives": len(disks),
            "sets_per_pool": backend.get("totalSets"), "drives_per_set": backend.get("totalDrivesPerSet"),
            "parity": backend.get("standardSCParity")}


class EndpointCases:
    def cluster_health(self):
        """Admin info identifies the cluster; every server and drive is online and all nodes report one build."""
        url = self.endpoint + "/minio/admin/v3/info"
        req = AWSRequest(method="GET", url=url, headers={"X-Amz-Content-Sha256": hashlib.sha256(b"").hexdigest()})
        SigV4Auth(self.client._request_signer._credentials, "s3", self.region).add_auth(req)
        response = self.client._endpoint.http_session.send(req.prepare())
        check(response.status_code == 200, f"admin info: HTTP {response.status_code}; credentials need admin:ServerInfo")
        info = json.loads(response.content)
        self.report["cluster_shape"] = cluster_shape(info)
        self.report["server_builds"] = [{k: s.get(k) for k in ("endpoint", "version", "commitID", "uptime")}
                                         for s in info["servers"]]
        if getattr(self, "expected_commit", None):
            check(all(s.get("commitID") == self.expected_commit for s in info["servers"]),
                  "a server does not report --expected-commit")
        self.health()

    def extra_bucket(self, suffix, **options):
        bucket = "tminio-check-" + self.owner_token + "-" + suffix
        self.planned_buckets.add(bucket)
        self.report["planned_buckets"] = sorted(self.planned_buckets)
        self.create_test_bucket(bucket, **options)
        return bucket

    def enable_versioning(self, bucket):
        self.call("put_bucket_versioning", Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        check(self.call("get_bucket_versioning", Bucket=bucket).get("Status") == "Enabled", "versioning did not enable")

    def promote_null(self, record):
        self.expected.pop((record["bucket"], record["key"], record.get("version")), None)
        return self.remember({**record, "version": "null"})

    def history(self, bucket, prefix=""):
        entries = []
        for page in self.pages("list_object_versions", Bucket=bucket, Prefix=prefix):
            for field in ("Versions", "DeleteMarkers"):
                for v in page.get(field, []):
                    entries.append({"key": v["Key"], "version": v["VersionId"], "latest": v["IsLatest"],
                                    "marker": field == "DeleteMarkers", "size": v.get("Size"),
                                    "etag": v.get("ETag"), "modified": v["LastModified"].isoformat()})
        return sorted(entries, key=lambda v: (v["key"], v["version"], v["marker"]))

    def null_and_restore(self):
        """Null versions remain readable; deleting newer versions or a marker restores the exact older bytes."""
        bucket = self.extra_bucket("null")
        old = self.put(bucket, "restore", 4096, "null-original")
        self.enable_versioning(bucket)
        old = self.promote_null(old)
        middle = self.put(bucket, "restore", 8192, "middle")
        latest = self.put(bucket, "restore", 16384, "latest")
        for r in (old, middle, latest):
            self.verify(r)
        self.delete(latest)
        check(self.call("head_object", Bucket=bucket, Key="restore")["VersionId"] == middle["version"], "wrong restored version")
        self.verify(middle)
        self.delete(middle)
        check(self.call("head_object", Bucket=bucket, Key="restore").get("VersionId", "null") == "null", "null version not restored")
        marker = self.call("delete_object", Bucket=bucket, Key="restore")["VersionId"]
        self.missing(bucket, "restore")
        self.verify(old)
        self.call("delete_object", Bucket=bucket, Key="restore", VersionId=marker)
        self.verify(old)
        history = self.history(bucket)
        check(len(history) == 1 and history[0]["version"] == "null" and history[0]["latest"], "restore changed history")
        # Suspended versioning exercises replacement of the null slot as well.
        self.call("put_bucket_versioning", Bucket=bucket, VersioningConfiguration={"Status": "Suspended"})
        self.verify(self.put(bucket, "suspended", 1024, "first"))
        self.verify(self.put(bucket, "suspended", 2048, "replacement"))
        self.enable_versioning(bucket)

    def concurrent(self):
        """Eight clients race versioned PUT/DELETE; every acknowledged data version and marker survives."""
        workers, rounds = 8, 12
        clients = [self.make_client() for _ in range(workers)]
        stopped = threading.Event()
        key = "concurrent/shared"
        def worker(index):
            records, markers = [], []
            for n in range(rounds):
                if stopped.is_set():
                    break
                r = self.put(self.versioned_bucket, key, 64 * 1024, f"worker-{index}-{n}", client=clients[index])
                self.verify(r, clients[index])  # Explicit version, never guess shared latest ordering.
                records.append(r)
                marker = self.call("delete_object", client=clients[index], Bucket=self.versioned_bucket, Key=key)
                check(marker.get("DeleteMarker") and marker.get("VersionId"), "concurrent DELETE omitted marker")
                markers.append(marker["VersionId"])
                own = self.put(self.bucket, f"concurrent/worker-{index}", 4096, f"round-{n}", client=clients[index])
                self.verify(own, clients[index])
            return records, markers
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker, i) for i in range(workers)]
            try:
                results = [f.result() for f in futures]
            finally:
                stopped.set()
                # Executor drains bounded SDK requests before cleanup or reporting.
        records = [r for rs, _ in results for r in rs]
        markers = [m for _, ms in results for m in ms]
        check(len(records) == len(markers) == workers * rounds, "concurrency workload incomplete")
        expected = {(r["version"], False) for r in records} | {(m, True) for m in markers}
        check(len({r["version"] for r in records}) == len(records), "duplicate acknowledged data version IDs")
        check(not {r["version"] for r in records}.intersection(markers), "data/marker version ID collision")
        # This fork returns an existing latest marker for repeated DELETEs
        # (erasureServerPools.DeleteObject). Concurrent DELETE acknowledgements
        # may therefore share a marker ID; every distinct acknowledged ID must exist.
        actual = self.history(self.versioned_bucket, key)
        check(len(actual) == len(expected) and {(v["version"], v["marker"]) for v in actual} == expected,
              "concurrent history lost, duplicated or added a version")
        for r in records:
            self.verify(r)
        final = self.put(self.versioned_bucket, key, 1024, "final-after-workers")
        check(self.call("head_object", Bucket=self.versioned_bucket, Key=key)["VersionId"] == final["version"], "stale latest after workers")
        self.verify(final)
        self.report["concurrency"] = {"workers": workers, "rounds": rounds, "data_versions": len(records),
                                      "delete_acknowledgements": len(markers), "distinct_markers": len(set(markers))}

    def backdated(self, bucket, key, days):
        client = self.make_client()
        when = datetime.now(timezone.utc) - timedelta(days=days)
        def header(request, **_):
            request.headers["x-minio-source-mtime"] = when.isoformat().replace("+00:00", "Z")
        client.meta.events.register("before-sign.s3.PutObject", header, unique_id="fixture-age")
        try:
            record = self.put(bucket, key, 1024, key, client=client)
        finally:
            client.meta.events.unregister("before-sign.s3.PutObject", unique_id="fixture-age")
        actual = self.call("head_object", Bucket=bucket, Key=key)["LastModified"]
        check(abs((actual - when).total_seconds()) < 2, "server did not accept fixture age; cannot validate expiration")
        return record

    def lifecycle_actions(self):
        """Observe real current expiry and sole-marker removal; protected data and noncurrent reads survive."""
        bucket = self.extra_bucket("age")
        null = self.backdated(bucket, "expire/null", 3)
        self.enable_versioning(bucket)
        null = self.promote_null(null)
        old = self.backdated(bucket, "expire/versioned", 3)
        fresh = self.put(bucket, "expire/fresh", 1024)
        data = self.put(bucket, "markers/protected", 1024)
        protected_marker = self.call("delete_object", Bucket=bucket, Key=data["key"])["VersionId"]
        control = self.put(bucket, "markers/control", 1024)
        control_marker = self.call("delete_object", Bucket=bucket, Key=control["key"])["VersionId"]
        self.delete(control)
        # An aged noncurrent version protects against hiding version-ID reads.
        nc = self.backdated(bucket, "keep/history", 5)
        current = self.put(bucket, "keep/history", 2048)
        self.call("put_bucket_lifecycle_configuration", Bucket=bucket, LifecycleConfiguration={"Rules": [
            {"ID": "age-current", "Status": "Enabled", "Filter": {"Prefix": "expire/"}, "Expiration": {"Days": 1}},
            {"ID": "marker-control", "Status": "Enabled", "Filter": {"Prefix": "markers/"}, "Expiration": {"ExpiredObjectDeleteMarker": True}},
            {"ID": "keep-current", "Status": "Enabled", "Filter": {"Prefix": "keep/"}, "Expiration": {"Days": 1}},
        ]})
        limit = getattr(self, "lifecycle_timeout", 600)
        deadline = time.monotonic() + limit
        print(f"    Waiting up to {limit}s for actual lifecycle processing...", flush=True)
        while True:
            history = self.history(bucket)
            self.verify(fresh)
            self.verify(data)
            self.verify(nc)
            self.verify(current)
            self.missing(bucket, data["key"])
            protected = {(v["version"], v["marker"]) for v in history if v["key"] == data["key"]}
            check(protected == {(data["version"], False), (protected_marker, True)}, "lifecycle removed a protecting marker or data")
            expired = all(any(v["key"] == r["key"] and v["marker"] and v["latest"] for v in history) for r in (null, old))
            control_gone = not any(v["key"] == control["key"] for v in history)
            if expired and control_gone:
                for r in (null, old):
                    self.missing(bucket, r["key"])
                    self.verify(r)  # These are now noncurrent; no NC deletion rule applies.
                break
            check(time.monotonic() < deadline, "lifecycle did not converge; scanner progress is not a PASS")
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        # Scanner-generated markers belong to this explicitly observed test.
        for v in history:
            if v["key"] in (null["key"], old["key"]) and v["marker"]:
                self.known_versions.add((bucket, v["key"], v["version"]))
        self.report["lifecycle_observed"] = {"current_expiry": True, "null_current_expiry": True,
            "sole_marker_expired": control_marker, "protected_marker_retained": protected_marker,
            "noncurrent_reads": True, "forced_split_pool_layout": False, "noncurrent_reclamation": "not asserted"}
