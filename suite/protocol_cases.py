"""Endpoint protocol coverage brought over from the original Locust runner."""
import hashlib
import json
import os

from botocore.exceptions import ClientError

from binary_check import check


class ProtocolCases:
    def conditional_requests(self):
        """GET/HEAD/copy enforce ETag preconditions; ranges return exactly the requested bytes."""
        bucket, key = self.bucket, "conditional/source"
        data = b"conditional request fixture\n" * 256
        etag = self.call("put_object", Bucket=bucket, Key=key, Body=data, ContentType="application/octet-stream")["ETag"]
        expected = {"bucket": bucket, "key": key, "version": None, "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(), "metadata": {}, "content_type": "application/octet-stream"}
        self.remember(expected)
        args = dict(Bucket=bucket, Key=key)
        for method in ("get_object", "head_object"):
            result = self.call(method, **args, IfMatch=etag)
            if "Body" in result:
                with result["Body"] as body:
                    check(body.read() == data, "conditional GET returned different bytes")
            check(result["ContentLength"] == len(data), "conditional response length differs")
            self.error(method, {"304", "NotModified"}, status=304, **args, IfNoneMatch=etag)
            self.error(method, {"412", "PreconditionFailed"}, status=412, **args, IfMatch='"wrong"')
        copied = dict(Bucket=bucket, Key="conditional/copy", CopySource=args)
        self.call("copy_object", **copied, CopySourceIfMatch=etag)
        self.remember({**expected, "key": copied["Key"]})
        result = self.call("get_object", Bucket=bucket, Key=copied["Key"])
        with result["Body"] as body:
            check(body.read() == data, "conditional copy changed bytes")
        self.error("copy_object", {"PreconditionFailed"}, status=412,
                   **{**copied, "Key": "conditional/rejected"}, CopySourceIfMatch='"wrong"')
        self.missing(bucket, "conditional/rejected")
        result = self.call("get_object", **args, Range="bytes=13-97")
        with result["Body"] as body:
            check(body.read() == data[13:98], "range returned wrong bytes")
        check(result["ResponseMetadata"]["HTTPStatusCode"] == 206 and
              result["ContentRange"] == f"bytes 13-97/{len(data)}", "range response headers differ")
        self.error("get_object", {"InvalidRange"}, status=416, **args, Range=f"bytes={len(data)}-")

    def bucket_configuration(self):
        """Location, bucket tags and CORS configuration preserve their values and ownership."""
        bucket = self.bucket
        location = self.call("get_bucket_location", Bucket=bucket).get("LocationConstraint") or "us-east-1"
        check(location == self.region, "bucket location differs from configured region")
        owner = {"Key": "tminio-check-owner", "Value": self.owner_token}
        tags = [owner, {"Key": "release-test", "Value": "ubuntu26.04"}]
        try:
            self.call("put_bucket_tagging", Bucket=bucket, Tagging={"TagSet": tags})
            actual = self.call("get_bucket_tagging", Bucket=bucket)["TagSet"]
            check(sorted(actual, key=lambda v: v["Key"]) == sorted(tags, key=lambda v: v["Key"]), "bucket tags differ")
        finally:
            self.call("put_bucket_tagging", Bucket=bucket, Tagging={"TagSet": [owner]})
        cors = {"CORSRules": [{"AllowedMethods": ["GET", "HEAD"],
                               "AllowedOrigins": ["https://release-test.invalid"], "MaxAgeSeconds": 60}]}
        try:
            self.call("put_bucket_cors", Bucket=bucket, CORSConfiguration=cors)
        except ClientError as error:
            check(error.response["Error"]["Code"] == "NotImplemented" and
                  error.response["ResponseMetadata"]["HTTPStatusCode"] == 501,
                  "unexpected CORS rejection")
            self.report.setdefault("capabilities", {})["bucket_cors"] = "unsupported (HTTP 501 NotImplemented); behavior verified"
        else:
            try:
                check(self.call("get_bucket_cors", Bucket=bucket)["CORSRules"] == cors["CORSRules"], "CORS differs")
            finally:
                self.call("delete_bucket_cors", Bucket=bucket)
            self.error("get_bucket_cors", {"NoSuchCORSConfiguration"}, status=404, Bucket=bucket)
            self.report.setdefault("capabilities", {})["bucket_cors"] = "round trip and deletion verified"

    def bucket_policy(self):
        """Anonymous GET is limited to one synthetic object; sibling reads, lists and writes stay denied."""
        bucket = self.bucket
        allowed = self.put(bucket, "policy/allowed", 1024)
        self.put(bucket, "policy/private", 1024)
        policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": ["*"]},
                  "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{bucket}/policy/allowed"]}]}
        self.policy_buckets.add(bucket)  # Also clean up when the PUT response is lost.
        try:
            self.call("put_bucket_policy", Bucket=bucket, Policy=json.dumps(policy))
            status, data = self.http_request(f"{self.endpoint}/{bucket}/policy/allowed")
            check(status == 200 and hashlib.sha256(data).hexdigest() == allowed["sha256"], "anonymous GET differs")
            for suffix, method, body in (("/policy/private", "GET", None), ("?list-type=2", "GET", None),
                                         ("/policy/denied", "PUT", b"denied")):
                if method == "PUT":
                    self.event("mutation", operation="anonymous_put", bucket=bucket, key="policy/denied")
                status, _ = self.http_request(self.endpoint + "/" + bucket + suffix, data=body, method=method)
                check(status == 403, f"bucket policy allowed anonymous {method} outside its object")
        finally:
            self.remove_test_policy(bucket)
        status, _ = self.http_request(f"{self.endpoint}/{bucket}/policy/allowed")
        check(status == 403, "deleted policy still permits anonymous GET")
        self.missing(bucket, "policy/denied")

    def encryption_behavior(self):
        """HTTP rejects SSE-C without storing data; HTTPS verifies correct-key reads and wrong/missing-key denial."""
        args = dict(Bucket=self.bucket, Key="encryption/customer-key")
        customer = dict(SSECustomerAlgorithm="AES256", SSECustomerKey=os.urandom(32))
        data = b"customer-key encryption test"
        if self.endpoint.startswith("http:"):
            self.error("put_object", {"InvalidRequest"}, status=400, **args, **customer, Body=data)
            self.missing(self.bucket, args["Key"])
            self.report.setdefault("capabilities", {})["sse_c"] = "HTTP rejection verified; encrypted round trip requires HTTPS"
            return
        try:
            self.call("put_object", **args, **customer, Body=data)
            result = self.call("get_object", **args, **customer)
            with result["Body"] as body:
                check(body.read() == data, "SSE-C decrypted data differs")
            self.error("get_object", {"InvalidRequest", "InvalidArgument"}, status=400, **args)
            self.error("get_object", {"AccessDenied"}, status=403, **args,
                       SSECustomerAlgorithm="AES256", SSECustomerKey=os.urandom(32))
        finally:
            self.call("delete_object", **args)
        self.missing(self.bucket, args["Key"])
        self.report.setdefault("capabilities", {})["sse_c"] = "encrypted round trip and key rejection verified"

    def object_lock_configuration(self):
        """A fresh empty lock-enabled bucket reports object lock and versioning, without retaining objects."""
        bucket = self.extra_bucket("lock", object_lock=True)
        config = self.call("get_object_lock_configuration", Bucket=bucket)["ObjectLockConfiguration"]
        check(config.get("ObjectLockEnabled") == "Enabled" and not config.get("Rule"), "unexpected object lock defaults")
        check(self.call("get_bucket_versioning", Bucket=bucket).get("Status") == "Enabled", "object lock did not enable versioning")
