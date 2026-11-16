"""Teardown for buckets created by the explicitly selected advanced lab tests."""
import json
import time

from botocore.exceptions import ClientError


def absent(error, *codes):
    return isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in codes


class LabBuckets:
    def __init__(self, artifacts, token):
        self.artifacts, self.token, self.entries = artifacts, token, []
        self.created_names = set()

    def record(self, client, name, locked=False):
        entry = {"client": client, "name": name, "locked": locked, "tagged": False}
        self.created_names.add(name)
        self.entries.append(entry)
        self.save()
        return entry

    def save(self):
        path = self.artifacts / "remaining-test-buckets.json"
        previous = json.loads(path.read_text()) if path.exists() else []
        remaining = [entry for entry in previous if entry["bucket"] not in self.created_names]
        remaining.extend({"bucket": e["name"], "endpoint": e["client"].meta.endpoint_url, "object_lock": e["locked"]}
                         for e in self.entries)
        path.write_text(json.dumps(remaining, indent=2))


    def prepare(self, entry):
        c, name = entry["client"], entry["name"]
        try:
            tags = c.get_bucket_tagging(Bucket=name).get("TagSet", [])
        except ClientError as error:
            if absent(error, "NoSuchBucket"):
                return False
            if not absent(error, "NoSuchTagSet", "NoSuchTagSetError"):
                raise
            tags = []
        owner = [t for t in tags if t["Key"] == "tminio-check-owner"]
        if owner != [{"Key": "tminio-check-owner", "Value": self.token}]:
            assert not tags and not entry["tagged"], "lab bucket ownership tag changed"
            versions = c.list_object_versions(Bucket=name)
            uploads = c.list_multipart_uploads(Bucket=name)
            assert not any(versions.get(k) for k in ("Versions", "DeleteMarkers", "IsTruncated")), "untagged bucket contains data"
            assert not any(uploads.get(k) for k in ("Uploads", "IsTruncated")), "untagged bucket contains uploads"
        for method, codes in (("delete_bucket_replication", ("ReplicationConfigurationNotFoundError", "XMinioAdminRemoteTargetNotFoundError")),
                              ("delete_bucket_lifecycle", ("NoSuchLifecycleConfiguration",)),
                              ("delete_bucket_policy", ("NoSuchBucketPolicy",))):
            try:
                getattr(c, method)(Bucket=name)
            except ClientError as error:
                if not (absent(error, *codes) and error.response["ResponseMetadata"]["HTTPStatusCode"] == 404):
                    raise
        return True

    def purge(self, entry, deadline):
        c, name = entry["client"], entry["name"]
        def budget():
            assert time.monotonic() < deadline, "lab cleanup deadline exceeded"
        for page in c.get_paginator("list_multipart_uploads").paginate(Bucket=name):
            for upload in page.get("Uploads", []):
                budget()
                c.abort_multipart_upload(Bucket=name, Key=upload["Key"], UploadId=upload["UploadId"])
        # Enumerate before deleting so mutation cannot invalidate pagination.
        objects = []
        for page in c.get_paginator("list_object_versions").paginate(Bucket=name):
            budget()
            for field in ("Versions", "DeleteMarkers"):
                for v in page.get(field, []):
                    if entry["locked"] and field == "Versions":
                        c.put_object_legal_hold(Bucket=name, Key=v["Key"], VersionId=v["VersionId"], LegalHold={"Status": "OFF"})
                    objects.append({"Key": v["Key"], "VersionId": v["VersionId"]})
        for start in range(0, len(objects), 1000):
            budget()
            batch = objects[start:start + 1000]
            options = {"BypassGovernanceRetention": True} if entry["locked"] else {}
            result = c.delete_objects(Bucket=name, Delete={"Objects": batch, "Quiet": False}, **options)
            assert not result.get("Errors"), "lab cleanup returned object errors"
            assert {(v["Key"], v.get("VersionId")) for v in result.get("Deleted", [])} == {
                (v["Key"], v["VersionId"]) for v in batch}, "lab cleanup response incomplete"
        c.delete_bucket(Bucket=name)
        try:
            c.head_bucket(Bucket=name)
        except ClientError as error:
            assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 404, "cannot confirm bucket absence"
        else:
            raise AssertionError("lab test bucket remains")

    def cleanup(self):
        errors, ready = {}, []
        deadline = time.monotonic() + 300
        # Stop both ends' replication and lifecycle before deleting either end.
        for entry in list(self.entries):
            try:
                if self.prepare(entry):
                    ready.append(entry)
                else:
                    self.entries.remove(entry)
            except Exception as error:
                errors[entry["name"]] = {"type": type(error).__name__,
                    "code": error.response.get("Error", {}).get("Code") if isinstance(error, ClientError) else None}
        for entry in ready:
            try:
                self.purge(entry, deadline)
                self.entries.remove(entry)
            except Exception as error:
                errors[entry["name"]] = {"type": type(error).__name__,
                    "code": error.response.get("Error", {}).get("Code") if isinstance(error, ClientError) else None}
        self.save()
        assert not errors, "lab test bucket cleanup failed; see remaining-test-buckets.json: " + repr(errors)
