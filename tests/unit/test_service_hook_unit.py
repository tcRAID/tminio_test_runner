"""Offline SSH adapter validation; SSH is never executed by these tests."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from tminio_test_runner import service_hook
def inventory():
    return {"environment": "dev", "nodes": {
        "a": {"host": "dev-node-a", "unit": "tminio.service"},
        "b": {"host": "dev-node-b", "unit": "tminio@pool1.service"},
    }, "groups": {"node": ["a"], "quorum": ["a", "b"]}}


@pytest.mark.parametrize("change", [
    lambda config: config.update(environment="production"),
    lambda config: config["groups"].pop("quorum"),
    lambda config: config["groups"].update(quorum=["missing"]),
    lambda config: config["nodes"]["b"].update(host="-oProxyCommand=evil"),
    lambda config: config["nodes"]["b"].update(host="host;touch /tmp/evil"),
    lambda config: config["nodes"]["b"].update(unit="tminio.service;reboot"),
    lambda config: config["nodes"]["b"].update(unit="--all.service"),
])
def test_invalid_inventory_never_executes_even_a_valid_first_node(monkeypatch, change):
    config = inventory()
    change(config)
    monkeypatch.setattr(service_hook.subprocess, "run", lambda *args, **kwargs: pytest.fail("SSH was invoked"))
    with pytest.raises(ValueError):
        service_hook.run_action(config, "quorum_restore")


@pytest.mark.parametrize("first_failure", ["exit", "timeout", "missing_ssh"])
def test_recovery_attempts_every_node_after_partial_failure(monkeypatch, first_failure):
    commands = []

    def ssh(command, **kwargs):
        commands.append(command)
        assert kwargs["shell"] is False and kwargs["timeout"] == 20
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert command[-4:-1] == ["systemctl", "start", "--"]
        if len(commands) == 1:
            if first_failure == "timeout":
                raise subprocess.TimeoutExpired(command, 20)
            if first_failure == "missing_ssh":
                raise FileNotFoundError("private local path")
            return SimpleNamespace(returncode=255)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(service_hook.subprocess, "run", ssh)
    report = service_hook.run_action(inventory(), "quorum_restore")
    assert len(commands) == 2 and report["attempted"] == 2 and report["failed"] == 1
    assert report["results"][1] == {"node": "b", "ok": True, "exit_code": 0}
    assert "private" not in json.dumps(report)


def test_target_down_and_up_use_only_explicit_target_group():
    config = inventory()
    config["groups"]["target"] = ["b"]
    config["sudo"] = False
    for action, operation in [("target_down", "stop"), ("target_up", "start")]:
        plan, timeout = service_hook.action_plan(config, action)
        assert len(plan) == 1 and plan[0][0] == "b"
        assert plan[0][1][-5:] == ["dev-node-b", "systemctl", operation, "--", "tminio@pool1.service"]
        assert "sudo" not in plan[0][1] and timeout == 20


def test_example_inventory_is_disabled_until_operator_opt_in():
    config = json.loads((Path(__file__).resolve().parents[2] / "config/service-inventory.example.json").read_text())
    with pytest.raises(ValueError, match="environment"):
        service_hook.action_plan(config, "node_down")
