import json

import pytest

from tminio_test_runner.helpers import admin_info

pytestmark = pytest.mark.integration


def test_expected_topology_and_artifact(src, settings, artifacts):
    info = admin_info(src)
    (artifacts / "server-info.json").write_text(json.dumps(info, indent=2))
    backend, servers = info["backend"], info["servers"]
    assert backend["backendType"] == "Erasure"
    assert backend["offlineDisks"] == 0, "start the test with all drives healthy"
    assert all(s["state"] == "online" for s in servers)
    assert len(backend["totalSets"]) == settings["expected_pools"]
    assert len(servers) == settings["expected_nodes"]
    assert backend["standardSCParity"] == settings["expected_parity"]
    assert all(n == settings["expected_data"] + settings["expected_parity"] for n in backend["totalDrivesPerSet"])
    expected = settings.get("expected_commit")
    if expected:
        assert all(s["commitID"] == expected for s in servers), "nodes are not all running the expected commit"
    versions = {(s.get("version"), s.get("commitID")) for s in servers}
    assert len(versions) == 1, "baseline cluster contains mixed artifacts"
