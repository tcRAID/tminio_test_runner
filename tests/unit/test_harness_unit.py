"""Local checks of the test harness; these do not qualify a MinIO release."""
from __future__ import annotations

import io
import json
import subprocess
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from tminio_test_runner import helpers
def _fault_settings():
    return {"faults": {"node_down": ["lab-only-down"], "node_up": ["lab-only-up"]}}


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_fault_intent_survives_failed_injection_and_failed_recovery(tmp_path, monkeypatch, failure):
    settings = _fault_settings()
    state_path = tmp_path / "pending-recovery.json"
    expected = {"node_down": "node_up"}

    def failed_down(argv, **kwargs):
        assert argv == ["lab-only-down"]
        # The recovery record must exist before the command can partly fail.
        assert json.loads(state_path.read_text()) == expected
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout=1)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(helpers, "run_controlled", failed_down)
    with pytest.raises((AssertionError, subprocess.TimeoutExpired)):
        helpers.run_hook(settings, "node_down", tmp_path)
    assert json.loads(state_path.read_text()) == expected

    def failed_up(argv, **kwargs):
        assert argv == ["lab-only-up"]
        assert json.loads(state_path.read_text()) == expected
        return SimpleNamespace(returncode=9)

    monkeypatch.setattr(helpers, "run_controlled", failed_up)
    with pytest.raises(AssertionError):
        helpers.run_hook(settings, "node_up", tmp_path)
    assert json.loads(state_path.read_text()) == expected

    def recovered(argv, **kwargs):
        assert argv == ["lab-only-up"]
        assert json.loads(state_path.read_text()) == expected
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(helpers, "run_controlled", recovered)
    helpers.run_hook(settings, "node_up", tmp_path)
    assert json.loads(state_path.read_text()) == {}


def test_fault_without_recovery_never_executes(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("fault was executed without a configured recovery")

    monkeypatch.setattr(helpers, "run_controlled", forbidden)
    with pytest.raises(ValueError, match="missing recovery"):
        helpers.run_hook({"faults": {"node_down": ["lab-only-down"]}}, "node_down", tmp_path)


@pytest.mark.parametrize("status", [403, 500, 503])
def test_missing_object_assertion_rejects_permission_and_server_errors(status):
    def head_object(**kwargs):
        raise ClientError({"Error": {"Code": "TestError"},
                           "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadObject")

    with pytest.raises(AssertionError):
        helpers.assert_missing(SimpleNamespace(head_object=head_object), "bucket", "key", "version")


def test_missing_object_assertion_requires_actual_absence():
    def absent(**kwargs):
        assert kwargs == {"Bucket": "bucket", "Key": "key", "VersionId": "version"}
        raise ClientError({"Error": {"Code": "NoSuchVersion"},
                           "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")

    helpers.assert_missing(SimpleNamespace(head_object=absent), "bucket", "key", "version")
    with pytest.raises(AssertionError, match="expected absent object"):
        helpers.assert_missing(SimpleNamespace(head_object=lambda **_: {}), "bucket", "key")


def test_version_assertion_rejects_corrupt_acknowledged_content_and_closes_body():
    stream = io.BytesIO(b"corrupted payload")
    record = {"key": "object", "version": "acknowledged-version", "size": len(b"expected payload"),
              "sha256": helpers.digest(b"expected payload"), "metadata": {}}

    def get_object(**kwargs):
        assert kwargs == {"Bucket": "bucket", "Key": "object", "VersionId": "acknowledged-version"}
        return {"Body": stream, "VersionId": "acknowledged-version", "Metadata": {}}

    def head_object(**kwargs):
        pytest.fail("content corruption must fail before accepting HEAD metadata")

    client = SimpleNamespace(get_object=get_object, head_object=head_object)
    with pytest.raises(AssertionError, match="corrupt version"):
        helpers.assert_version(client, "bucket", record)
    assert stream.closed


def test_version_listing_consumes_every_page_and_includes_markers():
    visited = []
    pages = [
        {"Versions": [{"Key": "obj", "VersionId": "v1"}], "IsTruncated": True},
        {"DeleteMarkers": [{"Key": "obj", "VersionId": "d1"}], "IsTruncated": True},
        {"Versions": [{"Key": "obj", "VersionId": "v2"}], "IsTruncated": False},
    ]

    def paginate(**kwargs):
        assert kwargs == {"Bucket": "bucket", "Prefix": "obj"}
        for index, page in enumerate(pages):
            visited.append(index)
            yield page

    def get_paginator(operation):
        assert operation == "list_object_versions"
        return SimpleNamespace(paginate=paginate)

    result = helpers.list_versions(SimpleNamespace(get_paginator=get_paginator), "bucket", "obj")
    assert visited == [0, 1, 2]
    assert [(item["VersionId"], item["IsDeleteMarker"]) for item in result] == [
        ("v1", False), ("d1", True), ("v2", False)]
    assert all("IsDeleteMarker" not in item for page in pages for item in page.get("Versions", []))
