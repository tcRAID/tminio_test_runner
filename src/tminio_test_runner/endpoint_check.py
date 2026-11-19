#!/usr/bin/env python3
"""MinIO release test for Ubuntu 26.04: automatic setup and teardown on a running S3 endpoint."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import shutil
import signal
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

from botocore.exceptions import BotoCoreError, ClientError

from .binary_check import Deadline, GIB, NoRedirect, Suite, check, utcnow
from .endpoint_cases import EndpointCases
from .protocol_cases import ProtocolCases
from .credentials import credentials


CHECK_NAMES = ["Cluster health and identity", "Endpoint and test buckets", "Object operations", "Overwrite and batch delete",
               "Pagination and key names", "Multipart uploads", "Access restrictions", "Presigned URLs",
               "Conditional requests", "Bucket configuration", "Bucket policy", "Encryption behavior", "Object lock configuration",
               "Versioning", "Null versions and restoration", "Concurrent operations", "Lifecycle configuration",
               "Lifecycle actions", "Final data integrity", "Remove test buckets"]
NOT_COVERED = [
    "Existing application data not created by this test",
    "Server binary file hash, host OS/kernel, systemd configuration and automatic reboot/service control",
    "Healing, fault injection, replication between clusters, rebalance and decommission",
    "Forced split-object placement, free-space thresholds and noncurrent space reclamation",
    "Console login, external IAM/KMS/LDAP/OIDC, application workflows and performance/soak",
]


def endpoint_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname and
                 not parsed.username and not parsed.password and not parsed.query and
                 not parsed.fragment and parsed.path in ("", "/") and
                 not any(c.isspace() or ord(c) < 32 for c in value))
        if parsed.port is not None:
            valid = valid and 0 < parsed.port < 65536
    except ValueError:
        valid = False
    if not valid:
        # Do not repeat a possibly credential-bearing URL in diagnostics.
        raise argparse.ArgumentTypeError("use an http(s) S3 endpoint with host/port only; no credentials, query or path")
    return value.rstrip("/")


class EndpointSuite(ProtocolCases, EndpointCases, Suite):
    check_names = CHECK_NAMES

    def __init__(self, endpoint, access, secret, parent, timeout=1800, *, region="us-east-1", ca_file=None):
        endpoint = endpoint_url(endpoint)
        context = ssl.create_default_context(cafile=ca_file)
        self.binary = None
        self.state_lock = threading.RLock()
        self.owned_buckets = set()
        self.uncertain_buckets = set()
        self.untagged_buckets = set()
        self.lifecycle_buckets = set()
        self.policy_buckets = set()
        self.known_keys = set()
        self.known_versions = set()
        self.known_uploads = set()
        self.creating = None
        self.init_work(parent, timeout)
        self.endpoint = endpoint
        self.access, self.secret = access, secret
        self.secrets = [access, secret]
        self.region = region
        self.verify_tls = str(Path(ca_file).resolve(strict=True)) if ca_file else True
        self.http = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=context))
        prefix = "tminio-check-" + self.owner_token
        self.bucket, self.versioned_bucket, self.lifecycle_bucket = (prefix + suffix for suffix in ("-obj", "-ver", "-ilm"))
        self.planned_buckets = {self.bucket, self.versioned_bucket, self.lifecycle_bucket}
        self.report.update(mode="endpoint", endpoint=self.endpoint, region=region,
            tls_verification=self.verify_tls if self.endpoint.startswith("https:") else None, planned_buckets=sorted(self.planned_buckets),
            owned_buckets=[], uncertain_buckets=[], not_covered=NOT_COVERED,
            scope="S3 checks against the supplied service, using only this run's new test buckets.",
            server_os="not inspected", server_binary="not inspected")
        self.report.pop("binary")
        self.report["runner_os_release"] = self.report.pop("os_release")
        try:
            self.client = self.make_client()
            self.save()
        except BaseException:
            # No remote setup has run, but client initialization may fail after
            # local work allocation. Leave reports, never temporary payload dirs.
            try:
                self.cleanup_data()
            finally:
                for client in self.clients:
                    client.close()
            raise

    def make_client(self, credentials=None, service="s3"):
        client = super().make_client(credentials, service)
        if service == "s3":
            def no_replica_proxy(request, signature_version=None, **_):
                # A presigned URL must remain usable without an undocumented
                # extra signed header. SDK integrity reads disable proxying.
                if not (signature_version or "").endswith("-query"):
                    request.headers["X-Minio-Source-Proxy-Request"] = "false"
            for operation in ("GetObject", "HeadObject", "GetObjectTagging"):
                client.meta.events.register("before-sign.s3." + operation, no_replica_proxy)
        return client

    def remember(self, record):
        with self.state_lock:
            return super().remember(record)

    def record_response(self, operation, response, args):
        with self.state_lock:
            return super().record_response(operation, response, args)

    # There is deliberately no service-control path in this mode, even if a shared
    # test accidentally calls a binary-mode method in a future refactor.
    def start(self):
        raise AssertionError("endpoint mode cannot start the service")

    def stop(self, **_):
        raise AssertionError("endpoint mode cannot stop the service")

    def restart(self):
        raise AssertionError("endpoint mode cannot restart the service")

    def event(self, kind, **fields):
        with self.state_lock:
            self.locked_event(kind, **fields)

    def locked_event(self, kind, **fields):
        if kind == "mutation":
            bucket = fields.get("bucket")
            check(bucket in self.owned_buckets,
                  "mutation outside this run's buckets refused")
            if fields.get("key") is not None:
                self.known_keys.add((bucket, fields["key"]))
            for item in fields.get("objects") or []:
                self.known_keys.add((bucket, item["Key"]))
        if getattr(self, "request_log_limit", None):
            from .request_log import append_event
            rotated = append_event(self.directory / "requests.jsonl",
                                   self.safe({"time": utcnow(), "check": self.case, "kind": kind, **fields}),
                                   self.request_log_limit)
            self.report["request_log"]["rotations"] += int(rotated)
        else:
            super().event(kind, **fields)

    def call(self, method, *, client=None, **kwargs):
        bucket = kwargs.get("Bucket")
        if method == "assume_role":
            check(client is not None and bucket is None, "invalid STS call")
        elif method == "create_bucket":
            check(bucket == self.creating and bucket in self.planned_buckets,
                  "bucket creation was not reserved by this run")
        else:
            check(bucket in self.owned_buckets, "operation outside this run's buckets refused")
        if "CopySource" in kwargs:
            source = kwargs["CopySource"]
            check(isinstance(source, dict) and source.get("Bucket") in self.owned_buckets,
                  "copy source outside this run's buckets refused")
        if method == "put_bucket_lifecycle_configuration":
            self.lifecycle_buckets.add(bucket)  # Include a configuration whose response might be lost.
        result = super().call(method, client=client, **kwargs)
        if kwargs.get("Key") is not None:
            if result.get("VersionId"):
                self.known_versions.add((bucket, kwargs["Key"], result["VersionId"]))
            if method == "create_multipart_upload":
                self.known_uploads.add((bucket, kwargs["Key"], result["UploadId"]))
        return result

    def record_ownership(self):
        self.report["owned_buckets"] = sorted(self.owned_buckets)
        self.report["uncertain_buckets"] = sorted(self.uncertain_buckets)
        self.save()

    def create_test_bucket(self, bucket, *, object_lock=False):
        check(bucket in self.planned_buckets and bucket not in self.owned_buckets, "invalid new bucket")
        # A 128-bit random name plus a prior absence check prevents adopting any
        # pre-existing bucket, including one owned by the supplied credentials.
        try:
            super().call("head_bucket", Bucket=bucket)
        except ClientError as error:
            check(error.response["ResponseMetadata"]["HTTPStatusCode"] == 404,
                  "cannot establish that the new test bucket is absent (need bucket creation permissions)")
        else:
            raise AssertionError("generated bucket already exists; refusing to use it")
        self.uncertain_buckets.add(bucket)
        self.record_ownership()  # Preserve intent if the create response is lost.
        self.event("bucket_create_intent", bucket=bucket)
        self.creating = bucket
        try:
            args = {"Bucket": bucket}
            if object_lock:
                args["ObjectLockEnabledForBucket"] = True
            if self.region != "us-east-1":
                args["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            self.call("create_bucket", **args)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                # A concurrent creator won the name: this bucket is never ours.
                self.uncertain_buckets.discard(bucket)
                self.record_ownership()
            raise
        finally:
            self.creating = None
        self.owned_buckets.add(bucket)
        self.untagged_buckets.add(bucket)
        self.uncertain_buckets.remove(bucket)
        self.record_ownership()
        self.call("put_bucket_tagging", Bucket=bucket,
                  Tagging={"TagSet": [{"Key": "tminio-check-owner", "Value": self.owner_token}]})
        self.untagged_buckets.discard(bucket)

    def health(self):
        for path in ("live", "ready", "cluster", "cluster/read"):
            status, _ = self.http_request(self.endpoint + "/minio/health/" + path)
            check(status == 200, f"health/{path}: HTTP {status}; use the S3 API endpoint exposing MinIO health probes")

    def startup(self):
        """Endpoint health succeeds; three fresh private test buckets are created with ownership tags."""
        check(shutil.disk_usage(self.directory).free >= 2 * GIB, "need at least 2 GiB free local payload space")
        self.health()
        for bucket in (self.bucket, self.versioned_bucket, self.lifecycle_bucket):
            self.create_test_bucket(bucket)
        self.call("put_bucket_versioning", Bucket=self.versioned_bucket, VersioningConfiguration={"Status": "Enabled"})
        check(self.call("get_bucket_versioning", Bucket=self.versioned_bucket).get("Status") == "Enabled",
              "versioning did not enable")
        self.verify(self.put(self.bucket, "startup/persist", 1024))

    def final_integrity(self):
        """All retained bytes, metadata, versions, deletions and lifecycle rules still match; health stays good."""
        for record in self.expected.values():
            self.verify(record)
        for bucket, key in self.deleted:
            self.missing(bucket, key)
        self.check_history()
        rules = self.call("get_bucket_lifecycle_configuration", Bucket=self.lifecycle_bucket)["Rules"]
        check(sorted(rules, key=lambda r: r["ID"]) == sorted(self.report["lifecycle_rules"], key=lambda r: r["ID"]),
              "lifecycle configuration changed")
        self.health()

    def _forget_bucket(self, bucket):
        self.owned_buckets.discard(bucket)
        self.uncertain_buckets.discard(bucket)
        self.untagged_buckets.discard(bucket)
        self.record_ownership()

    def cleanup_uploads(self, bucket):
        """List every pending upload, including uploads whose creation response was lost."""
        args, uploads, seen = {"Bucket": bucket}, [], set()
        for _ in range(10000):
            page = self.call("list_multipart_uploads", **args)
            uploads.extend(page.get("Uploads", []))
            if not page.get("IsTruncated"):
                return uploads
            token = page.get("NextKeyMarker"), page.get("NextUploadIdMarker")
            check(all(token) and token not in seen, "missing/repeated multipart marker")
            seen.add(token)
            args.update(KeyMarker=token[0], UploadIdMarker=token[1])
        raise AssertionError("multipart pagination did not terminate")

    def remove_test_policy(self, bucket):
        try:
            self.call("delete_bucket_policy", Bucket=bucket)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "NoSuchBucketPolicy":
                raise
        self.policy_buckets.discard(bucket)

    def cleanup_bucket(self, bucket):
        """Purge one owned test bucket, including versions, markers and unfinished uploads."""
        check(bucket in self.planned_buckets and bucket.startswith("tminio-check-" + self.owner_token + "-"),
              "cleanup outside this run's generated bucket names refused")
        check(bucket in self.owned_buckets or bucket in self.uncertain_buckets, "bucket ownership was not recorded")
        # An uncertain create may have reached the server. Only these scoped reads
        # bypass the normal owned-bucket guard; no global discovery or adoption.
        inspect = super().call if bucket in self.uncertain_buckets else self.call
        try:
            tags = inspect("get_bucket_tagging", Bucket=bucket).get("TagSet", [])
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "NoSuchBucket":
                self._forget_bucket(bucket)
                return
            if code not in {"NoSuchTagSet", "NoSuchTagSetError"}:
                raise
            tags = []
        expected = [{"Key": "tminio-check-owner", "Value": self.owner_token}]
        owned_tag = [tag for tag in tags if tag.get("Key") == "tminio-check-owner"] == expected
        if not owned_tag:
            check(not tags and bucket in self.untagged_buckets | self.uncertain_buckets,
                  "bucket ownership tag changed; refusing cleanup")
            # No object writes are allowed before tagging. With a lost create or
            # tag failure, delete only the empty, previously absent UUID bucket.
            versions = inspect("list_object_versions", Bucket=bucket)
            uploads = inspect("list_multipart_uploads", Bucket=bucket)
            check(not any(versions.get(k) for k in ("Versions", "DeleteMarkers", "IsTruncated")) and
                  not any(uploads.get(k) for k in ("Uploads", "IsTruncated")),
                  "untagged bucket is not empty; refusing cleanup")
        # A matching tag or the empty partial-setup proof above authorizes removal.
        self.owned_buckets.add(bucket)
        self.uncertain_buckets.discard(bucket)
        self.record_ownership()
        if bucket in self.policy_buckets:
            self.remove_test_policy(bucket)
        if owned_tag:
            versions = [v for page in self.pages("list_object_versions", Bucket=bucket)
                        for field in ("Versions", "DeleteMarkers") for v in page.get(field, [])]
            uploads = self.cleanup_uploads(bucket)
            for item in [*versions, *uploads]:
                check((bucket, item["Key"]) in self.known_keys, "unknown key found; refusing cleanup")
            # IDs can be unknown after a lost write response or a scanner action.
            # The tag, generated bucket and recorded key intent establish scope.
            if bucket in self.lifecycle_buckets:
                self.call("delete_bucket_lifecycle", Bucket=bucket)
                self.lifecycle_buckets.discard(bucket)
            for upload in uploads:
                try:
                    self.call("abort_multipart_upload", Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != "NoSuchUpload":
                        raise
            for offset in range(0, len(versions), 1000):
                objects = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in versions[offset:offset + 1000]]
                result = self.call("delete_objects", Bucket=bucket, Delete={"Objects": objects, "Quiet": False})
                check(not result.get("Errors"), "cleanup returned per-object errors")
                deleted = [(v["Key"], v.get("VersionId")) for v in result.get("Deleted", [])]
                check(sorted(deleted) == sorted((v["Key"], v["VersionId"]) for v in objects), "incomplete cleanup response")
        self.call("delete_bucket", Bucket=bucket)
        self.error("head_bucket", {"404", "NoSuchBucket", "NotFound"}, status=404, Bucket=bucket)
        self._forget_bucket(bucket)

    def cleanup_remote(self):
        """Attempt every bucket; a failed bucket must not prevent cleanup of the others."""
        errors = {}
        pending = self.owned_buckets | self.uncertain_buckets
        for attempt in range(3):
            retry = set()
            for bucket in sorted(pending):
                try:
                    self.cleanup_bucket(bucket)
                    errors.pop(bucket, None)
                except Exception as error:
                    errors[bucket] = self.redact(f"{type(error).__name__}: {error}")
                    self.report["cleanup_errors"] = dict(errors)
                    # Retry only transport/server failures during teardown. Never
                    # retry functional checks or weaken a failed ownership guard.
                    transient = isinstance(error, (BotoCoreError, OSError))
                    if isinstance(error, ClientError):
                        response = error.response
                        transient = (response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500 or
                                     response.get("Error", {}).get("Code") in {"BucketNotEmpty", "SlowDown"})
                    if transient:
                        retry.add(bucket)
            if not retry or attempt == 2:
                break
            pending = retry
            time.sleep(1)
        self.report["cleanup_errors"] = errors
        check(not errors, "test bucket cleanup failed: " + "; ".join(f"{b}: {e}" for b, e in errors.items()))

    def teardown(self):
        """Remove this run's remote test data and local payloads; keep diagnostic reports."""
        try:
            self.cleanup_remote()
        except BaseException as error:
            self.report["remote_cleanup_error"] = self.redact(f"{type(error).__name__}: {error}")
            raise
        finally:
            # Local cleanup still runs if the service becomes unreachable or the
            # independent cleanup deadline expires while deleting remote data.
            try:
                self.cleanup_data()
            except Exception as error:
                self.report["local_cleanup_error"] = self.redact(f"{type(error).__name__}: {error}")
                raise
            self.report["local_test_data_removed"] = True
        check(not self.data_retained(), "test data remains after teardown")

    def data_retained(self):
        return bool(self.owned_buckets or self.uncertain_buckets or
                    any((self.directory / name).exists() for name in ("data", "payloads", "config", "certs")))


def main(argv=None, *, soak=False):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Credentials: MINIO_ACCESS_KEY and MINIO_SECRET_KEY, or interactive prompts. "
        "Requires admin:ServerInfo, bucket creation/deletion, object/version/tag/lifecycle operations and STS AssumeRole. "
        "Runs all 20 checks on the current service. No old version, saved baseline or binary path is needed."))
    parser.add_argument("endpoint", nargs="?", type=endpoint_url, default=os.getenv("MINIO_ENDPOINT"), help="S3 API URL, for example http://minio.internal:9000")
    parser.add_argument("--work-dir", default=".", help="existing directory for reports and temporary payloads")
    parser.add_argument("--timeout", type=int, default=None, help="overall deadline (default: 1800; soak automatically allows duration + 600 seconds)")
    parser.add_argument("--cleanup-timeout", type=int, default=300, help="separate teardown deadline in seconds (default: 300)")
    parser.add_argument("--region", default="us-east-1", help="server region (default: us-east-1)")
    parser.add_argument("--ca-file", help="PEM CA bundle for an internal HTTPS certificate (TLS verification is always enabled)")
    parser.add_argument("--lifecycle-timeout", type=int, default=600, help="seconds to wait for real lifecycle processing (default: 600)")
    parser.add_argument("--expected-commit", help="optional exact MinIO commit ID required on every server")
    if soak:
        parser.add_argument("--duration", type=int, default=300, help="soak duration in seconds (default: 300)")
        parser.add_argument("--workers", type=int, default=32, help="concurrent soak clients, 1..128 (default: 32)")
    args = parser.parse_args(argv)
    if soak and not 1 <= args.workers <= 128:
        parser.error("--workers must be between 1 and 128")
    if args.timeout is None:
        args.timeout = max(1800, args.duration + 600) if soak else 1800
    if not args.endpoint:
        if not sys.stdin.isatty():
            parser.error("supply the endpoint URL or MINIO_ENDPOINT")
        try:
            args.endpoint = endpoint_url(input("MinIO endpoint [http://localhost:9000]: ").strip() or "http://localhost:9000")
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))
    if soak and (args.duration <= 0 or args.duration + 120 >= args.timeout):
        parser.error("soak duration must be positive and leave at least 120 seconds within --timeout")
    if sys.platform != "linux" or min(args.timeout, args.lifecycle_timeout, args.cleanup_timeout) <= 0:
        parser.error("requires Linux and positive timeout values")
    if args.ca_file and not Path(args.ca_file).is_file():
        parser.error("--ca-file must be an existing PEM file")
    try:
        access, secret = credentials()
    except ValueError as error:
        parser.error(str(error))
    os.umask(0o077)
    suite = None
    previous = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
    def deadline(*_):
        raise Deadline("overall test deadline exceeded")
    def interrupted(*_):
        raise KeyboardInterrupt
    try:
        suite = EndpointSuite(args.endpoint, access, secret, args.work_dir, args.timeout,
                              region=args.region, ca_file=args.ca_file)
        if soak:
            suite.check_names = ["Cluster health and identity", "Endpoint and test buckets", "Bounded soak", "Remove test buckets"]
            from .soak_cases import configure_soak
            configure_soak(suite, args.duration, args.workers)
        suite.expected_commit = args.expected_commit
        suite.lifecycle_timeout = args.lifecycle_timeout
        suite.report["product"] = "MinIO release test for Ubuntu 26.04"
        if not soak:
            suite.report["scope"] = "Current-service checks on one running cluster with automatic test setup and teardown."
        suite.report["lifecycle_timeout_seconds"] = args.lifecycle_timeout
        suite.report["cleanup_timeout_seconds"] = args.cleanup_timeout
        print(f"MinIO release test for Ubuntu 26.04\nEndpoint: {suite.endpoint}\nReports: {suite.directory}\n"
              f"All {len(suite.check_names)} checks include automatic setup and teardown. Allow several GiB of test traffic and storage.", flush=True)
        for signum, handler in ((signal.SIGALRM, deadline), (signal.SIGTERM, interrupted), (signal.SIGINT, interrupted)):
            signal.signal(signum, handler)
        signal.alarm(args.timeout)
        if suite.run_case("Cluster health and identity", suite.cluster_health) and suite.run_case("Endpoint and test buckets", suite.startup):
            cases = [("Object operations", suite.objects), ("Overwrite and batch delete", suite.cycles),
                                    ("Pagination and key names", suite.listing), ("Multipart uploads", suite.multipart),
                                    ("Access restrictions", suite.access_control), ("Presigned URLs", suite.presigned),
                                    ("Conditional requests", suite.conditional_requests), ("Bucket configuration", suite.bucket_configuration),
                                    ("Bucket policy", suite.bucket_policy), ("Encryption behavior", suite.encryption_behavior),
                                    ("Object lock configuration", suite.object_lock_configuration),
                                    ("Versioning", suite.versions), ("Null versions and restoration", suite.null_and_restore),
                                    ("Concurrent operations", suite.concurrent), ("Lifecycle configuration", suite.lifecycle),
                                    ("Lifecycle actions", suite.lifecycle_actions),
                                    ("Final data integrity", suite.final_integrity)]
            if soak:
                from .soak_cases import bounded_soak
                cases = [("Bounded soak", lambda: bounded_soak(suite))]
            for title, function in cases:
                suite.run_case(title, function)
    except (KeyboardInterrupt, Deadline):
        if suite is None:
            print("Setup interrupted before remote test data was created.", file=sys.stderr)
            return 1
        if not suite.report["results"] or suite.report["results"][-1]["status"] == "PASS":
            suite.report["results"].append({"name": "Run interrupted", "status": "FAIL", "seconds": 0,
                                          "actual": "Interrupted or deadline exceeded"})
    except Exception as error:
        if not suite:
            message = str(error).replace(secret, "[REDACTED]").replace(access, "[REDACTED]")
            print(f"Cannot start endpoint check: {type(error).__name__}: {message}", file=sys.stderr)
            return 2
        suite.report["results"].append({"name": "Runner", "status": "FAIL", "seconds": 0, "actual": suite.redact(error)})
    finally:
        signal.alarm(0)
        try:
            if suite:
                # Give teardown its own budget even after a test deadline. A
                # second Ctrl-C cannot cancel this bounded cleanup accidentally.
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                def cleanup_deadline(*_):
                    raise Deadline("teardown deadline exceeded")
                signal.signal(signal.SIGALRM, cleanup_deadline)
                signal.alarm(args.cleanup_timeout)
                try:
                    suite.run_case("Remove test buckets", suite.teardown)
                except (KeyboardInterrupt, Deadline):
                    pass  # run_case recorded FAIL; still close clients and report.
                finally:
                    signal.alarm(0)
                    for client in suite.clients:
                        try:
                            client.close()
                        except Exception as error:
                            suite.report["results"].append({"name": "Close client", "status": "FAIL", "seconds": 0,
                                                            "actual": suite.redact(error)})
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    if suite.data_retained():
        print("Cleanup incomplete. Remaining test bucket names and cleanup errors are in results.json.", flush=True)
    return suite.finish()


if __name__ == "__main__":
    raise SystemExit(main())
