from __future__ import annotations

import hashlib
import os
import pathlib
import random
import sys
import threading
import uuid
from collections import Counter

from locust import User, between, task

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from locustfiles.minio_s3 import LONG_OBJECT_LIMIT, S3Mixin


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
