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
    config.evidence_dir = Path(path or Path.cwd() / "test-reports" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])).resolve()
    config.evidence_dir.mkdir(parents=True, exist_ok=True)
    config.release_results = []
    config.release_deselected = 0


def pytest_deselected(items):
    if items:
        items[0].config.release_deselected += len(items)


def pytest_collection_modifyitems(config, items):
    groups = config.getoption("--release-groups")
    if groups and not config.getoption("--release-full"):
        from tminio_test_runner.entrypoint import test_paths
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
def artifacts(pytestconfig):
    return pytestconfig.evidence_dir
