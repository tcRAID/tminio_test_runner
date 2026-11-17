"""Explicit dev-only SSH/systemd adapter for the optional fault gates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


ACTIONS = {
    "node_down": ("node", "stop"), "node_up": ("node", "start"),
    "quorum_loss": ("quorum", "stop"), "quorum_restore": ("quorum", "start"),
    "target_down": ("target", "stop"), "target_up": ("target", "start"),
}
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}\Z")
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.service\Z")


def action_plan(inventory, action):
    if not isinstance(inventory, dict) or inventory.get("environment") != "dev":
        raise ValueError("inventory.environment must explicitly be 'dev'")
    if action not in ACTIONS:
        raise ValueError("unsupported service action")
    timeout = inventory.get("timeout_seconds", 20)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
        raise ValueError("timeout_seconds must be between 1 and 120")
    use_sudo = inventory.get("sudo", True)
    if type(use_sudo) is not bool:
        raise ValueError("sudo must be a JSON boolean")
    group, operation = ACTIONS[action]
    groups, nodes = inventory.get("groups"), inventory.get("nodes")
    if not isinstance(groups, dict) or not isinstance(nodes, dict):
        raise ValueError("inventory requires groups and nodes mappings")
    members = groups.get(group)
    if not isinstance(members, list) or not 1 <= len(members) <= 64:
        raise ValueError(f"groups.{group} must contain 1..64 node names")
    plan, seen = [], set()
    for name in members:
        if not isinstance(name, str) or not ALIAS.fullmatch(name) or name in seen:
            raise ValueError("group contains an invalid or duplicate node name")
        seen.add(name)
        node = nodes.get(name)
        if not isinstance(node, dict):
            raise ValueError("group references a missing node")
        host, unit = node.get("host"), node.get("unit")
        if not isinstance(host, str) or not ALIAS.fullmatch(host):
            raise ValueError("host must be an SSH config alias or DNS name without options or user@")
        if not isinstance(unit, str) or not UNIT.fullmatch(unit):
            raise ValueError("unit must be an explicit safe .service name")
        # SSH joins its remote command through a shell. Every remote token is
        # fixed or allowlisted; arbitrary shell fragments are never accepted.
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "ConnectTimeout=5", "--", host]
        command += (["sudo", "-n"] if use_sudo else []) + ["systemctl", operation, "--", unit]
        plan.append((name, command))
    return plan, float(timeout)


def run_action(inventory, action):
    plan, timeout = action_plan(inventory, action)  # Validate every target first.
    results = []
    for name, command in plan:
        result = {"node": name, "ok": False}
        try:
            completed = subprocess.run(command, shell=False, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=timeout, check=False)
            result.update(ok=completed.returncode == 0, exit_code=completed.returncode)
        except subprocess.TimeoutExpired:
            result["error"] = "timeout"
        except OSError:
            result["error"] = "could_not_start_ssh"
        # In particular, recovery attempts ALL members even after one failure.
        results.append(result)
    return {"action": action, "attempted": len(results),
            "failed": sum(not result["ok"] for result in results), "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("action", choices=ACTIONS)
    args = parser.parse_args()
    try:
        inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
        report = run_action(inventory, args.action)
    except (OSError, ValueError):
        print(json.dumps({"error": "invalid or unreadable dev service inventory; no action executed"}))
        return 2
    print(json.dumps(report))
    return int(report["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
