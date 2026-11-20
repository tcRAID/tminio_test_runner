"""Safety regressions for testing an existing service without touching its data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from botocore.stub import Stubber
import pytest

from tminio_test_runner import endpoint_check as endpoint
@pytest.fixture
def suite(tmp_path):
    value = endpoint.EndpointSuite("https://minio.invalid:9000", "test-access", "test-secret-password", tmp_path)
    yield value
    for client in value.clients:
        client.close()


@pytest.mark.parametrize("url", ["/path/to/minio", "https://user:secret@host:9000", "http://host/bucket",
                                 "http://host?token=secret", "http://host:bad", "http://host:0", "http://host/#fragment"])
def test_endpoint_rejects_non_origins_without_echoing_credentials(url):
    with pytest.raises(argparse.ArgumentTypeError) as error:
        endpoint.endpoint_url(url)
    assert url not in str(error.value)


def test_valid_urls_and_secure_client_defaults(suite):
    assert endpoint.endpoint_url("https://host:9000/") == "https://host:9000"
    assert endpoint.endpoint_url("http://[::1]:9000") == "http://[::1]:9000"
    assert suite.verify_tls is True
    assert suite.client.meta.config.proxies == {}
    assert suite.client.meta.config.retries["total_max_attempts"] == 1
    assert len(suite.planned_buckets) == 3
    assert not suite.owned_buckets
    assert all(suite.owner_token in b and len(b) <= 63 for b in suite.planned_buckets)


@pytest.mark.parametrize("options,expected", [({}, 60), ({"read_timeout": 120}, 120)])
def test_read_timeout_reaches_main_worker_and_sts_clients(tmp_path, options, expected):
    value = endpoint.EndpointSuite("http://minio.invalid:9000", "test-access", "test-secret", tmp_path, **options)
    try:
        clients = [value.client, value.make_client(), value.make_client(service="sts")]
        for client in clients:
            assert client.meta.config.read_timeout == expected
            assert client.meta.config.connect_timeout == 5
            assert client.meta.config.retries["total_max_attempts"] == 1
            assert client.meta.config.proxies == {}
        assert value.report["read_timeout_seconds"] == expected
    finally:
        for client in value.clients:
            client.close()
        value.cleanup_data()


@pytest.mark.parametrize("soak", [False, True])
@pytest.mark.parametrize("arguments,expected", [([], 60), (["--read-timeout", "120"], 120)])
def test_cli_read_timeout_is_applied_and_reported_before_health_check(
        tmp_path, monkeypatch, soak, arguments, expected):
    created = []
    def stop_before_network(value):
        created.append(value)
        raise RuntimeError("intentional health failure before network access")
    monkeypatch.setattr(endpoint.EndpointSuite, "cluster_health", stop_before_network)
    monkeypatch.setattr(endpoint, "credentials", lambda: ("test-access", "test-secret"))
    args = ["http://minio.invalid:9000", "--work-dir", str(tmp_path), *arguments]
    if soak:
        args += ["--duration", "3600"]
    assert endpoint.main(args, soak=soak) == 1
    value, = created
    assert value.client.meta.config.read_timeout == expected
    report = json.loads((value.directory / "results.json").read_text())
    assert report["read_timeout_seconds"] == expected
    assert report["timeout_seconds"] == (4200 if soak else 1800)
    assert report["results"][0]["name"] == "Cluster health and identity"
    assert "intentional health failure" in report["results"][0]["actual"]
    assert not value.data_retained()


@pytest.mark.parametrize("soak", [False, True])
@pytest.mark.parametrize("seconds", ["0", "-1"])
def test_invalid_read_timeout_is_rejected_before_credentials_or_network(monkeypatch, capsys, soak, seconds):
    monkeypatch.setattr(endpoint, "credentials", lambda: pytest.fail("invalid read timeout reached credential lookup"))
    with pytest.raises(SystemExit) as error:
        endpoint.main(["http://minio.invalid:9000", "--read-timeout", seconds], soak=soak)
    assert error.value.code == 2
    assert "--read-timeout must be positive" in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
def test_endpoint_can_never_control_a_service(suite, operation):
    with pytest.raises(AssertionError, match="cannot"):
        getattr(suite, operation)()


def test_mutations_reads_and_copy_sources_are_confined_to_created_buckets(suite):
    suite.owned_buckets.add(suite.bucket)
    with Stubber(suite.client):  # Any SDK request would fail the empty stub.
        for method in ("put_object", "delete_bucket", "list_object_versions", "put_bucket_versioning"):
            with pytest.raises(AssertionError, match="outside"):
                suite.call(method, Bucket="production")
        with pytest.raises(AssertionError, match="copy source"):
            suite.call("copy_object", Bucket=suite.bucket, Key="copy", CopySource={"Bucket": "production", "Key": "keep"})
        with pytest.raises(AssertionError, match="reserved"):
            suite.call("create_bucket", Bucket=suite.versioned_bucket)
        with pytest.raises(AssertionError, match="mutation outside"):
            suite.event("mutation", bucket="production", key="keep", operation="presigned_put")


@pytest.mark.parametrize("exists", [True, False])
def test_existing_or_inaccessible_bucket_is_never_adopted(suite, exists):
    with Stubber(suite.client) as stub:
        if exists:
            stub.add_response("head_bucket", {}, {"Bucket": suite.bucket})
        else:
            stub.add_client_error("head_bucket", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(AssertionError, match="already exists|cannot establish"):
            suite.create_test_bucket(suite.bucket)
        stub.assert_no_pending_responses()
    assert not suite.owned_buckets and not suite.uncertain_buckets


def test_conflicting_create_never_adopts_or_deletes_bucket(suite):
    with Stubber(suite.client) as stub:
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        stub.add_client_error("create_bucket", service_error_code="BucketAlreadyOwnedByYou", http_status_code=409)
        with pytest.raises(endpoint.ClientError):
            suite.create_test_bucket(suite.bucket)
    assert not suite.owned_buckets
    assert suite.creating is None
    assert not suite.uncertain_buckets
    with Stubber(suite.client):  # Collision is never contacted by teardown.
        suite.teardown()
    assert not suite.data_retained()


def test_new_bucket_creation_and_tagging_uses_requested_region(suite):
    suite.region = "internal-east-1"
    with Stubber(suite.client) as stub:
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        stub.add_response("create_bucket", {}, {"Bucket": suite.bucket,
                          "CreateBucketConfiguration": {"LocationConstraint": "internal-east-1"}})
        stub.add_response("put_bucket_tagging", {}, {"Bucket": suite.bucket,
                          "Tagging": {"TagSet": [{"Key": "tminio-check-owner", "Value": suite.owner_token}]}})
        suite.create_test_bucket(suite.bucket)
        stub.assert_no_pending_responses()
    assert suite.owned_buckets == {suite.bucket} and not suite.uncertain_buckets


@pytest.mark.parametrize("foreign", ["tag", "key", "upload_key"])
def test_cleanup_refuses_foreign_data_before_any_deletion(suite, foreign):
    suite.owned_buckets.add(suite.bucket)
    suite.known_keys.add((suite.bucket, "known"))
    with Stubber(suite.client) as stub:
        stub.add_response("get_bucket_tagging", {"TagSet": [{"Key": "tminio-check-owner",
                          "Value": "someone-else" if foreign == "tag" else suite.owner_token}]})
        if foreign != "tag":
            versions = [] if foreign == "upload_key" else [{"Key": "unknown", "VersionId": "foreign"}]
            stub.add_response("list_object_versions", {"Versions": versions})
            stub.add_response("list_multipart_uploads", {"Uploads": [{"Key": "unknown", "UploadId": "foreign"}]} if foreign == "upload_key" else {})
        with pytest.raises(AssertionError, match="ownership|unknown"):
            suite.cleanup_remote()
        stub.assert_no_pending_responses()
    assert suite.owned_buckets == {suite.bucket}


def test_cleanup_removes_explicit_versions_and_delete_markers_only(suite):
    suite.owned_buckets.add(suite.bucket)
    suite.known_keys.add((suite.bucket, "known"))
    suite.known_versions.update((suite.bucket, "known", v) for v in ("v1", "marker"))
    objects = [{"Key": "known", "VersionId": "v1"}, {"Key": "known", "VersionId": "marker"}]
    with Stubber(suite.client) as stub:
        stub.add_response("get_bucket_tagging", {"TagSet": [{"Key": "tminio-check-owner", "Value": suite.owner_token}]})
        stub.add_response("list_object_versions", {"Versions": objects[:1], "DeleteMarkers": objects[1:]})
        stub.add_response("list_multipart_uploads", {})
        stub.add_response("delete_objects", {"Deleted": objects}, {"Bucket": suite.bucket, "Delete": {"Objects": objects, "Quiet": False}})
        stub.add_response("delete_bucket", {}, {"Bucket": suite.bucket})
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        suite.teardown()
        stub.assert_no_pending_responses()
    assert not suite.data_retained() and not suite.data.exists()
    assert (suite.directory / "results.json").exists()


def test_noninteractive_missing_credentials_fails_before_work_is_created(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)
    for name in ("TMINIO_TEST_ACCESS_KEY", "TMINIO_TEST_SECRET_KEY"):
        env.pop(name, None)
    result = subprocess.run([sys.executable, "-m", "tminio_test_runner.endpoint_check", "http://localhost:9000"],
                            env=env, cwd=tmp_path, input="", capture_output=True, text=True, timeout=15)
    assert result.returncode == 2 and "TMINIO_TEST_ACCESS_KEY" in result.stderr
    assert not list(tmp_path.iterdir())


def test_tagging_failure_removes_empty_partial_setup(suite):
    with Stubber(suite.client) as stub:
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        stub.add_response("create_bucket", {})
        stub.add_client_error("put_bucket_tagging", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(endpoint.ClientError):
            suite.create_test_bucket(suite.bucket)
        assert suite.bucket in suite.untagged_buckets
        stub.add_client_error("get_bucket_tagging", service_error_code="NoSuchTagSet", http_status_code=404)
        stub.add_response("list_object_versions", {})
        stub.add_response("list_multipart_uploads", {})
        stub.add_response("delete_bucket", {}, {"Bucket": suite.bucket})
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        suite.teardown()
        stub.assert_no_pending_responses()
    assert not suite.data_retained()


def test_lost_create_response_removes_empty_previously_absent_bucket(suite, monkeypatch):
    from botocore.exceptions import ReadTimeoutError
    def lose_response(**kwargs):
        raise ReadTimeoutError(endpoint_url=suite.endpoint)
    monkeypatch.setattr(suite.client, "create_bucket", lose_response)
    with Stubber(suite.client) as stub:
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        with pytest.raises(ReadTimeoutError):
            suite.create_test_bucket(suite.bucket)
        assert suite.uncertain_buckets == {suite.bucket}
        stub.add_client_error("get_bucket_tagging", service_error_code="NoSuchTagSet", http_status_code=404)
        stub.add_response("list_object_versions", {})
        stub.add_response("list_multipart_uploads", {})
        stub.add_response("delete_bucket", {}, {"Bucket": suite.bucket})
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        suite.teardown()
        stub.assert_no_pending_responses()
    assert not suite.data_retained()


def test_lost_write_and_multipart_responses_are_cleaned_using_recorded_key_intent(suite):
    suite.owned_buckets.add(suite.bucket)
    suite.known_keys.add((suite.bucket, "attempted"))
    objects = [{"Key": "attempted", "VersionId": "unacknowledged-version"},
               {"Key": "attempted", "VersionId": "scanner-marker"}]
    with Stubber(suite.client) as stub:
        stub.add_response("get_bucket_tagging", {"TagSet": [{"Key":"tminio-check-owner", "Value":suite.owner_token}]})
        stub.add_response("list_object_versions", {"Versions":objects[:1], "DeleteMarkers":objects[1:]})
        stub.add_response("list_multipart_uploads", {"Uploads":[{"Key":"attempted", "UploadId":"lost-upload"}]})
        stub.add_response("abort_multipart_upload", {}, {"Bucket":suite.bucket, "Key":"attempted", "UploadId":"lost-upload"})
        stub.add_response("delete_objects", {"Deleted":objects}, {"Bucket":suite.bucket, "Delete":{"Objects":objects, "Quiet":False}})
        stub.add_response("delete_bucket", {}, {"Bucket":suite.bucket})
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        suite.teardown()
        stub.assert_no_pending_responses()
    assert not suite.data_retained()


def test_pending_upload_pagination_carries_key_and_upload_marker(suite):
    suite.owned_buckets.add(suite.bucket)
    with Stubber(suite.client) as stub:
        stub.add_response("list_multipart_uploads", {"Uploads":[{"Key":"a", "UploadId":"one"}],
            "IsTruncated":True, "NextKeyMarker":"a", "NextUploadIdMarker":"one"}, {"Bucket":suite.bucket})
        stub.add_response("list_multipart_uploads", {"Uploads":[{"Key":"a", "UploadId":"two"}]},
            {"Bucket":suite.bucket, "KeyMarker":"a", "UploadIdMarker":"one"})
        assert len(suite.cleanup_uploads(suite.bucket)) == 2
        stub.assert_no_pending_responses()


def test_client_initialization_failure_removes_allocated_local_payloads(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("bad client configuration")
    monkeypatch.setattr(endpoint.EndpointSuite, "make_client", fail)
    with pytest.raises(ValueError, match="configuration"):
        endpoint.EndpointSuite("http://localhost:9000", "access", "secret", tmp_path)
    directory = next(tmp_path.glob("minio-check-*"))
    assert not any((directory/name).exists() for name in ("data", "payloads", "config", "certs"))
    assert (directory/"results.json").exists()


def test_interrupted_client_setup_returns_failure_without_local_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("TMINIO_TEST_ACCESS_KEY", "test-access")
    monkeypatch.setenv("TMINIO_TEST_SECRET_KEY", "test-secret-password")
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(endpoint.EndpointSuite, "make_client", interrupt)
    assert endpoint.main(["http://localhost:9000", "--work-dir", str(tmp_path)]) == 1
    directory = next(tmp_path.glob("minio-check-*"))
    assert not any((directory/name).exists() for name in ("data", "payloads", "config", "certs"))


def test_teardown_cannot_pass_if_remote_data_was_not_removed(suite, monkeypatch):
    suite.owned_buckets.add(suite.bucket)
    monkeypatch.setattr(suite, "cleanup_remote", lambda: None)
    with pytest.raises(AssertionError, match="data remains"):
        suite.teardown()
    assert not suite.payloads.exists()
    assert suite.data_retained()
