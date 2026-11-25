"""Soak coverage, bounded-resource and command-line regressions; no network."""
import json
import io
import signal
import threading
from types import SimpleNamespace

import pytest

from tminio_test_runner import endpoint_check, soak_cases


@pytest.mark.parametrize("workers", ["0", "-1", "129", "many"])
def test_invalid_workers_are_rejected_before_credentials_or_network(monkeypatch, workers):
    def forbidden():
        raise AssertionError("invalid arguments reached credential lookup")
    monkeypatch.setattr(endpoint_check, "credentials", forbidden)
    with pytest.raises(SystemExit) as error:
        endpoint_check.main(["http://example.invalid:9000", "--workers", workers], soak=True)
    assert error.value.code == 2


@pytest.mark.parametrize("arguments,expected_timeout,expected_workers", [
    ([], 1800, 32),
    (["--duration", "3600"], 4200, 32),
    (["--duration", "86400", "--workers", "64"], 87000, 64),
    (["--duration", "3600", "--timeout", "5000", "--workers", "1"], 5000, 1),
    (["--workers", "128"], 1800, 128),
])
def test_soak_deadline_scales_without_extra_user_settings(
        monkeypatch, tmp_path, arguments, expected_timeout, expected_workers):
    # Stop at health, before any endpoint operation. Retain real parsing,
    # configuration, reporting and automatic local teardown.
    created = []
    original = endpoint_check.EndpointSuite
    monkeypatch.setattr(original, "make_client", lambda self: SimpleNamespace(close=lambda: None))
    def factory(*args, **kwargs):
        suite = original(*args, **kwargs)
        def no_network():
            raise RuntimeError("intentional health failure in unit test")
        suite.cluster_health = no_network
        created.append(suite)
        return suite
    monkeypatch.setattr(endpoint_check, "EndpointSuite", factory)
    monkeypatch.setattr(endpoint_check, "credentials", lambda: ("test-access", "test-secret"))
    assert endpoint_check.main(["http://example.invalid:9000", "--work-dir", str(tmp_path), *arguments], soak=True) == 1
    suite, = created
    assert suite.timeout == expected_timeout
    assert suite.soak_workers == expected_workers
    assert not suite.data_retained()
    report = json.loads((suite.directory / "results.json").read_text())
    assert report["timeout_seconds"] == expected_timeout
    assert report["soak"]["workers"] == expected_workers
    assert report["object_sizes"] == list(soak_cases.OBJECT_SIZES)
    assert "listing_keys" not in report
    assert "multipart_bytes" not in report


@pytest.mark.parametrize("arguments", [
    ["--duration", "0"],
    ["--duration", "-1"],
    ["--duration", "3600", "--timeout", "3600"],
    ["--timeout", "0"],
])
def test_invalid_soak_deadlines_are_rejected_before_network(monkeypatch, arguments):
    monkeypatch.setattr(endpoint_check, "credentials", lambda: pytest.fail("invalid timing reached credentials"))
    with pytest.raises(SystemExit) as error:
        endpoint_check.main(["http://example.invalid:9000", *arguments], soak=True)
    assert error.value.code == 2


def test_request_log_rotation_keeps_valid_recent_events_and_bounds_disk(tmp_path):
    from tminio_test_runner.request_log import append_event
    path = tmp_path / "requests.jsonl"
    rotations = sum(append_event(path, {"sequence": index, "payload": "x" * 40}, 256, 3)
                    for index in range(100))
    files = list(tmp_path.glob("requests.jsonl*"))
    assert rotations > 3
    assert len(files) == 4
    assert all(item.stat().st_size <= 256 for item in files)
    events = [json.loads(line) for item in files for line in item.read_text().splitlines()]
    sequence = [event["sequence"] for event in events]
    assert len(sequence) == len(set(sequence))
    assert max(sequence) == 99
    assert min(sequence) > 0


def test_rotating_logs_preserve_attempted_keys_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoint_check.EndpointSuite, "make_client", lambda self: SimpleNamespace(close=lambda: None))
    suite = endpoint_check.EndpointSuite("http://example.invalid:9000", "private-access", "private-secret", tmp_path)
    try:
        soak_cases.configure_soak(suite, 300, 1)
        suite.request_log_limit = 1024
        suite.owned_buckets.add(suite.bucket)
        for index in range(100):
            suite.event("mutation", bucket=suite.bucket, key=f"attempt-{index}",
                        operation="put_object", diagnostic="private-access private-secret")
        logs = list(suite.directory.glob("requests.jsonl*"))
        assert 1 < len(logs) <= 4
        text = "".join(path.read_text() for path in logs)
        assert "private-access" not in text and "private-secret" not in text
        assert len(suite.known_keys) == 100
        assert (suite.bucket, "attempt-0") in suite.known_keys
        assert "attempt-0" not in text
    finally:
        suite.cleanup_data()


class StreamingBytes(io.BytesIO):
    """Reject unbounded reads so tests detect accidentally buffering downloads."""
    def read(self, size=-1):
        assert size > 0, "soak download must request bounded chunks"
        return super().read(size)


class MemorySoak:
    """A version-aware in-memory endpoint, with optional request corruption."""
    def __init__(self, fail_operation=None, corrupt=False, corrupt_multipart=False):
        self.state_lock = threading.RLock()
        self.report = {"not_covered": []}
        self.known_keys, self.known_versions, self.known_uploads = set(), set(), set()
        self.versions, self.uploads, self.tags, self.metadata = {}, {}, {}, {}
        self.parts, self.upload_metadata = {}, {}
        self.corrupt_multipart = corrupt_multipart
        self.operations, self.put_keys, self.put_sizes = [], [], []
        self.max_versions = self.max_uploads = self.serial = self.completed = self.saved = 0
        self.fail_operation, self.corrupt = fail_operation, corrupt

    def extra_bucket(self, _):
        return "unit-soak"

    def enable_versioning(self, _):
        pass

    def make_client(self):
        return object()

    def save(self):
        self.saved += 1

    def event(self, *args, **kwargs):
        pass

    def redact(self, value):
        return str(value)

    def health(self):
        pass

    def history(self, _):
        return list(self.versions)

    def cleanup_uploads(self, _):
        return list(self.uploads)

    def missing(self, bucket, key, **_):
        matches = [(vid, body) for vid, (name, body) in self.versions.items() if name == key]
        assert matches and matches[-1][1] is None

    def call(self, operation, *, client=None, Bucket, Key, **kwargs):
        self.operations.append(operation)
        self.known_keys.add((Bucket, Key))
        if operation == self.fail_operation:
            raise RuntimeError("injected " + operation + " failure")
        if operation in ("put_object", "copy_object") or (operation == "delete_object" and "VersionId" not in kwargs):
            self.serial += 1
            version = str(self.serial)
            if operation == "put_object":
                body = kwargs["Body"]
                self.metadata[version] = kwargs["Metadata"]
                self.put_keys.append(Key)
                self.put_sizes.append(len(body))
            elif operation == "copy_object":
                body = self.versions[kwargs["CopySource"]["VersionId"]][1]
                self.metadata[version] = self.metadata[kwargs["CopySource"]["VersionId"]]
            else:
                body = None
            self.versions[version] = (Key, body)
            self.known_versions.add((Bucket, Key, version))
            self.max_versions = max(self.max_versions, len(self.versions))
            return {"VersionId": version}
        if operation == "put_object_tagging":
            self.tags[kwargs["VersionId"]] = kwargs["Tagging"]
            return {}
        if operation == "get_object_tagging":
            return self.tags[kwargs["VersionId"]]
        if operation in ("get_object", "head_object"):
            body = self.versions[kwargs["VersionId"]][1]
            result = {"ContentLength": len(body), "Metadata": self.metadata[kwargs["VersionId"]]}
            if operation == "get_object":
                result["Body"] = StreamingBytes(bytes([body[0] ^ 255]) + body[1:] if self.corrupt else body)
            return result
        if operation == "delete_object":
            version = kwargs["VersionId"]
            del self.versions[version]
            if not self.versions:
                self.completed += 1
            return {"VersionId": version}
        if operation == "create_multipart_upload":
            self.serial += 1
            upload = str(self.serial)
            self.uploads[upload] = Key
            self.upload_metadata[upload] = kwargs.get("Metadata", {})
            self.known_uploads.add((Bucket, Key, upload))
            self.max_uploads = max(self.max_uploads, len(self.uploads))
            return {"UploadId": upload}
        if operation == "upload_part":
            assert kwargs["UploadId"] in self.uploads
            self.parts[kwargs["UploadId"]] = kwargs["Body"]
            return {"ETag": "part"}
        if operation == "complete_multipart_upload":
            upload = kwargs["UploadId"]
            assert self.uploads[upload] == Key
            assert kwargs["MultipartUpload"] == {"Parts": [{"PartNumber": 1, "ETag": "part"}]}
            self.serial += 1
            version = str(self.serial)
            self.versions[version] = (Key, self.parts.pop(upload))
            self.metadata[version] = self.upload_metadata.pop(upload)
            self.known_versions.add((Bucket, Key, version))
            self.max_versions = max(self.max_versions, len(self.versions))
            del self.uploads[upload]
            if self.corrupt_multipart:
                self.corrupt = True
            return {"VersionId": version}
        if operation == "abort_multipart_upload":
            self.uploads.pop(kwargs["UploadId"], None)
            self.parts.pop(kwargs["UploadId"], None)
            self.upload_metadata.pop(kwargs["UploadId"], None)
            return {}
        raise AssertionError("unexpected operation " + operation)


def run_memory_soak(monkeypatch, suite, cycles):
    # A logical clock avoids sleeps and ends only after the chosen number of
    # complete cycles. Leave threading's real monotonic clock untouched.
    monkeypatch.setattr(soak_cases, "time", SimpleNamespace(monotonic=lambda: 100 if suite.completed < cycles else 10000))
    soak_cases.configure_soak(suite, 300, 1)
    soak_cases.bounded_soak(suite)


def test_soak_uses_every_object_size_and_cleans_versions_uploads(monkeypatch):
    suite = MemorySoak()
    run_memory_soak(monkeypatch, suite, 3)
    assert suite.put_sizes == list(soak_cases.OBJECT_SIZES)
    assert not suite.versions and not suite.uploads
    assert not suite.known_versions and not suite.known_uploads
    assert suite.max_versions == 4 and suite.max_uploads == 1
    assert suite.report["soak"]["cycles_per_worker"] == [3]
    assert suite.report["soak"]["cycles_per_size"] == {str(size): 1 for size in soak_cases.OBJECT_SIZES}
    assert suite.report["soak"]["bytes_verified"] == 3 * sum(soak_cases.OBJECT_SIZES)
    assert suite.operations.count("complete_multipart_upload") == 3
    assert suite.operations.count("get_object") == 9
    assert "abort_multipart_upload" not in suite.operations
    assert suite.report["soak"]["state"] == "finished"


def test_key_rotation_is_bounded_and_reuses_slots(monkeypatch):
    monkeypatch.setattr(soak_cases, "OBJECT_SIZES", (257, 1025, 4097))
    suite = MemorySoak()
    run_memory_soak(monkeypatch, suite, soak_cases.KEY_SLOTS + 3)
    assert len(set(suite.put_keys)) == soak_cases.KEY_SLOTS
    assert suite.put_keys[:3] == suite.put_keys[soak_cases.KEY_SLOTS:]
    assert len(suite.known_keys) == soak_cases.KEY_SLOTS * 2
    assert not suite.versions and not suite.uploads


@pytest.mark.parametrize("failure", ["checksum", "upload_part", "complete_multipart_upload", "multipart_checksum"])
def test_soak_failure_preserves_partial_statistics_and_removes_cycle_data(monkeypatch, failure):
    failed_upload = failure in ("upload_part", "complete_multipart_upload")
    suite = MemorySoak(fail_operation=failure if failed_upload else None,
                       corrupt=failure == "checksum", corrupt_multipart=failure == "multipart_checksum")
    with pytest.raises((AssertionError, RuntimeError), match="checksum|upload_part|complete_multipart_upload"):
        run_memory_soak(monkeypatch, suite, 3)
    assert suite.operations.count("abort_multipart_upload") == int(failed_upload)
    assert not suite.versions and not suite.uploads
    assert not suite.known_versions and not suite.known_uploads
    assert suite.report["soak"]["cycles_per_worker"] == [0]
    assert suite.saved > 0
    assert suite.report["soak"]["state"] == "interrupted or failed"


def test_failed_worker_allows_sibling_to_finish_started_cycle_before_stopping(monkeypatch):
    stop, sibling_started = threading.Event(), threading.Event()
    class SiblingSoak(MemorySoak):
        def __init__(self):
            super().__init__()
            self.client_count, self.sibling_operations = 0, []

        def make_client(self):
            index = self.client_count
            self.client_count += 1
            return SimpleNamespace(index=index)

        def call(self, operation, *, client=None, **kwargs):
            if client.index == 0 and operation == "get_object_tagging":
                raise RuntimeError("worker zero failed")
            with self.state_lock:
                if client.index == 1:
                    self.sibling_operations.append(operation)
                response = super().call(operation, client=client, **kwargs)
            if operation == "put_object":
                if client.index == 0:
                    assert sibling_started.wait(3), "sibling never started"
                else:
                    sibling_started.set()
                    assert stop.wait(3), "failure did not signal stop"
            return response
    suite = SiblingSoak()
    monkeypatch.setattr(soak_cases, "threading", SimpleNamespace(
        Event=lambda: stop, current_thread=threading.current_thread, main_thread=threading.main_thread))
    soak_cases.configure_soak(suite, 300, 2)
    with pytest.raises(RuntimeError, match="worker zero failed"):
        soak_cases.bounded_soak(suite)
    assert suite.sibling_operations.count("put_object") == 1
    assert suite.sibling_operations.count("complete_multipart_upload") == 1
    assert "abort_multipart_upload" not in suite.sibling_operations
    assert not suite.versions and not suite.uploads and not suite.known_versions
    assert suite.report["soak"]["cycles_per_worker"] == [0, 1]


def test_upload_failure_signals_stop_before_waiting_for_abort_cleanup(monkeypatch):
    stop, sibling_started, sibling_finished = threading.Event(), threading.Event(), threading.Event()

    class SlowCleanupSoak(MemorySoak):
        def __init__(self):
            super().__init__()
            self.client_count, self.sibling_puts, self.aborts = 0, 0, []

        def make_client(self):
            index = self.client_count
            self.client_count += 1
            return SimpleNamespace(index=index)

        def call(self, operation, *, client=None, **kwargs):
            if client.index == 0 and operation == "upload_part":
                assert sibling_started.wait(3), "sibling never started"
                raise RuntimeError("worker zero part failed")
            if operation == "abort_multipart_upload":
                self.aborts.append(client.index)
                assert stop.is_set(), "failed upload cleanup began before stopping new cycles"
                assert sibling_finished.wait(3), "started sibling cycle could not finish during cleanup"
            with self.state_lock:
                response = super().call(operation, client=client, **kwargs)
                if client.index == 1 and operation == "put_object":
                    self.sibling_puts += 1
                if (client.index == 1 and operation == "delete_object" and "VersionId" in kwargs
                        and not any(name.startswith("worker-1/") for name, _ in self.versions.values())):
                    sibling_finished.set()
            if client.index == 1 and operation == "put_object":
                sibling_started.set()
                assert stop.wait(3), "failure did not stop new cycles before cleanup"
            return response

    suite = SlowCleanupSoak()
    monkeypatch.setattr(soak_cases, "threading", SimpleNamespace(
        Event=lambda: stop, current_thread=threading.current_thread, main_thread=threading.main_thread))
    soak_cases.configure_soak(suite, 300, 2)
    with pytest.raises(RuntimeError, match="worker zero part failed"):
        soak_cases.bounded_soak(suite)
    assert suite.sibling_puts == 1 and suite.aborts == [0]
    assert not suite.versions and not suite.uploads
    assert suite.report["soak"]["cycles_per_worker"] == [0, 1]


@pytest.mark.parametrize("operation", ["put_object", "create_multipart_upload", "complete_multipart_upload"])
def test_lost_mutation_response_retains_key_for_global_teardown(monkeypatch, operation):
    class LostResponseSoak(MemorySoak):
        def call(self, method, *, client=None, Bucket, Key, **kwargs):
            response = super().call(method, client=client, Bucket=Bucket, Key=Key, **kwargs)
            if method == operation:
                # The server accepted the mutation, but the client cannot know
                # its returned version/upload ID. Ownership comes from key intent.
                if method in ("put_object", "complete_multipart_upload"):
                    self.known_versions.discard((Bucket, Key, response["VersionId"]))
                    if method == "complete_multipart_upload":
                        self.known_uploads.discard((Bucket, Key, kwargs["UploadId"]))
                else:
                    self.known_uploads.discard((Bucket, Key, response["UploadId"]))
                raise OSError("injected lost response")
            return response
    suite = LostResponseSoak()
    with pytest.raises(OSError, match="lost response"):
        run_memory_soak(monkeypatch, suite, 3)
    assert not suite.known_versions and not suite.known_uploads
    remaining_keys = {name for name, _ in suite.versions.values()} | set(suite.uploads.values())
    assert remaining_keys
    assert all(("unit-soak", key) in suite.known_keys for key in remaining_keys)
    assert suite.report["soak"]["state"] == "interrupted or failed"


@pytest.mark.parametrize("repeat_signal", [None, signal.SIGINT, signal.SIGTERM, signal.SIGALRM])
@pytest.mark.parametrize("inflight_operation", ["put_object", "upload_part"])
def test_controller_interruption_finishes_cycle_before_cleanup(monkeypatch, repeat_signal, inflight_operation):
    stop, started = threading.Event(), threading.Event()
    class InterruptedSoak(MemorySoak):
        def call(self, operation, **kwargs):
            response = super().call(operation, **kwargs)
            if operation == inflight_operation:
                started.set()
                assert stop.wait(3), "controller failed to stop active worker"
                if repeat_signal is not None:
                    # Arrives while the controller is draining this request.
                    # A raising handler would escape Thread.join prematurely.
                    signal.raise_signal(repeat_signal)
            return response
    def interrupt_wait(*args, **kwargs):
        assert started.wait(3), "worker never started"
        raise KeyboardInterrupt
    suite = InterruptedSoak()
    monkeypatch.setattr(soak_cases, "threading", SimpleNamespace(
        Event=lambda: stop, current_thread=threading.current_thread, main_thread=threading.main_thread))
    monkeypatch.setattr(soak_cases, "wait", interrupt_wait)
    soak_cases.configure_soak(suite, 300, 1)
    old_handler = signal.getsignal(repeat_signal) if repeat_signal is not None else None
    def repeated_interrupt(*_):
        raise RuntimeError("second signal interrupted worker drain")
    try:
        if repeat_signal is not None:
            signal.signal(repeat_signal, repeated_interrupt)
        with pytest.raises(KeyboardInterrupt):
            soak_cases.bounded_soak(suite)
        if repeat_signal is not None:
            assert signal.getsignal(repeat_signal) is repeated_interrupt
    finally:
        if repeat_signal is not None:
            signal.signal(repeat_signal, old_handler)
    assert suite.operations.count("put_object") == 1
    assert suite.operations.count("complete_multipart_upload") == 1
    assert suite.operations.count("get_object") == 3
    assert "abort_multipart_upload" not in suite.operations
    assert not suite.versions and not suite.known_versions and not suite.uploads
    assert suite.report["soak"]["cycles_per_worker"] == [1]
    assert suite.report["soak"]["state"] == "interrupted or failed"


def test_duration_expiring_during_part_upload_still_completes_and_verifies(monkeypatch):
    class ExpiringSoak(MemorySoak):
        now = 100
        def call(self, operation, **kwargs):
            result = super().call(operation, **kwargs)
            if operation == "upload_part":
                self.now = 10000
            return result
    suite = ExpiringSoak()
    monkeypatch.setattr(soak_cases, "time", SimpleNamespace(monotonic=lambda: suite.now))
    soak_cases.configure_soak(suite, 300, 1)
    soak_cases.bounded_soak(suite)
    assert suite.operations.count("complete_multipart_upload") == 1
    assert suite.operations.count("get_object") == 3
    assert "abort_multipart_upload" not in suite.operations
    assert not suite.versions and not suite.uploads
    assert suite.report["soak"]["cycles_per_worker"] == [1]
    assert suite.report["soak"]["state"] == "finished"


def test_cycle_cleanup_failure_still_attempts_other_versions(monkeypatch):
    class CleanupFailureSoak(MemorySoak):
        def __init__(self):
            super().__init__()
            self.deleted_versions = []

        def call(self, operation, **kwargs):
            if operation == "delete_object" and "VersionId" in kwargs:
                self.deleted_versions.append(kwargs["VersionId"])
                if len(self.deleted_versions) == 1:
                    raise RuntimeError("injected first delete failure")
            return super().call(operation, **kwargs)
    suite = CleanupFailureSoak()
    with pytest.raises(AssertionError, match="cleanup failed"):
        run_memory_soak(monkeypatch, suite, 3)
    assert len(suite.deleted_versions) == 4
    assert len(suite.versions) == len(suite.known_versions) == 1
    assert not suite.uploads
    assert suite.report["soak"]["cycles_per_worker"] == [0]
