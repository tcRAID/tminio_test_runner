"""Offline guards for new placement/grace fixtures and false-pass prevention."""
import json
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from tminio_test_runner.admin_config import effective_value
from tminio_test_runner.helpers import assert_missing
from tminio_test_runner.pool_layout import changed_pool, metadata_snapshot, newest_pool_min_free
from tests.system.test_replication import _replica_marker_headers


def test_metadata_evidence_identifies_one_writer_and_rejects_missing_or_ambiguous_layout(tmp_path):
    roots = [[str(tmp_path / "p0")], [str(tmp_path / "p1")]]
    file = tmp_path / "p0" / "bucket" / "key" / "xl.meta"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"first version")
    before = metadata_snapshot(roots, "bucket", "key")
    assert set(before) == {0}
    file.write_bytes(b"second version")
    after = metadata_snapshot(roots, "bucket", "key")
    assert changed_pool(before, after) == 0
    for initial, updated in (({}, {}), (before, before), ({}, {0: ("one",), 1: ("two",)})):
        with pytest.raises(AssertionError, match="one metadata writer"):
            changed_pool(initial, updated)


def test_placement_configuration_restores_on_fixture_failure(tmp_path):
    current = "15"
    calls = []
    def run(*args):
        nonlocal current
        calls.append(args)
        if args[:3] == ("admin", "config", "set"):
            current = next(part.split("=", 1)[1] for part in args if part.startswith("newest_pool_min_free="))
            return ""
        return "drive newest_pool_min_free=" + current
    with pytest.raises(RuntimeError, match="fixture failed"):
        with newest_pool_min_free(SimpleNamespace(run=run), tmp_path, 99):
            journal = json.loads((tmp_path / "pending-admin-restore.json").read_text())
            assert journal == {"subsystem": "drive", "arguments": ["newest_pool_min_free=15"]}
            assert current == "99"
            raise RuntimeError("fixture failed")
    assert current == "15"
    assert not (tmp_path / "pending-admin-restore.json").exists()


def test_fixed_environment_cannot_fake_both_placement_branches(tmp_path):
    calls = []
    def run(*args):
        calls.append(args)
        return "# MINIO_DRIVE_NEWEST_POOL_MIN_FREE=99\ndrive newest_pool_min_free=15"
    with pytest.raises(AssertionError, match="overrides dynamic"):
        with newest_pool_min_free(SimpleNamespace(run=run), tmp_path, 1):
            pytest.fail("invalid fixture was accepted")
    assert len(calls) == 1
    assert not (tmp_path / "pending-admin-restore.json").exists()


def test_grace_config_uses_environment_precedence_and_requires_key():
    setting = "replica_delete_marker_grace_days"
    env = "MINIO_ILM_REPLICA_DELETE_MARKER_GRACE_DAYS"
    assert effective_value(f"ilm {setting}=2", "ilm", setting, env) == "2"
    assert effective_value(f"# {env}=7\nilm {setting}=2", "ilm", setting, env) == "7"
    with pytest.raises(AssertionError, match="omitted"):
        effective_value("ilm expiration_workers=10", "ilm", setting, env)


@pytest.mark.parametrize("value", [0, 100, -1, True])
def test_invalid_pool_reserve_never_mutates_server(tmp_path, value):
    def forbidden(*args):
        pytest.fail("invalid reserve reached the server")
    with pytest.raises(AssertionError, match="1..99"):
        with newest_pool_min_free(SimpleNamespace(run=forbidden), tmp_path, value):
            pytest.fail("invalid reserve was accepted")


@pytest.mark.parametrize("status,version,replica,marker", [
    (403, "expected", "REPLICA", "true"),
    (404, "wrong", "REPLICA", "true"),
    (404, "expected", "COMPLETED", "true"),
    (404, "expected", "REPLICA", "false"),
])
def test_grace_oracle_rejects_non_replica_and_wrong_markers(status, version, replica, marker):
    def head_object(**kwargs):
        raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {
            "HTTPStatusCode": status, "HTTPHeaders": {"x-amz-version-id": version,
            "x-amz-delete-marker": marker, "x-minio-replication-deletemarker-status": replica}}}, "HeadObject")
    with pytest.raises(AssertionError):
        _replica_marker_headers(SimpleNamespace(head_object=head_object), "bucket", "key", "expected")


def test_missing_bucket_never_counts_as_successful_object_expiry():
    def head_object(**kwargs):
        raise ClientError({"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
    with pytest.raises(AssertionError):
        assert_missing(SimpleNamespace(head_object=head_object), "bucket", "key")
