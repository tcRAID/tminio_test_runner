"""Cleanup regressions for the already-deployed Ubuntu 26.04 release runner.

These tests exercise the real runner, reports and local cleanup. Network
operations are replaced so failure paths never require a running MinIO server.
"""
import json
import signal

import pytest

from tminio_test_runner import endpoint_check as endpoint
LOCAL_DATA_DIRECTORIES = ("data", "payloads", "config", "certs")
CASE_METHODS = (
    "cluster_health", "startup", "objects", "cycles", "listing", "multipart",
    "access_control", "presigned", "conditional_requests", "bucket_configuration", "bucket_policy",
    "encryption_behavior", "object_lock_configuration", "versions", "null_and_restore", "concurrent",
    "lifecycle", "lifecycle_actions", "final_integrity",
)


@pytest.fixture
def local_runner(tmp_path, monkeypatch):
    """Keep real report/cleanup behavior while forbidding all network traffic."""
    monkeypatch.setenv("TMINIO_TEST_ACCESS_KEY", "release-test-access")
    monkeypatch.setenv("TMINIO_TEST_SECRET_KEY", "release-test-secret")
    state = {"instances": [], "ran": [], "removed": [], "alarm": 0}
    original_init = endpoint.EndpointSuite.__init__

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        state["instances"].append(self)

    def alarm(seconds):
        old = state["alarm"]
        state["alarm"] = seconds
        return old

    def forbidden_network(*args, **kwargs):
        pytest.fail("a cleanup unit test attempted an SDK or HTTP request")

    def forbidden_control(*args, **kwargs):
        pytest.fail("the endpoint runner attempted to control the deployed service")

    def case(method):
        def execute(self):
            state["ran"].append(method)
            if method == "startup":
                # Simulate a newly created bucket and bytes left by partial setup.
                self.owned_buckets.add(self.bucket)
                self.record_ownership()
                (self.payloads / "pending-upload").write_bytes(b"test payload")
            if state.get("failure_method") == method:
                raise state["failure"]
        return execute

    def remove_remote(self):
        assert state["alarm"] == 17, "cleanup did not receive its independent deadline"
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
        state["removed"].append(set(self.owned_buckets))
        if state.get("cleanup_failure"):
            raise state["cleanup_failure"]
        self.owned_buckets.clear()
        self.uncertain_buckets.clear()
        self.record_ownership()

    monkeypatch.setattr(endpoint.EndpointSuite, "__init__", initialize)
    monkeypatch.setattr(endpoint.signal, "alarm", alarm)
    monkeypatch.setattr(endpoint.EndpointSuite, "call", forbidden_network)
    monkeypatch.setattr(endpoint.EndpointSuite, "http_request", forbidden_network)
    for method in ("start", "stop", "restart"):
        monkeypatch.setattr(endpoint.EndpointSuite, method, forbidden_control)
    for method in CASE_METHODS:
        monkeypatch.setattr(endpoint.EndpointSuite, method, case(method))
    monkeypatch.setattr(endpoint.EndpointSuite, "cleanup_remote", remove_remote)

    def run():
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}
        status = endpoint.main(["http://minio.invalid:9000", "--work-dir", str(tmp_path),
                                "--timeout", "23", "--cleanup-timeout", "17"])
        assert state["alarm"] == 0
        assert all(signal.getsignal(sig) == handler for sig, handler in before.items())
        suite = state["instances"][-1]
        report = json.loads((suite.directory / "results.json").read_text())
        return status, suite, report

    state["run"] = run
    return state


@pytest.mark.parametrize("method,error,expected_status", [
    (None, None, 0),
    ("cluster_health", AssertionError("health failed before setup"), 1),
    ("startup", AssertionError("setup stopped after first bucket"), 1),
    ("objects", AssertionError("object checksum mismatch"), 1),
    ("objects", endpoint.Deadline("test deadline exceeded"), 1),
    ("objects", KeyboardInterrupt(), 1),
])
def test_main_always_cleans_up_without_a_preexisting_baseline(local_runner, method, error, expected_status):
    state = local_runner
    state.update(failure_method=method, failure=error)
    status, suite, report = state["run"]()

    assert status == expected_status
    assert len(state["removed"]) == 1
    assert not suite.data_retained()
    assert report["test_data_retained"] is False
    assert all(not (suite.directory / name).exists() for name in LOCAL_DATA_DIRECTORIES)
    assert (suite.directory / "summary.txt").is_file()
    assert (suite.directory / "junit.xml").is_file()
    assert not (suite.directory.parent / "minio-migration.json").exists()
    cleanup = [r for r in report["results"] if r["name"] == "Remove test buckets"]
    assert len(cleanup) == 1 and cleanup[0]["status"] == "PASS"
    if error:
        assert report["status"] == "FAIL", "successful cleanup concealed the original test failure"
        assert any(r["status"] == "FAIL" and r["name"] != "Remove test buckets" for r in report["results"])
    else:
        assert report["status"] == "PASS"
        assert len(report["results"]) == len(endpoint.CHECK_NAMES)
    if method == "startup":
        assert state["removed"] == [{suite.bucket}]
        assert "objects" not in state["ran"]
    if isinstance(error, (endpoint.Deadline, KeyboardInterrupt)):
        assert "cycles" not in state["ran"], "an interrupt should end testing before cleanup"


@pytest.mark.parametrize("error", [OSError("cleanup endpoint unreachable"), endpoint.Deadline("cleanup deadline exceeded")])
def test_failed_teardown_keeps_original_failure_and_removes_local_payloads(local_runner, error):
    state = local_runner
    state.update(failure_method="objects", failure=AssertionError("original checksum failure"), cleanup_failure=error)
    status, suite, report = state["run"]()

    assert status == 1 and report["status"] == "FAIL"
    assert len(state["removed"]) == 1
    assert report["test_data_retained"] is True
    assert suite.bucket in report["owned_buckets"]
    assert all(not (suite.directory / name).exists() for name in LOCAL_DATA_DIRECTORIES)
    failures = {r["name"]: r for r in report["results"] if r["status"] == "FAIL"}
    assert "original checksum failure" in failures["Object operations"]["actual"]
    assert str(error) in failures["Remove test buckets"]["actual"]
    assert (suite.directory / "summary.txt").is_file()
    assert (suite.directory / "junit.xml").is_file()


@pytest.mark.parametrize("error", [OSError("remote cleanup failed"), endpoint.Deadline("cleanup deadline"), KeyboardInterrupt()])
def test_teardown_local_cleanup_survives_remote_exceptions(tmp_path, monkeypatch, error):
    suite = endpoint.EndpointSuite("http://minio.invalid:9000", "test-access", "test-secret", tmp_path)
    (suite.payloads / "unuploaded-bytes").write_bytes(b"payload")

    def fail_remote():
        raise error

    monkeypatch.setattr(suite, "cleanup_remote", fail_remote)
    try:
        with pytest.raises(type(error)):
            suite.teardown()
        assert all(not (suite.directory / name).exists() for name in LOCAL_DATA_DIRECTORIES)
        assert (suite.directory / "results.json").is_file()
        assert (suite.directory / "objects.json").is_file()
    finally:
        for client in suite.clients:
            client.close()


@pytest.mark.parametrize("timeout", ["0", "-1"])
def test_cleanup_deadline_must_be_positive_before_setup(tmp_path, monkeypatch, timeout):
    monkeypatch.setenv("TMINIO_TEST_ACCESS_KEY", "test-access")
    monkeypatch.setenv("TMINIO_TEST_SECRET_KEY", "test-secret")
    with pytest.raises(SystemExit) as error:
        endpoint.main(["http://minio.invalid:9000", "--work-dir", str(tmp_path), "--cleanup-timeout", timeout])
    assert error.value.code == 2
    assert not list(tmp_path.iterdir())


def test_one_bucket_cleanup_failure_does_not_skip_other_owned_buckets(tmp_path, monkeypatch):
    suite = endpoint.EndpointSuite("http://minio.invalid:9000", "test-access", "test-secret", tmp_path)
    buckets = sorted(suite.planned_buckets)
    suite.owned_buckets.update(buckets)
    blocked = buckets[0]
    visited = []

    def remove_bucket(self, bucket):
        visited.append(bucket)
        if bucket == blocked:
            raise AssertionError("bucket ownership tag changed")
        self.owned_buckets.remove(bucket)
        self.record_ownership()

    monkeypatch.setattr(endpoint.EndpointSuite, "cleanup_bucket", remove_bucket)
    try:
        with pytest.raises(AssertionError, match="ownership"):
            suite.teardown()
        assert set(visited) == set(buckets)
        assert suite.owned_buckets == {blocked}
        assert suite.data_retained()
        assert all(not (suite.directory / name).exists() for name in LOCAL_DATA_DIRECTORIES)
    finally:
        for client in suite.clients:
            client.close()


@pytest.mark.parametrize("failures,expected_attempts", [(1, 2), (4, 3)])
def test_transient_bucket_failure_retries_without_blocking_other_cleanup(tmp_path, monkeypatch, failures, expected_attempts):
    suite = endpoint.EndpointSuite("http://minio.invalid:9000", "test-access", "test-secret", tmp_path)
    buckets = sorted(suite.planned_buckets)
    suite.owned_buckets.update(buckets)
    failing = buckets[0]
    visited = []

    def remove_bucket(self, bucket):
        visited.append(bucket)
        if bucket == failing and visited.count(bucket) <= failures:
            raise OSError("temporary connection failure")
        self.owned_buckets.remove(bucket)
        self.record_ownership()

    monkeypatch.setattr(endpoint.EndpointSuite, "cleanup_bucket", remove_bucket)
    monkeypatch.setattr(endpoint.time, "sleep", lambda seconds: None)
    try:
        if failures < expected_attempts:
            suite.teardown()
            assert not suite.data_retained()
            assert not suite.report["cleanup_errors"]
        else:
            with pytest.raises(AssertionError, match="temporary connection failure"):
                suite.teardown()
            assert suite.owned_buckets == {failing}
            assert set(suite.report["cleanup_errors"]) == {failing}
        assert visited[:len(buckets)] == buckets
        assert visited.count(failing) == expected_attempts
        assert all(visited.count(bucket) == 1 for bucket in buckets[1:])
        assert all(not (suite.directory / name).exists() for name in LOCAL_DATA_DIRECTORIES)
    finally:
        for client in suite.clients:
            client.close()


def test_local_cleanup_failure_is_reported_as_retained_data(local_runner, monkeypatch):
    def cannot_remove_local(self):
        raise OSError("local payload removal failed")

    monkeypatch.setattr(endpoint.EndpointSuite, "cleanup_data", cannot_remove_local)
    status, suite, report = local_runner["run"]()
    assert status == 1 and report["status"] == "FAIL"
    assert not suite.owned_buckets and not suite.uncertain_buckets
    assert suite.payloads.exists()
    assert report["test_data_retained"] is True
    assert "local payload removal failed" in report["local_cleanup_error"]
    cleanup = [r for r in report["results"] if r["name"] == "Remove test buckets"]
    assert len(cleanup) == 1 and cleanup[0]["status"] == "FAIL"
