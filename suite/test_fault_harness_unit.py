"""Offline fault harness regressions; these do not qualify a live cluster."""

from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

import test_faults as faults


def test_read_timeout_reporting_does_not_abort_traffic(monkeypatch):
    endpoint = "https://private-user:private-secret@private-endpoint.test"
    timeout = ReadTimeoutError(endpoint_url=endpoint)
    assert timeout.response is None  # Botocore's actual timeout representation.
    seed = {"key": "oracle/seed", "version": "seed-version"}
    written = {"key": "traffic/written", "version": "accepted-version"}
    verified = []

    def read(client, bucket, record):
        if record is seed:
            raise timeout
        verified.append(record)

    monkeypatch.setattr(faults, "assert_version", read)
    monkeypatch.setattr(faults, "put_version", lambda *args, **kwargs: written)
    traffic = faults._Traffic(object(), "bucket", [seed], "quorum_loss", 1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(traffic.worker, worker) for worker in range(2)]
        for future in futures:
            future.result(timeout=5)

    assert traffic.errors == [{"operation": "get_seed", "type": "ReadTimeoutError",
                               "code": "", "http_status": None}] * 2
    assert traffic.unexpected == []
    assert traffic.acknowledged == [written, written]
    assert verified == [written, written]
    assert traffic.successful_operations == 4
    serialized = json.dumps(traffic.errors)
    assert "private" not in serialized


@pytest.mark.parametrize("response", [None, [], {"Error": None, "ResponseMetadata": None}])
def test_error_summary_handles_optional_or_malformed_response(response):
    error = RuntimeError("sensitive endpoint text")
    error.response = response
    assert faults._error_summary("admin_info", error) == {
        "operation": "admin_info", "type": "RuntimeError", "code": "", "http_status": None,
    }


def test_error_summary_keeps_s3_failure_code_and_status_only():
    error = ClientError({"Error": {"Code": "SlowDownRead", "Message": "sensitive message"},
                         "ResponseMetadata": {"HTTPStatusCode": 503, "HostId": "private host"}},
                        "GetObject")
    assert faults._error_summary("get_seed", error) == {
        "operation": "get_seed", "type": "ClientError", "code": "SlowDownRead", "http_status": 503,
    }
    assert faults._availability_error(error)
