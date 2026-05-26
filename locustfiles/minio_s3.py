from __future__ import annotations

import base64
import datetime as dt
import hashlib
import http.client
import json
import os
import time
import urllib.parse
import uuid
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from locust import User, between, task

ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "")
SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "")
ENDPOINT = os.getenv("MINIO_ENDPOINT", "").rstrip("/")
BUCKET_PREFIX = os.getenv("MINIO_TEST_BUCKET_PREFIX", "minio-test")
CLEANUP = os.getenv("MINIO_TEST_CLEANUP", "1") != "0"
LONG_OBJECT_LIMIT = int(os.getenv("MINIO_LONG_OBJECT_LIMIT", "500"))
S3_CLIENT_CONFIG = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    retries={"max_attempts": 4, "mode": "standard"},
)


class TestFailure(RuntimeError):
    pass


def _bucket(label: str) -> str:
    prefix = BUCKET_PREFIX.strip("-").lower() or "minio-test"
    return f"{prefix}-{label}-{uuid.uuid4().hex[:18]}"


def _tags(values: dict[str, str]) -> str:
    return urllib.parse.urlencode(values)


def _validated_http_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise TestFailure(f"URL must be absolute HTTP(S): {url}")
    if parsed.username or parsed.password:
        raise TestFailure("URL user-info is not allowed")
    return parsed


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _origin(parsed: urllib.parse.ParseResult) -> tuple[str, str, int]:
    hostname = parsed.hostname
    if hostname is None:
        raise TestFailure("URL hostname is required")
    return parsed.scheme, hostname.lower(), parsed.port or _default_port(parsed.scheme)


def _request_target(parsed: urllib.parse.ParseResult) -> str:
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return target


class S3Mixin:
    abstract = True
    wait_time = between(0.1, 0.5)

    def endpoint(self) -> str:
        return str(getattr(self, "_endpoint", ENDPOINT or str(getattr(self, "host", "")))).rstrip("/")

    def on_start(self) -> None:
        raw_endpoint = (ENDPOINT or str(getattr(self, "host", ""))).rstrip("/")
        if not raw_endpoint or not ACCESS_KEY or not SECRET_KEY:
            raise TestFailure(
                "MINIO_ENDPOINT or Locust --host, plus MINIO_ACCESS_KEY and MINIO_SECRET_KEY, must be set"
            )
        parsed = _validated_http_url(raw_endpoint)
        self._endpoint = raw_endpoint
        self._endpoint_origin = _origin(parsed)
        self.client = self.new_client()

    def new_client(self) -> Any:
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint(),
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            config=S3_CLIENT_CONFIG,
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

    def record_optional_client_error(
        self, request_type: str, name: str, func: Callable[[], Any], expected: set[str]
    ) -> Any:
        def _optional() -> Any:
            try:
                return func()
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                status = str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
                if code in expected or status in expected:
                    return exc
                raise

        return self.record(request_type, name, _optional)

    def http_request(self, url: str, method: str = "GET", data: bytes | None = None, timeout: int = 30) -> tuple[int, bytes]:
        parsed = _validated_http_url(url)
        if _origin(parsed) != self._endpoint_origin:
            raise TestFailure("refusing cross-origin HTTP request")
        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(parsed.hostname, parsed.port or _default_port(parsed.scheme), timeout=timeout)
        headers = {"Content-Length": str(len(data))} if data is not None else {}
        try:
            conn.request(method, _request_target(parsed), body=data, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def read_url(self, url: str, method: str = "GET", data: bytes | None = None) -> bytes:
        def _read() -> bytes:
            status, body = self.http_request(url, method=method, data=data, timeout=30)
            if status >= 400:
                raise TestFailure(f"HTTP {method} returned status {status}")
            return body

        return self.record("HTTP", f"presigned-{method}", _read)

    def cleanup_warning(self, bucket: str, action: str, exc: Exception) -> None:
        print(f"cleanup warning for {bucket} during {action}: {type(exc).__name__}: {exc}", flush=True)

    def handle_cleanup_error(self, bucket: str, action: str, exc: Exception, expected: set[str]) -> None:
        if isinstance(exc, ClientError):
            error = exc.response.get("Error", {})
            metadata = exc.response.get("ResponseMetadata", {})
            code = str(error.get("Code", ""))
            status = str(metadata.get("HTTPStatusCode", ""))
            if code in expected or status in expected:
                return
        self.cleanup_warning(bucket, action, exc)

    def cleanup_bucket(self, bucket: str) -> None:
        if not CLEANUP:
            return
        c = self.client
        try:
            uploads = c.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
            for upload in uploads:
                c.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
        except ClientError as exc:
            self.handle_cleanup_error(bucket, "abort multipart uploads", exc, {"NoSuchBucket", "404"})
        except Exception as exc:
            self.cleanup_warning(bucket, "abort multipart uploads", exc)
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
        except ClientError as exc:
            self.handle_cleanup_error(bucket, "delete object versions", exc, {"NoSuchBucket", "404"})
        except Exception as exc:
            self.cleanup_warning(bucket, "delete object versions", exc)
        try:
            paginator = c.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket):
                objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                for index in range(0, len(objects), 1000):
                    c.delete_objects(Bucket=bucket, Delete={"Objects": objects[index : index + 1000]})
        except ClientError as exc:
            self.handle_cleanup_error(bucket, "delete objects", exc, {"NoSuchBucket", "404"})
        except Exception as exc:
            self.cleanup_warning(bucket, "delete objects", exc)
        cleanup_calls = (
            (c.delete_bucket_policy, {"NoSuchBucket", "NoSuchBucketPolicy", "404"}),
            (c.delete_bucket_lifecycle, {"NoSuchBucket", "NoSuchLifecycleConfiguration", "404"}),
            (c.delete_bucket_cors, {"NoSuchBucket", "NoSuchCORSConfiguration", "NotImplemented", "404", "501"}),
            (c.delete_bucket_tagging, {"NoSuchBucket", "NoSuchTagSet", "404"}),
        )
        for cleanup, expected in cleanup_calls:
            try:
                cleanup(Bucket=bucket)
            except ClientError as exc:
                self.handle_cleanup_error(bucket, cleanup.__name__, exc, expected)
            except Exception as exc:
                self.cleanup_warning(bucket, cleanup.__name__, exc)
        try:
            c.delete_bucket(Bucket=bucket)
        except ClientError as exc:
            self.handle_cleanup_error(bucket, "delete bucket", exc, {"NoSuchBucket", "404"})
        except Exception as exc:
            self.cleanup_warning(bucket, "delete bucket", exc)

    def create_bucket(self, label: str, **kwargs: Any) -> str:
        bucket = _bucket(label)
        self.record("S3", "CreateBucket", lambda: self.client.create_bucket(Bucket=bucket, **kwargs))
        return bucket

    def health_checks(self) -> None:
        for path in ("/minio/health/live", "/minio/health/ready"):
            url = f"{self.endpoint()}{path}"

            def _read() -> int:
                status, _ = self.http_request(url, timeout=10)
                return status

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
            public_body = self.read_url(f"{self.endpoint()}/{bucket}/{key}")
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
            cors_response = self.record_optional_client_error(
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
                {"501", "NotImplemented"},
            )
            if isinstance(cors_response, ClientError):
                self.check("bucket-cors-not-implemented", True, "CORS API reported NotImplemented")
            else:
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
            body = b"sample payload with sse-c"

            def put_ssec() -> Any:
                return self.client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    SSECustomerAlgorithm="AES256",
                    SSECustomerKey=customer_key,
                )

            if self.endpoint().lower().startswith("http://"):
                rejected = self.record_optional_client_error(
                    "S3",
                    "PutObject.sse-c",
                    put_ssec,
                    {"400", "InvalidRequest"},
                )
                self.check(
                    "sse-c-requires-https",
                    isinstance(rejected, ClientError),
                    "SSE-C unexpectedly succeeded over HTTP",
                )
                return

            self.record(
                "S3",
                "PutObject.sse-c",
                put_ssec,
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
        except Exception as exc:
            print(f"smoke workload failed: {type(exc).__name__}: {exc}", flush=True)
            raise
        finally:
            runner = self.environment.runner
            if runner:
                runner.quit()
