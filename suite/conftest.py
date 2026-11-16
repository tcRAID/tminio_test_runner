from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from urllib.parse import quote, urlsplit, urlunsplit

import pytest

from helpers import make_client


def pytest_addoption(parser):
    parser.addoption("--integration", action="store_true", help="allow writes to fresh dev test buckets")
    parser.addoption("--run-faults", action="store_true", help="allow configured node/cluster fault hooks")
    parser.addoption("--run-admin", action="store_true", help="allow cluster-wide config changes on isolated dev")
    parser.addoption("--config", default=os.getenv("TMINIO_TEST_CONFIG"), help="JSON test configuration")
    parser.addoption("--artifacts", default=None, help="directory for this run's evidence")
    parser.addoption("--release-full", action="store_true", help="internal runner gate: no test deselection allowed")
    parser.addoption("--require-complete", action="store_true", help="internal runner gate: skipped/unexecuted tests fail the run")
    parser.addoption("--release-groups", default="", help="internal runner selection for scoped fixtures")


def pytest_configure(config):
    path = config.getoption("--artifacts")
    config.evidence_dir = Path(path or Path(__file__).parent / "artifacts" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])).resolve()
    config.evidence_dir.mkdir(parents=True, exist_ok=True)
    config.release_results = []
    config.release_deselected = 0


def pytest_deselected(items):
    if items:
        items[0].config.release_deselected += len(items)


def pytest_collection_modifyitems(config, items):
    groups = config.getoption("--release-groups")
    if groups and not config.getoption("--release-full"):
        from entrypoint import test_paths
        allowed = [path.split("::", 1) for path in test_paths(Path(__file__).resolve().parent, groups.split(","))]
        for item in items:
            function = item.nodeid.split("::", 1)[-1].split("[", 1)[0]
            if not any(Path(path[0]).resolve() == item.path.resolve() and (len(path) == 1 or path[1] == function) for path in allowed):
                raise pytest.UsageError("collected a test outside the selected release groups")
    for item in items:
        if item.get_closest_marker("integration") and not config.getoption("--integration"):
            item.add_marker(pytest.mark.skip(reason="requires --integration and a disposable dev endpoint"))
        if item.get_closest_marker("fault") and not config.getoption("--run-faults"):
            item.add_marker(pytest.mark.skip(reason="fault injection requires --run-faults and configured recovery hooks"))
        if item.get_closest_marker("admin") and not config.getoption("--run-admin"):
            item.add_marker(pytest.mark.skip(reason="cluster config changes require --run-admin on isolated dev"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" or report.failed or report.skipped:
        item.config.release_results.append({"test": report.nodeid, "phase": report.when,
                                           "outcome": report.outcome, "seconds": report.duration})
    if report.failed:
        text = report.longreprtext
        data = item.funcargs.get("settings", {})
        for spec in (data.get("source"), data.get("target")):
            if spec:
                for key, default in (("access_key_env", "TMINIO_TEST_ACCESS_KEY"), ("secret_key_env", "TMINIO_TEST_SECRET_KEY")):
                    secret = os.getenv(spec.get(key, default), "")
                    if secret:
                        text = text.replace(secret, "<redacted>")
        name = hashlib.sha256(report.nodeid.encode()).hexdigest()[:12]
        (item.config.evidence_dir / f"failure-{name}-{report.when}.txt").write_text(text, encoding="utf-8")


def pytest_sessionfinish(session, exitstatus):
    results = session.config.release_results
    expected = {item.nodeid for item in session.items}
    executed = {result["test"] for result in results if result["phase"] == "call"}
    complete = bool(expected) and executed == expected and not any(result["outcome"] != "passed" for result in results)
    full = session.config.getoption("--release-full")
    gate_errors = []
    if session.config.getoption("--require-complete") and not complete:
        gate_errors.append("selected tests contain skips, failures or unexecuted cases")
    if full and session.config.release_deselected:
        gate_errors.append("full run deselected tests")
    if gate_errors and int(exitstatus) == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    summary = {"pytest_exitstatus": int(session.exitstatus), "results": results,
               "collected": len(expected), "deselected": session.config.release_deselected,
               "gate_errors": gate_errors, "full_suite_completed": full and complete and not session.config.release_deselected and int(session.exitstatus) == 0,
               "skipped_are_not_passes": True,
               "selected_tests_passed": complete and int(session.exitstatus) == 0,
               "production_release_qualified": False}
    (session.config.evidence_dir / "results.json").write_text(json.dumps(summary, indent=2))


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
def artifacts(pytestconfig):
    return pytestconfig.evidence_dir


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
    from lab_cleanup import LabBuckets
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


class Mc:
    source_alias = "src"
    target_alias = "dst"

    def __init__(self, executable, config_dir, settings):
        self.executable, self.config_dir, self.settings = executable, config_dir, settings

    def replicate(self, source_bucket, target_bucket):
        target = self.settings["target"]
        parsed = urlsplit(target.get("replication_endpoint", target["endpoint"]))
        access = quote(os.environ[target.get("access_key_env", "TMINIO_TEST_ACCESS_KEY")], safe="")
        secret = quote(os.environ[target.get("secret_key_env", "TMINIO_TEST_SECRET_KEY")], safe="")
        remote = urlunsplit((parsed.scheme, f"{access}:{secret}@{parsed.netloc}", "/" + target_bucket, "", ""))
        return self.run("replicate", "add", "src/" + source_bucket, "--remote-bucket", remote,
                        "--replicate", "delete,delete-marker,existing-objects,replica-metadata-sync", "--priority", "1")

    def run(self, *args, timeout=120):
        try:
            result = subprocess.run([self.executable, "--config-dir", str(self.config_dir), *args],
                                    shell=False, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            raise AssertionError("mc command timed out or could not start; arguments withheld") from None
        if result.returncode:
            # Args/stderr may contain a credential-bearing replication URL.
            raise AssertionError(f"mc {args[0] if args else ''} failed (exit {result.returncode}); output withheld to protect credentials")
        return result.stdout


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
