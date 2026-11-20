"""Explicit HTTP 503 retries stay bounded, replay safe and visible in reports."""
from concurrent.futures import ThreadPoolExecutor
import io
import json

from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.stub import Stubber
import pytest

from tminio_test_runner import endpoint_check, request_retry, soak_cases


def unavailable(operation="HeadObject", *, status=503, message="temporarily unavailable"):
    return ClientError({"Error": {"Code": "SlowDown", "Message": message},
                        "ResponseMetadata": {"HTTPStatusCode": status, "RetryAttempts": 0,
                                             "RequestId": "request-unavailable"}}, operation)


def success(**fields):
    return {"ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0,
                                  "RequestId": "request-success"}, **fields}


@pytest.fixture
def suite(tmp_path, monkeypatch):
    value = endpoint_check.EndpointSuite("http://minio.invalid:9000", "private-access", "private-secret", tmp_path)
    soak_cases.configure_soak(value, 300, 32)
    value.owned_buckets.add(value.bucket)
    value.retry_delays = []
    monkeypatch.setattr(request_retry.time, "sleep", value.retry_delays.append)
    monkeypatch.setattr(request_retry.random, "uniform", lambda *_: 0.125)
    yield value
    for client in value.clients:
        client.close()
    if value.data.exists():
        value.cleanup_data()


def counters(suite):
    return {name: suite.report["http_503_retry"][name] for name in (
        "responses", "retries", "recovered_requests", "exhausted_requests", "unreplayable_requests")}


def test_503_then_success_reports_both_responses_and_recovery(suite):
    args = {"Bucket": suite.bucket, "Key": "retry-success"}
    with Stubber(suite.client) as stub:
        stub.add_client_error("head_object", service_error_code="SlowDown", http_status_code=503,
                              expected_params=args)
        stub.add_response("head_object", success(ContentLength=3), args)
        assert suite.call("head_object", **args)["ContentLength"] == 3
        stub.assert_no_pending_responses()
    assert suite.total_requests == 2
    assert suite.retry_delays == [1.125]
    assert counters(suite) == dict(responses=1, retries=1, recovered_requests=1,
                                   exhausted_requests=0, unreplayable_requests=0)
    events = [json.loads(line) for line in (suite.directory / "requests.jsonl").read_text().splitlines()]
    assert [event["status"] for event in events if event["kind"] == "response"] == [503, 200]
    assert [event["kind"] for event in events if event["kind"] != "response"] == [
        "http_503", "retry", "retry_recovered"]


def test_third_503_exhausts_retries_and_keeps_the_request_failed(suite):
    args = {"Bucket": suite.bucket, "Key": "retry-exhausted"}
    with Stubber(suite.client) as stub:
        for _ in range(3):
            stub.add_client_error("head_object", service_error_code="SlowDown", http_status_code=503,
                                  expected_params=args)
        with pytest.raises(ClientError) as error:
            suite.call("head_object", **args)
        assert error.value.response["ResponseMetadata"]["HTTPStatusCode"] == 503
        stub.assert_no_pending_responses()
    assert suite.total_requests == 3
    assert suite.retry_delays == [1.125, 2.125]
    assert counters(suite) == dict(responses=3, retries=2, recovered_requests=0,
                                   exhausted_requests=1, unreplayable_requests=0)


def test_expected_missing_object_after_503_counts_as_recovered(suite):
    args = {"Bucket": suite.bucket, "Key": "already-deleted"}
    with Stubber(suite.client) as stub:
        stub.add_client_error("get_object", service_error_code="SlowDown", http_status_code=503,
                              expected_params=args)
        stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404,
                              expected_params=args)
        suite.missing(suite.bucket, args["Key"])
        stub.assert_no_pending_responses()
    assert suite.report["http_503_retry"]["recovered_requests"] == 1
    assert suite.report["http_503_retry"]["failed_requests"] == 0
    events = [json.loads(line) for line in (suite.directory / "retry-events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "retry_recovered"
    assert events[-1]["status"] == 404


def test_expected_http_status_does_not_bypass_original_error_code_assertion(suite):
    args = {"Bucket": suite.bucket, "Key": "wrong-error-code"}
    with Stubber(suite.client) as stub:
        stub.add_client_error("get_object", service_error_code="SlowDown", http_status_code=503,
                              expected_params=args)
        stub.add_client_error("get_object", service_error_code="UnexpectedError", http_status_code=404,
                              expected_params=args)
        with pytest.raises(AssertionError, match="UnexpectedError"):
            suite.missing(suite.bucket, args["Key"])
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("status", [500, 403])
def test_non_503_status_is_not_retried_even_with_slowdown_code(suite, status):
    with Stubber(suite.client) as stub:
        stub.add_client_error("head_object", service_error_code="SlowDown", http_status_code=status)
        with pytest.raises(ClientError):
            suite.call("head_object", Bucket=suite.bucket, Key="other-status")
        stub.assert_no_pending_responses()
    assert suite.total_requests == 1
    assert not suite.retry_delays
    assert not any(counters(suite).values())


def test_transport_timeout_is_not_retried(suite, monkeypatch):
    attempts = []
    def timed_out(**kwargs):
        attempts.append(kwargs)
        raise ReadTimeoutError(endpoint_url=suite.endpoint)
    monkeypatch.setattr(suite.client, "head_object", timed_out)
    with pytest.raises(ReadTimeoutError):
        suite.call("head_object", Bucket=suite.bucket, Key="timeout")
    assert len(attempts) == 1
    assert not suite.retry_delays
    assert not any(counters(suite).values())


def test_seekable_upload_replays_from_original_stream_position(suite, monkeypatch):
    body = io.BytesIO(b"prefix-payload")
    body.seek(7)
    attempts = []
    def upload(**kwargs):
        attempts.append(kwargs["Body"].read())
        if len(attempts) == 1:
            raise unavailable("PutObject")
        return success(VersionId="new-version")
    monkeypatch.setattr(suite.client, "put_object", upload)
    assert suite.call("put_object", Bucket=suite.bucket, Key="stream", Body=body)["VersionId"] == "new-version"
    assert attempts == [b"payload", b"payload"]
    assert (suite.bucket, "stream", "new-version") in suite.known_versions
    assert counters(suite)["recovered_requests"] == 1


def test_nonseekable_upload_is_not_retried_after_503(suite, monkeypatch):
    class NonSeekable:
        def read(self, *_):
            return b"payload"
    attempts = []
    def upload(**kwargs):
        attempts.append(kwargs["Body"].read())
        raise unavailable("PutObject")
    monkeypatch.setattr(suite.client, "put_object", upload)
    with pytest.raises(ClientError):
        suite.call("put_object", Bucket=suite.bucket, Key="unreplayable", Body=NonSeekable())
    assert attempts == [b"payload"]
    assert not suite.retry_delays
    assert counters(suite) == dict(responses=1, retries=0, recovered_requests=0,
                                   exhausted_requests=0, unreplayable_requests=1)


def test_upload_that_stops_being_seekable_keeps_original_503_failure(suite, monkeypatch):
    class FailedRewind(io.BytesIO):
        consumed = False
        def read(self, *args):
            self.consumed = True
            return super().read(*args)
        def seek(self, *args):
            if self.consumed:
                raise OSError("stream rewind failed")
            return super().seek(*args)
    attempts = []
    def upload(**kwargs):
        attempts.append(kwargs["Body"].read())
        raise unavailable("PutObject")
    monkeypatch.setattr(suite.client, "put_object", upload)
    with pytest.raises(ClientError):
        suite.call("put_object", Bucket=suite.bucket, Key="rewind-failed", Body=FailedRewind(b"payload"))
    assert attempts == [b"payload"]
    assert not suite.retry_delays
    assert suite.report["http_503_retry"]["unreplayable_requests"] == 1
    assert suite.report["http_503_retry"]["failed_requests"] == 1


@pytest.mark.parametrize("next_failure", ["500", "timeout", "missing-http-status"])
def test_different_failure_after_503_ends_retry_chain_and_is_reported(suite, monkeypatch, next_failure):
    attempts = []
    def read(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise unavailable()
        if next_failure == "500":
            raise unavailable(status=500, message="private-secret private-access server failure")
        if next_failure == "missing-http-status":
            raise ClientError({"Error": {"Code": "UnknownFailure", "Message": "missing response metadata"}},
                              "HeadObject")
        raise ReadTimeoutError(endpoint_url=suite.endpoint)
    monkeypatch.setattr(suite.client, "head_object", read)
    with pytest.raises((ClientError, ReadTimeoutError)):
        suite.call("head_object", Bucket=suite.bucket, Key="terminal-failure")
    assert len(attempts) == 2
    assert suite.report["http_503_retry"]["retries"] == 1
    assert suite.report["http_503_retry"]["failed_requests"] == 1
    assert suite.report["http_503_retry"]["recovered_requests"] == 0
    text = (suite.directory / "retry-events.jsonl").read_text()
    events = [json.loads(line) for line in text.splitlines()]
    assert events[-1]["kind"] == "retry_failed"
    assert "private-access" not in text and "private-secret" not in text


def test_interrupt_during_backoff_does_not_count_an_unissued_retry(suite, monkeypatch):
    def interrupt(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(request_retry.time, "sleep", interrupt)
    with Stubber(suite.client) as stub:
        stub.add_client_error("head_object", service_error_code="SlowDown", http_status_code=503)
        with pytest.raises(KeyboardInterrupt):
            suite.call("head_object", Bucket=suite.bucket, Key="interrupted-backoff")
        stub.assert_no_pending_responses()
    assert suite.total_requests == 1
    assert suite.report["http_503_retry"]["responses"] == 1
    assert suite.report["http_503_retry"]["retries"] == 0


def test_parallel_retry_counts_and_full_history_survive_request_log_rotation(suite, monkeypatch):
    suite.request_log_limit = 1024
    attempts = {}
    def read(**kwargs):
        key = kwargs["Key"]
        # Each key is owned by one worker; only retry/report state is shared.
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] == 1:
            raise unavailable(message="private-access private-secret temporarily unavailable")
        return success(ContentLength=1)
    monkeypatch.setattr(suite.client, "head_object", read)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda index: suite.call("head_object", Bucket=suite.bucket,
                                                            Key=f"parallel-{index}"), range(32)))
    assert len(results) == 32 and set(attempts.values()) == {2}
    assert suite.total_requests == 64
    assert counters(suite) == dict(responses=32, retries=32, recovered_requests=32,
                                   exhausted_requests=0, unreplayable_requests=0)
    assert suite.report["request_log"]["rotations"] > 3
    history = (suite.directory / "retry-events.jsonl").read_text()
    events = [json.loads(line) for line in history.splitlines()]
    assert len(events) == 96
    assert sum(event["kind"] == "http_503" for event in events) == 32
    assert sum(event["kind"] == "retry_recovered" for event in events) == 32
    chains = {}
    for event in events:
        chains.setdefault(event["call_id"], []).append(event)
    assert len(chains) == 32
    for chain in chains.values():
        assert [event["kind"] for event in chain] == ["http_503", "retry", "retry_recovered"]
        assert len({event["key"] for event in chain}) == 1
    assert "private-access" not in history and "private-secret" not in history
    assert {event["key"] for event in events} == {f"parallel-{index}" for index in range(32)}
    logs = list(suite.directory.glob("requests.jsonl*"))
    assert len(logs) <= 4 and sum(path.stat().st_size for path in logs) <= 4096


def test_normal_endpoint_suite_keeps_strict_no_retry_behavior(tmp_path):
    value = endpoint_check.EndpointSuite("http://minio.invalid:9000", "test-access", "test-secret", tmp_path)
    value.owned_buckets.add(value.bucket)
    try:
        with Stubber(value.client) as stub:
            stub.add_client_error("head_object", service_error_code="SlowDown", http_status_code=503)
            with pytest.raises(ClientError):
                value.call("head_object", Bucket=value.bucket, Key="strict")
            stub.assert_no_pending_responses()
        assert value.total_requests == 1
        assert not (value.directory / "retry-events.jsonl").exists()
    finally:
        for client in value.clients:
            client.close()
        value.cleanup_data()


def test_reconciliation_removes_committed_503_versions_markers_and_uploads_only_for_exact_key(suite):
    key, sibling = "worker-0/slot-0/data", "worker-0/slot-0/data-other"
    args = {"Bucket": suite.bucket, "Key": key}
    with Stubber(suite.client) as stub:
        for method, parameters, response in (
            ("put_object", {**args, "Body": b"payload"}, success(VersionId="ack-version")),
            ("delete_object", args, success(VersionId="ack-marker")),
            ("create_multipart_upload", args, success(UploadId="ack-upload")),
        ):
            stub.add_client_error(method, service_error_code="SlowDown", http_status_code=503,
                                  expected_params=parameters)
            stub.add_response(method, response, parameters)
            suite.call(method, **parameters)
        assert suite.retry_reconcile_keys == {(suite.bucket, key)}
        # The first503attempts might have committed, so their IDs were not
        # acknowledged. Prefix results also contain another worker's exact key.
        stub.add_response("list_multipart_uploads", {"Uploads": [
            {"Key": key, "UploadId": "unack-upload"},
            {"Key": sibling, "UploadId": "foreign-upload"},
            {"Key": key, "UploadId": "ack-upload"},
        ]}, {"Bucket": suite.bucket, "Prefix": key})
        for upload in ("unack-upload", "ack-upload"):
            stub.add_response("abort_multipart_upload", {}, {**args, "UploadId": upload})
        stub.add_response("list_object_versions", {
            "Versions": [{"Key": key, "VersionId": "unack-version"},
                         {"Key": sibling, "VersionId": "foreign-version"}],
            "IsTruncated": True, "NextKeyMarker": key, "NextVersionIdMarker": "unack-version",
        }, {"Bucket": suite.bucket, "Prefix": key})
        stub.add_response("list_object_versions", {
            "Versions": [{"Key": key, "VersionId": "ack-version"}],
            "DeleteMarkers": [{"Key": key, "VersionId": "unack-marker"},
                              {"Key": key, "VersionId": "ack-marker"},
                              {"Key": sibling, "VersionId": "foreign-marker"}],
        }, {"Bucket": suite.bucket, "Prefix": key, "KeyMarker": key, "VersionIdMarker": "unack-version"})
        for version in ("unack-version", "ack-version", "unack-marker", "ack-marker"):
            stub.add_response("delete_object", {}, {**args, "VersionId": version})
        soak_cases.reconcile_retried_keys(suite, suite.bucket, (key,), suite.client)
        stub.assert_no_pending_responses()
    assert not suite.retry_reconcile_keys and not suite.known_versions and not suite.known_uploads
    assert suite.known_keys == {(suite.bucket, key)}
    assert suite.report["http_503_retry"]["recovered_requests"] == 3


def test_reconciliation_failure_preserves_failed_key_but_attempts_other_keys(suite):
    failed, cleaned = "worker/failed", "worker/cleaned"
    suite.known_keys.update((suite.bucket, key) for key in (failed, cleaned))
    suite.retry_reconcile_keys.update(suite.known_keys)
    with Stubber(suite.client) as stub:
        stub.add_client_error("list_multipart_uploads", service_error_code="AccessDenied", http_status_code=403,
                              expected_params={"Bucket": suite.bucket, "Prefix": failed})
        stub.add_response("list_multipart_uploads", {}, {"Bucket": suite.bucket, "Prefix": cleaned})
        stub.add_response("list_object_versions", {}, {"Bucket": suite.bucket, "Prefix": cleaned})
        with pytest.raises(AssertionError, match="could not reconcile"):
            soak_cases.reconcile_retried_keys(suite, suite.bucket, (failed, cleaned), suite.client)
        stub.assert_no_pending_responses()
    assert suite.retry_reconcile_keys == {(suite.bucket, failed)}


def test_retry_reconciliation_refuses_keys_without_recorded_mutation_intent(suite):
    suite.retry_reconcile_keys.add((suite.bucket, "unowned"))
    with Stubber(suite.client):
        with pytest.raises(AssertionError, match="could not reconcile"):
            soak_cases.reconcile_retried_keys(suite, suite.bucket, ("unowned",), suite.client)
    assert suite.retry_reconcile_keys == {(suite.bucket, "unowned")}


def completed_cleanup_responses(stub, suite):
    args = {"Bucket": suite.bucket}
    stub.add_response("get_bucket_tagging", {"TagSet": [
        {"Key": "tminio-check-owner", "Value": suite.owner_token}]}, args)
    stub.add_response("list_object_versions", {}, args)
    stub.add_response("list_multipart_uploads", {}, args)
    stub.add_response("delete_bucket", {}, args)
    stub.add_client_error("head_bucket", service_error_code="NoSuchBucket", http_status_code=404,
                          expected_params=args)


def test_successful_whole_bucket_cleanup_cannot_erase_exhausted_503_failure(suite):
    suite.check_names = ["Remove test buckets"]
    with Stubber(suite.client) as stub:
        # First bucket-cleanup attempt exhausts the per-call retries. The
        # existing bucket-level recovery retries and successfully removes it.
        for _ in range(3):
            stub.add_client_error("get_bucket_tagging", service_error_code="SlowDown", http_status_code=503,
                                  expected_params={"Bucket": suite.bucket})
        completed_cleanup_responses(stub, suite)
        assert suite.run_case("Remove test buckets", suite.teardown)
        stub.assert_no_pending_responses()
    assert not suite.data_retained()
    assert suite.report["http_503_retry"]["exhausted_requests"] == 1
    assert suite.finish() == 1
    assert suite.report["results"][-1]["name"] == "HTTP 503 retry policy"
    assert suite.report["results"][-1]["status"] == "FAIL"
    assert suite.report["test_data_retained"] is False


def test_successful_later_cleanup_cannot_erase_unreplayable_503_failure(suite, monkeypatch):
    class NonSeekable:
        def read(self, *_):
            return b"payload"
    def upload(**kwargs):
        kwargs["Body"].read()
        raise unavailable("PutObject")
    monkeypatch.setattr(suite.client, "put_object", upload)
    with pytest.raises(ClientError):
        suite.call("put_object", Bucket=suite.bucket, Key="unreplayable", Body=NonSeekable())
    suite.check_names = ["Remove test buckets"]
    with Stubber(suite.client) as stub:
        completed_cleanup_responses(stub, suite)
        assert suite.run_case("Remove test buckets", suite.teardown)
        stub.assert_no_pending_responses()
    assert not suite.data_retained()
    assert suite.report["http_503_retry"]["unreplayable_requests"] == 1
    assert suite.finish() == 1
    assert suite.report["results"][-1]["name"] == "HTTP 503 retry policy"
    assert suite.report["results"][-1]["status"] == "FAIL"
