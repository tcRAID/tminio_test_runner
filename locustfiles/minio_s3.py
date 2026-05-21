from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import random
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from locust import User, between, task


ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
REGION = os.getenv("MINIO_REGION", "us-east-1")
VERIFY_TLS = os.getenv("MINIO_VERIFY_TLS", "1") != "0"
BUCKET_PREFIX = os.getenv("MINIO_TEST_BUCKET_PREFIX", "minio-test")
CLEANUP = os.getenv("MINIO_TEST_CLEANUP", "1") != "0"
LONG_OBJECT_LIMIT = int(os.getenv("MINIO_LONG_OBJECT_LIMIT", "500"))


class TestFailure(RuntimeError):
    pass


def _bucket(label: str) -> str:
    prefix = BUCKET_PREFIX.strip("-").lower() or "minio-test"
    return f"{prefix}-{label}-{uuid.uuid4().hex[:18]}"


def _tags(values: dict[str, str]) -> str:
    return urllib.parse.urlencode(values)


class S3Mixin:
    abstract = True
    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        if not ACCESS_KEY or not SECRET_KEY:
            raise TestFailure("MINIO_ACCESS_KEY and MINIO_SECRET_KEY must be set")
        self.client = self.new_client()

    def new_client(self) -> Any:
        return boto3.client(
            "s3",
            endpoint_url=self.host,
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            region_name=REGION,
            verify=VERIFY_TLS,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 4, "mode": "standard"},
            ),
        )

    def record(
        self,
        request_type: str,
        name: str,
        func: Callable[[], Any],
        response_length: int = 0,
    ) -> Any:
        started = time.perf_counter()
        exception = None
        try:
            return func()
        except Exception as exc:
            exception = exc
            raise
        finally:
            self.environment.events.request.fire(
                request_type=request_type,
                name=name,
                response_time=(time.perf_counter() - started) * 1000,
                response_length=response_length,
                exception=exception,
            )

    def check(self, name: str, condition: bool, message: str) -> None:
        def _check() -> None:
            if not condition:
                raise TestFailure(message)

        self.record("CHECK", name, _check)

    def expect_client_error(self, name: str, func: Callable[[], Any], expected: set[str]) -> str:
        def _expect() -> str:
            try:
                func()
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                status = str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
                if code in expected or status in expected:
                    return code or status
                raise
            raise TestFailure(f"expected client error {expected}, but operation succeeded")

        return self.record("CHECK", name, _expect)

    def read_url(self, url: str, method: str = "GET", data: bytes | None = None) -> bytes:
        def _read() -> bytes:
            req = urllib.request.Request(url, data=data, method=method)
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()

        return self.record("HTTP", f"presigned-{method}", _read)

    def cleanup_bucket(self, bucket: str) -> None:
        if not CLEANUP:
            return
        c = self.client
        try:
            uploads = c.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
            for upload in uploads:
                c.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
        except Exception:
            pass
        try:
            paginator = c.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=bucket):
                objects = []
                for item in page.get("Versions", []):
                    objects.append({"Key": item["Key"], "VersionId": item["VersionId"]})
                for item in page.get("DeleteMarkers", []):
                    objects.append({"Key": item["Key"], "VersionId": item["VersionId"]})
                for index in range(0, len(objects), 1000):
                    c.delete_objects(Bucket=bucket, Delete={"Objects": objects[index : index + 1000]})
        except Exception:
            pass
        try:
            paginator = c.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket):
                objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                for index in range(0, len(objects), 1000):
                    c.delete_objects(Bucket=bucket, Delete={"Objects": objects[index : index + 1000]})
        except Exception:
            pass
        for cleanup in (
            c.delete_bucket_policy,
            c.delete_bucket_lifecycle,
            c.delete_bucket_cors,
            c.delete_bucket_tagging,
        ):
            try:
                cleanup(Bucket=bucket)
            except Exception:
                pass
        try:
            c.delete_bucket(Bucket=bucket)
        except Exception:
            pass

    def create_bucket(self, label: str, **kwargs: Any) -> str:
        bucket = _bucket(label)
        self.record("S3", "CreateBucket", lambda: self.client.create_bucket(Bucket=bucket, **kwargs))
        return bucket

    def health_checks(self) -> None:
        for path in ("/minio/health/live", "/minio/health/ready"):
            url = f"{self.host.rstrip('/')}{path}"

            def _read() -> int:
                with urllib.request.urlopen(url, timeout=10) as response:
                    return response.status

            status = self.record("HTTP", path, _read)
            self.check(f"{path}-status", status == 200, f"{path} returned {status}")

    def basic_bucket_object_crud(self) -> None:
        bucket = self.create_bucket("basic")
        try:
            self.record("S3", "HeadBucket", lambda: self.client.head_bucket(Bucket=bucket))
            body = b"hello from minio locust smoke test\n"
            sha = hashlib.sha256(body).hexdigest()
            key = "docs/readme.txt"
            self.record(
                "S3",
                "PutObject",
                lambda: self.client.put_object(Bucket=bucket, Key=key, Body=body, Metadata={"sha256": sha}),
                len(body),
            )
            obj = self.record("S3", "GetObject", lambda: self.client.get_object(Bucket=bucket, Key=key))
            self.check("basic-get-body", obj["Body"].read() == body, "downloaded object body mismatch")
            head = self.record("S3", "HeadObject", lambda: self.client.head_object(Bucket=bucket, Key=key))
            self.check("basic-head-metadata", head["Metadata"].get("sha256") == sha, "metadata mismatch")

            for index in range(10):
                item = f"item {index}\n".encode()
                self.record(
                    "S3",
                    "PutObject.batch",
                    lambda item=item, index=index: self.client.put_object(
                        Bucket=bucket, Key=f"prefix/item-{index:02d}.txt", Body=item
                    ),
                    len(item),
                )
            listed = self.record(
                "S3", "ListObjectsV2", lambda: self.client.list_objects_v2(Bucket=bucket, Prefix="prefix/")
            )
            self.check("basic-list-count", len(listed.get("Contents", [])) == 10, "prefix listing count mismatch")

            copy_key = "copies/readme-copy.txt"
            self.record(
                "S3",
                "CopyObject",
                lambda: self.client.copy_object(
                    Bucket=bucket, Key=copy_key, CopySource={"Bucket": bucket, "Key": key}
                ),
            )
            copied = self.record("S3", "GetObject.copy", lambda: self.client.get_object(Bucket=bucket, Key=copy_key))
            self.check("basic-copy-body", copied["Body"].read() == body, "copied object body mismatch")
        finally:
            self.cleanup_bucket(bucket)

    def metadata_tags_range_conditionals(self) -> None:
        bucket = self.create_bucket("meta")
        try:
            key = "objects/ranged.bin"
            body = b"0123456789abcdefghijklmnopqrstuvwxyz"
            self.record(
                "S3",
                "PutObject.tags",
                lambda: self.client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    Metadata={"case": "metadata-range"},
                    Tagging=_tags({"project": "minio", "kind": "smoke"}),
                ),
                len(body),
            )
            ranged = self.record(
                "S3", "GetObject.range", lambda: self.client.get_object(Bucket=bucket, Key=key, Range="bytes=5-10")
            )
            self.check("range-get-bytes", ranged["Body"].read() == b"56789a", "range GET returned wrong bytes")
            tagset = self.record(
                "S3", "GetObjectTagging", lambda: self.client.get_object_tagging(Bucket=bucket, Key=key)
            )["TagSet"]
            self.check("object-tagging", {"Key": "project", "Value": "minio"} in tagset, "object tag missing")
            head = self.record("S3", "HeadObject.conditional", lambda: self.client.head_object(Bucket=bucket, Key=key))
            etag = head["ETag"]
            matched = self.record(
                "S3", "GetObject.if-match", lambda: self.client.get_object(Bucket=bucket, Key=key, IfMatch=etag)
            )
            self.check("if-match-body", matched["Body"].read() == body, "If-Match body mismatch")
            self.expect_client_error(
                "if-none-match-not-modified",
                lambda: self.client.get_object(Bucket=bucket, Key=key, IfNoneMatch=etag),
                {"304", "NotModified"},
            )
            self.record(
                "S3",
                "CopyObject.if-match",
                lambda: self.client.copy_object(
                    Bucket=bucket,
                    Key="objects/conditional-copy.bin",
                    CopySource={"Bucket": bucket, "Key": key},
                    CopySourceIfMatch=etag,
                ),
            )
        finally:
            self.cleanup_bucket(bucket)

    def multipart_upload(self) -> None:
        bucket = self.create_bucket("multipart")
        try:
            key = "large/multipart.bin"
            part1 = b"a" * (6 * 1024 * 1024)
            part2 = b"b" * (1024 * 1024)
            expected = hashlib.sha256(part1 + part2).hexdigest()
            upload = self.record(
                "S3", "CreateMultipartUpload", lambda: self.client.create_multipart_upload(Bucket=bucket, Key=key)
            )
            parts = []
            for number, body in ((1, part1), (2, part2)):
                response = self.record(
                    "S3",
                    "UploadPart",
                    lambda number=number, body=body: self.client.upload_part(
                        Bucket=bucket, Key=key, UploadId=upload["UploadId"], PartNumber=number, Body=body
                    ),
                    len(body),
                )
                parts.append({"ETag": response["ETag"], "PartNumber": number})
            self.record(
                "S3",
                "CompleteMultipartUpload",
                lambda: self.client.complete_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload["UploadId"],
                    MultipartUpload={"Parts": parts},
                ),
            )
            got = self.record("S3", "GetObject.multipart", lambda: self.client.get_object(Bucket=bucket, Key=key))
            self.check(
                "multipart-sha256",
                hashlib.sha256(got["Body"].read()).hexdigest() == expected,
                "multipart payload hash mismatch",
            )

            abort = self.record(
                "S3",
                "CreateMultipartUpload.abort",
                lambda: self.client.create_multipart_upload(Bucket=bucket, Key="large/abort.bin"),
            )
            self.record(
                "S3",
                "UploadPart.abort",
                lambda: self.client.upload_part(
                    Bucket=bucket,
                    Key="large/abort.bin",
                    UploadId=abort["UploadId"],
                    PartNumber=1,
                    Body=part1,
                ),
                len(part1),
            )
            self.record(
                "S3",
                "AbortMultipartUpload",
                lambda: self.client.abort_multipart_upload(
                    Bucket=bucket, Key="large/abort.bin", UploadId=abort["UploadId"]
                ),
            )
        finally:
            self.cleanup_bucket(bucket)

    def versioning_delete_markers(self) -> None:
        bucket = self.create_bucket("versioning")
        try:
            key = "versioned/object.txt"
            self.record(
                "S3",
                "PutBucketVersioning",
                lambda: self.client.put_bucket_versioning(
                    Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
                ),
            )
            time.sleep(1)
            v1 = self.record(
                "S3", "PutObject.versioned", lambda: self.client.put_object(Bucket=bucket, Key=key, Body=b"version-one")
            )["VersionId"]
            self.record(
                "S3", "PutObject.versioned", lambda: self.client.put_object(Bucket=bucket, Key=key, Body=b"version-two")
            )
            old = self.record(
                "S3", "GetObject.version-id", lambda: self.client.get_object(Bucket=bucket, Key=key, VersionId=v1)
            )
            self.check("versioned-get-body", old["Body"].read() == b"version-one", "versioned GET body mismatch")
            delete = self.record("S3", "DeleteObject.versioned", lambda: self.client.delete_object(Bucket=bucket, Key=key))
            self.check("delete-marker-created", delete.get("DeleteMarker") is True, "delete marker was not created")
            versions = self.record(
                "S3", "ListObjectVersions", lambda: self.client.list_object_versions(Bucket=bucket, Prefix=key)
            )
            self.check("version-count", len(versions.get("Versions", [])) >= 2, "expected at least two versions")
            self.check("delete-marker-count", len(versions.get("DeleteMarkers", [])) >= 1, "expected delete marker")
        finally:
            self.cleanup_bucket(bucket)

    def presigned_urls_and_policy(self) -> None:
        bucket = self.create_bucket("policy")
        try:
            key = "public/hello.txt"
            body = b"hello through presigned url"
            self.record("S3", "PutObject.public", lambda: self.client.put_object(Bucket=bucket, Key=key, Body=body))
            get_url = self.record(
                "S3",
                "GeneratePresignedGet",
                lambda: self.client.generate_presigned_url(
                    "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300
                ),
            )
            self.check("presigned-get-body", self.read_url(get_url) == body, "presigned GET body mismatch")

            put_key = "uploads/presigned-put.txt"
            put_body = b"uploaded with presigned PUT"
            put_url = self.record(
                "S3",
                "GeneratePresignedPut",
                lambda: self.client.generate_presigned_url(
                    "put_object", Params={"Bucket": bucket, "Key": put_key}, ExpiresIn=300
                ),
            )
            self.read_url(put_url, method="PUT", data=put_body)
            stored = self.record("S3", "GetObject.presigned-put", lambda: self.client.get_object(Bucket=bucket, Key=put_key))
            self.check("presigned-put-body", stored["Body"].read() == put_body, "presigned PUT body mismatch")

            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{bucket}/public/*"],
                    }
                ],
            }
            self.record(
                "S3", "PutBucketPolicy", lambda: self.client.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
            )
            public_body = self.read_url(f"{self.host.rstrip('/')}/{bucket}/{key}")
            self.check("bucket-policy-public-read", public_body == body, "anonymous policy GET mismatch")
        finally:
            self.cleanup_bucket(bucket)

    def bucket_configuration(self) -> None:
        bucket = self.create_bucket("config")
        try:
            self.record(
                "S3",
                "PutBucketTagging",
                lambda: self.client.put_bucket_tagging(
                    Bucket=bucket,
                    Tagging={
                        "TagSet": [
                            {"Key": "suite", "Value": "functional"},
                            {"Key": "mode", "Value": "smoke"},
                        ]
                    },
                ),
            )
            tags = self.record("S3", "GetBucketTagging", lambda: self.client.get_bucket_tagging(Bucket=bucket))["TagSet"]
            self.check("bucket-tagging", {"Key": "suite", "Value": "functional"} in tags, "bucket tag missing")
            self.record(
                "S3",
                "PutBucketCors",
                lambda: self.client.put_bucket_cors(
                    Bucket=bucket,
                    CORSConfiguration={
                        "CORSRules": [
                            {
                                "AllowedMethods": ["GET", "PUT"],
                                "AllowedOrigins": ["https://example.com"],
                                "AllowedHeaders": ["*"],
                                "ExposeHeaders": ["ETag"],
                                "MaxAgeSeconds": 300,
                            }
                        ]
                    },
                ),
            )
            cors = self.record("S3", "GetBucketCors", lambda: self.client.get_bucket_cors(Bucket=bucket))
            self.check("bucket-cors", len(cors.get("CORSRules", [])) == 1, "CORS rule missing")
            self.record(
                "S3",
                "PutBucketLifecycle",
                lambda: self.client.put_bucket_lifecycle_configuration(
                    Bucket=bucket,
                    LifecycleConfiguration={
                        "Rules": [
                            {
                                "ID": "expire-tmp",
                                "Status": "Enabled",
                                "Filter": {"Prefix": "tmp/"},
                                "Expiration": {"Days": 1},
                                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                            }
                        ]
                    },
                ),
            )
            lifecycle = self.record(
                "S3", "GetBucketLifecycle", lambda: self.client.get_bucket_lifecycle_configuration(Bucket=bucket)
            )
            self.check("bucket-lifecycle", len(lifecycle.get("Rules", [])) == 1, "lifecycle rule missing")
        finally:
            self.cleanup_bucket(bucket)

    def ssec(self) -> None:
        bucket = self.create_bucket("ssec")
        try:
            key = "encrypted/customer-key.bin"
            customer_key = base64.b64encode(os.urandom(32)).decode("ascii")
            body = b"secret payload with sse-c"
            self.record(
                "S3",
                "PutObject.sse-c",
                lambda: self.client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    SSECustomerAlgorithm="AES256",
                    SSECustomerKey=customer_key,
                ),
                len(body),
            )
            got = self.record(
                "S3",
                "GetObject.sse-c",
                lambda: self.client.get_object(
                    Bucket=bucket,
                    Key=key,
                    SSECustomerAlgorithm="AES256",
                    SSECustomerKey=customer_key,
                ),
            )
            self.check("sse-c-body", got["Body"].read() == body, "SSE-C object body mismatch")
            self.expect_client_error(
                "sse-c-requires-key",
                lambda: self.client.head_object(Bucket=bucket, Key=key),
                {"400", "InvalidRequest"},
            )
        finally:
            self.cleanup_bucket(bucket)

    def object_lock_governance(self) -> None:
        bucket = self.create_bucket("lock", ObjectLockEnabledForBucket=True)
        try:
            key = "locked/object.txt"
            retain_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
            put = self.record(
                "S3",
                "PutObject.object-lock",
                lambda: self.client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=b"locked",
                    ObjectLockMode="GOVERNANCE",
                    ObjectLockRetainUntilDate=retain_until,
                ),
            )
            version_id = put.get("VersionId")
            retention = self.record(
                "S3",
                "GetObjectRetention",
                lambda: self.client.get_object_retention(Bucket=bucket, Key=key, VersionId=version_id),
            )
            self.check("object-lock-mode", retention["Retention"]["Mode"] == "GOVERNANCE", "retention mode mismatch")
            self.record(
                "S3",
                "PutObjectLegalHold.on",
                lambda: self.client.put_object_legal_hold(
                    Bucket=bucket,
                    Key=key,
                    VersionId=version_id,
                    LegalHold={"Status": "ON"},
                ),
            )
            hold = self.record(
                "S3",
                "GetObjectLegalHold",
                lambda: self.client.get_object_legal_hold(Bucket=bucket, Key=key, VersionId=version_id),
            )
            self.check("legal-hold-on", hold["LegalHold"]["Status"] == "ON", "legal hold was not enabled")
            self.record(
                "S3",
                "PutObjectLegalHold.off",
                lambda: self.client.put_object_legal_hold(
                    Bucket=bucket,
                    Key=key,
                    VersionId=version_id,
                    LegalHold={"Status": "OFF"},
                ),
            )
            self.record(
                "S3",
                "DeleteObject.bypass-governance",
                lambda: self.client.delete_object(
                    Bucket=bucket,
                    Key=key,
                    VersionId=version_id,
                    BypassGovernanceRetention=True,
                ),
            )
        finally:
            self.cleanup_bucket(bucket)


class MinioSmokeUser(S3Mixin, User):
    abstract = False
    wait_time = between(0.1, 0.2)

    @task
    def run_smoke_once(self) -> None:
        try:
            self.health_checks()
            self.basic_bucket_object_crud()
            self.metadata_tags_range_conditionals()
            self.multipart_upload()
            self.versioning_delete_markers()
            self.presigned_urls_and_policy()
            self.bucket_configuration()
            self.ssec()
            self.object_lock_governance()
        except Exception:
            print(traceback.format_exc(), flush=True)
            raise
        finally:
            runner = self.environment.runner
            if runner:
                runner.quit()


class MinioLongUser(S3Mixin, User):
    abstract = False
    wait_time = between(0.05, 0.5)

    def on_start(self) -> None:
        super().on_start()
        self.bucket = self.create_bucket("long")
        self.record(
            "S3",
            "PutBucketVersioning.long",
            lambda: self.client.put_bucket_versioning(
                Bucket=self.bucket,
                VersioningConfiguration={"Status": "Enabled"},
            ),
        )
        self.known: dict[str, str] = {}
        self.stats: Counter[str] = Counter()
        self.lock = threading.Lock()

    def on_stop(self) -> None:
        self.cleanup_bucket(getattr(self, "bucket", ""))

    def remember(self, key: str, sha: str) -> None:
        with self.lock:
            self.known[key] = sha
            if len(self.known) > LONG_OBJECT_LIMIT:
                for victim in list(self.known.keys())[: max(1, LONG_OBJECT_LIMIT // 10)]:
                    self.known.pop(victim, None)

    def choose_key(self) -> str | None:
        with self.lock:
            if not self.known:
                return None
            return random.choice(list(self.known.keys()))

    @task(35)
    def put_object(self) -> None:
        size = random.choice([0, 1, 128, 4096, 65536, 1048576])
        body = os.urandom(size)
        sha = hashlib.sha256(body).hexdigest()
        key = f"load/{uuid.uuid4().hex}.bin"
        self.record(
            "S3",
            "PutObject.long",
            lambda: self.client.put_object(Bucket=self.bucket, Key=key, Body=body, Metadata={"sha256": sha}),
            len(body),
        )
        self.remember(key, sha)
        self.stats["put"] += 1

    @task(25)
    def get_object(self) -> None:
        key = self.choose_key()
        if not key:
            return
        obj = self.record("S3", "GetObject.long", lambda: self.client.get_object(Bucket=self.bucket, Key=key))
        got = obj["Body"].read()
        expected = obj.get("Metadata", {}).get("sha256")
        if expected:
            self.check(
                "long-get-sha256",
                hashlib.sha256(got).hexdigest() == expected,
                f"hash mismatch for {key}",
            )
        self.stats["get"] += 1

    @task(10)
    def head_object(self) -> None:
        key = self.choose_key()
        if key:
            self.record("S3", "HeadObject.long", lambda: self.client.head_object(Bucket=self.bucket, Key=key))
            self.stats["head"] += 1

    @task(10)
    def list_objects(self) -> None:
        self.record(
            "S3",
            "ListObjectsV2.long",
            lambda: self.client.list_objects_v2(Bucket=self.bucket, Prefix="load/", MaxKeys=100),
        )
        self.stats["list"] += 1

    @task(8)
    def copy_object(self) -> None:
        key = self.choose_key()
        if not key:
            return
        copy_key = f"copy/{uuid.uuid4().hex}.bin"
        self.record(
            "S3",
            "CopyObject.long",
            lambda: self.client.copy_object(
                Bucket=self.bucket, Key=copy_key, CopySource={"Bucket": self.bucket, "Key": key}
            ),
        )
        head = self.record(
            "S3", "HeadObject.long-copy", lambda: self.client.head_object(Bucket=self.bucket, Key=copy_key)
        )
        sha = head.get("Metadata", {}).get("sha256")
        if sha:
            self.remember(copy_key, sha)
        self.stats["copy"] += 1

    @task(5)
    def delete_object(self) -> None:
        key = self.choose_key()
        if not key:
            return
        self.record("S3", "DeleteObject.long", lambda: self.client.delete_object(Bucket=self.bucket, Key=key))
        with self.lock:
            self.known.pop(key, None)
        self.stats["delete"] += 1

    @task(5)
    def multipart_upload(self) -> None:
        key = f"multipart/{uuid.uuid4().hex}.bin"
        part1 = os.urandom(5 * 1024 * 1024)
        part2 = os.urandom(random.choice([1, 1024, 1024 * 1024]))
        upload = self.record(
            "S3",
            "CreateMultipartUpload.long",
            lambda: self.client.create_multipart_upload(Bucket=self.bucket, Key=key),
        )
        parts = []
        for number, body in ((1, part1), (2, part2)):
            response = self.record(
                "S3",
                "UploadPart.long",
                lambda number=number, body=body: self.client.upload_part(
                    Bucket=self.bucket,
                    Key=key,
                    UploadId=upload["UploadId"],
                    PartNumber=number,
                    Body=body,
                ),
                len(body),
            )
            parts.append({"ETag": response["ETag"], "PartNumber": number})
        self.record(
            "S3",
            "CompleteMultipartUpload.long",
            lambda: self.client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload["UploadId"],
                MultipartUpload={"Parts": parts},
            ),
        )
        self.remember(key, hashlib.sha256(part1 + part2).hexdigest())
        self.stats["multipart"] += 1

    @task(2)
    def bucket_tagging(self) -> None:
        self.record(
            "S3",
            "PutBucketTagging.long",
            lambda: self.client.put_bucket_tagging(
                Bucket=self.bucket,
                Tagging={"TagSet": [{"Key": "last_update", "Value": uuid.uuid4().hex[:12]}]},
            ),
        )
        self.record("S3", "GetBucketTagging.long", lambda: self.client.get_bucket_tagging(Bucket=self.bucket))
        self.stats["tagging"] += 1
