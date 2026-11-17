"""Live-cluster fixtures, collected only by explicit system-test selections."""
import json
import os
from pathlib import Path
import shutil
import uuid

import pytest
from tminio_test_runner.helpers import make_client
from tminio_test_runner.mc import Mc


@pytest.fixture(scope="session")
def settings(pytestconfig):
    config_path = pytestconfig.getoption("--config")
    if not config_path:
        pytest.fail("--integration requires --config config.local.json", pytrace=False)
    data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    groups = pytestconfig.getoption("--release-groups")
    if groups and "replication" not in groups.split(","):
        # mc initializes aliases for configured endpoints. A source-only subset
        # must not require or contact an unrelated target cluster.
        data.pop("target", None)
    if not data.get("source", {}).get("endpoint"):
        pytest.fail("configuration needs source.endpoint", pytrace=False)
    assert 1 <= int(data.get("workers", 16)) <= 128
    assert int(data.get("rounds", 32)) > 0
    assert 1 <= float(data.get("request_timeout", 20)) <= 120
    data["_run_id"] = uuid.uuid4().hex[:12]
    # Record topology expectations, not credentials or arbitrary hook arguments.
    evidence = {key: data[key] for key in ("workers", "rounds", "expected_pools", "expected_nodes", "expected_data", "expected_parity") if key in data}
    evidence["run_id"] = data["_run_id"]
    (pytestconfig.evidence_dir / "run.json").write_text(json.dumps(evidence, indent=2))
    return data




@pytest.fixture(scope="session")
def src(settings):
    client = make_client(settings["source"], settings.get("request_timeout", 20), settings.get("workers", 16))
    yield client
    client.close()


@pytest.fixture(scope="session")
def dst(settings):
    if not settings.get("target"):
        pytest.skip("second independent cluster target is not configured")
    client = make_client(settings["target"], settings.get("request_timeout", 20), settings.get("workers", 16))
    yield client
    client.close()


@pytest.fixture
def bucket_factory(src, settings, artifacts):
    from botocore.exceptions import ClientError
    from tminio_test_runner.lab_cleanup import LabBuckets
    registry = LabBuckets(artifacts, settings["_run_id"])
    def create(client=None, object_lock=False):
        client = client or src
        prefix = settings.get("bucket_prefix", "dev-release-")
        if not prefix.startswith("dev-release-") or len(prefix) > 25:
            raise ValueError("bucket_prefix must start with dev-release- and have at most 25 characters")
        name = prefix + settings["_run_id"] + "-" + uuid.uuid4().hex[:10]
        try:
            client.head_bucket(Bucket=name)
        except ClientError as error:
            assert error.response["ResponseMetadata"]["HTTPStatusCode"] == 404, "cannot establish bucket absence"
        else:
            raise AssertionError("generated lab bucket exists; refusing to use it")
        entry = registry.record(client, name, object_lock)
        args = {"Bucket": name}
        if object_lock:
            args["ObjectLockEnabledForBucket"] = True
        try:
            client.create_bucket(**args)
        except ClientError as error:
            if error.response["Error"]["Code"] in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                registry.entries.remove(entry)
                registry.save()
            raise
        client.put_bucket_tagging(Bucket=name, Tagging={"TagSet": [{"Key": "tminio-check-owner", "Value": registry.token}]})
        entry["tagged"] = True
        client.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
        assert client.get_bucket_versioning(Bucket=name)["Status"] == "Enabled"
        with (artifacts / "buckets.jsonl").open("a") as out:
            out.write(json.dumps({"bucket": name, "endpoint": client.meta.endpoint_url}) + "\n")
        return name
    try:
        yield create
    finally:
        registry.cleanup()




@pytest.fixture(scope="session")
def mc(settings, artifacts):
    executable = shutil.which(settings.get("mc_path", "mc"))
    if not executable:
        pytest.skip("mc executable is required for admin/replication setup; set mc_path")
    directory = artifacts / "mc-private"
    directory.mkdir(mode=0o700)
    cli = Mc(executable, directory, settings)
    try:
        for alias, key in (("src", "source"), ("dst", "target")):
            spec = settings.get(key)
            if spec and isinstance(spec.get("verify_tls"), str):
                ca_dir = directory / "certs" / "CAs"
                ca_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(spec["verify_tls"], ca_dir / (alias + ".crt"))
        for alias, key in (("src", "source"), ("dst", "target")):
            spec = settings.get(key)
            if spec:
                cli.run("alias", "set", alias, spec["endpoint"],
                        os.environ[spec.get("access_key_env", "TMINIO_TEST_ACCESS_KEY")],
                        os.environ[spec.get("secret_key_env", "TMINIO_TEST_SECRET_KEY")], "--api", "S3v4")
        yield cli
    finally:
        # Only remove the exact private directory created for this session.
        if directory.is_dir() and directory.parent == artifacts:
            shutil.rmtree(directory)
