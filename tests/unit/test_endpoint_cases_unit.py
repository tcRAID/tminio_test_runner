"""Current-service regressions for cluster health, object integrity and signed URLs."""
from copy import deepcopy
from io import BytesIO
import hashlib
from urllib.parse import parse_qs, urlsplit

from botocore.response import StreamingBody
from botocore.stub import Stubber
import pytest

from tminio_test_runner.endpoint_cases import cluster_shape
from tminio_test_runner.endpoint_check import EndpointSuite


@pytest.fixture
def suite(tmp_path):
    instance = EndpointSuite("http://127.0.0.1:1", "test-access", "test-secret", tmp_path)
    yield instance
    for client in instance.clients:
        client.close()


@pytest.fixture
def record(suite):
    # These tests model objects created by this run; no prior deployment or
    # saved inventory is involved.
    suite.owned_buckets.add(suite.bucket)
    return {"bucket": suite.bucket, "key": "key", "version": None, "size": 3,
            "sha256": hashlib.sha256(b"old").hexdigest(), "metadata": {},
            "content_type": "application/octet-stream"}


def test_presigned_get_does_not_require_hidden_proxy_header(suite):
    url = suite.client.generate_presigned_url("get_object", Params={"Bucket": suite.bucket, "Key": "key"}, ExpiresIn=60)
    headers = parse_qs(urlsplit(url).query)["X-Amz-SignedHeaders"][0]
    assert "x-minio-source-proxy-request" not in headers


def test_changed_bytes_fail_even_when_s3_request_succeeds(suite, record):
    with Stubber(suite.client) as stub:
        stub.add_response("get_object", {"Body": StreamingBody(BytesIO(b"new"), 3), "ContentLength": 3,
                          "Metadata": {}, "ContentType": "application/octet-stream"})
        with pytest.raises(AssertionError, match="checksum/size"):
            suite.verify(record)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("operation", ["get_object", "head_object"])
def test_changed_metadata_fails_even_when_bytes_match(suite, record, operation):
    response = {"ContentLength": 3, "Metadata": {}, "ContentType": "application/octet-stream"}
    get_response = {**response, "Body": StreamingBody(BytesIO(b"old"), 3)}
    head_response = dict(response)
    if operation == "get_object":
        get_response["Metadata"] = {"unexpected": "changed"}
    else:
        head_response["Metadata"] = {"unexpected": "changed"}
    params = {"Bucket": record["bucket"], "Key": record["key"]}
    with Stubber(suite.client) as stub:
        stub.add_response("get_object", get_response, params)
        stub.add_response("head_object", head_response, params)
        with pytest.raises(AssertionError, match="metadata mismatch"):
            suite.verify(record)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("version, passes", [("null", True), ("uuid-required", False)])
def test_absent_version_header_is_accepted_only_for_explicit_null_slot(suite, record, version, passes):
    record = {**record, "version": version}
    params = {"Bucket": record["bucket"], "Key": record["key"], "VersionId": version}
    response = {"ContentLength": 3, "Metadata": {}, "ContentType": "application/octet-stream"}
    with Stubber(suite.client) as stub:
        stub.add_response("get_object", {**response, "Body": StreamingBody(BytesIO(b"old"), 3)}, params)
        stub.add_response("head_object", response, params)
        if passes:
            suite.verify(record)
        else:
            with pytest.raises(AssertionError, match="wrong version"):
                suite.verify(record)
        stub.assert_no_pending_responses()


def info():
    return {"deploymentID": "cluster", "backend": {"backendType": "Erasure", "offlineDisks": 0,
             "totalSets": [1], "totalDrivesPerSet": [4], "standardSCParity": 2},
            "servers": [{"state": "online", "version": "new", "commitID": "new-commit",
                         "drives": [{"state": "ok"} for _ in range(4)]}]}


def test_healthy_cluster_reports_its_topology():
    assert cluster_shape(info()) == {"deployment_id": "cluster", "nodes": 1, "drives": 4,
                                     "sets_per_pool": [1], "drives_per_set": [4], "parity": 2}


@pytest.mark.parametrize("fault", ["node", "disk", "mixed", "identity"])
def test_unhealthy_or_ambiguous_cluster_cannot_pass(fault):
    value = info()
    if fault == "node":
        value["servers"][0]["state"] = "offline"
    elif fault == "disk":
        value["servers"][0]["drives"][0]["state"] = "offline"
    elif fault == "mixed":
        old = deepcopy(value["servers"][0])
        old["commitID"] = "old-commit"
        value["servers"].append(old)
    else:
        value.pop("deploymentID")
    with pytest.raises(AssertionError):
        cluster_shape(value)
