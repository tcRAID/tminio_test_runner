"""Config comments must not break restoration or hide ineffective resize tests."""
import pytest

from tminio_test_runner.admin_config import parse_config


def test_config_environment_comments_and_quoted_hash():
    output = '# MINIO_DRIVE_NEWEST_POOL_MIN_FREE=99\ndrive max_timeout=30s newest_pool_min_free=15 comment="kept # value"\n'
    expected = ["drive", "max_timeout=30s", "newest_pool_min_free=15", "comment=kept # value"]
    assert parse_config(output, "drive", mutable_env={"MINIO_DRIVE_NEWEST_POOL_MIN_FREE": "99"}) == expected
    assert parse_config(output, "drive") == expected


def test_config_preserves_unquoted_hash_in_persisted_value():
    assert parse_config("# MINIO_SCANNER_SPEED=fastest\nilm comment=release#1", "ilm") == [
        "ilm", "comment=release#1"]


@pytest.mark.parametrize("name,value,required,subsystem", [
    ("MINIO_DRIVE_NEWEST_POOL_MIN_FREE", "15", "99", "drive"),
    ("MINIO_ILM_EXPIRATION_WORKERS", "100", None, "ilm"),
])
def test_config_rejects_environment_that_prevents_the_test(name, value, required, subsystem):
    with pytest.raises(AssertionError, match="overrides dynamic configuration"):
        parse_config(f"# {name}={value}\n{subsystem} comment=", subsystem, mutable_env={name: required})


@pytest.mark.parametrize("output", ["# only annotations", "ilm comment=", "drive timeout=30s\nilm comment="])
def test_config_rejects_missing_or_wrong_subsystem(output):
    with pytest.raises(AssertionError):
        parse_config(output, "drive")
