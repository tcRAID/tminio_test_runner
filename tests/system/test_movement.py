"""Optional maintenance gates; hooks must act on an isolated dev deployment."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid

import pytest

from tminio_test_runner.helpers import admin_info, assert_missing, assert_version, eventually, list_versions, put_version
from tminio_test_runner.hook_process import UnverifiedHookCleanup, run_controlled


pytestmark = [pytest.mark.integration, pytest.mark.admin]


def _require_hooks(settings, names):
    maintenance = settings.get("maintenance", {})
    missing = [name for name in names if not maintenance.get(name)]
    if missing:
        pytest.skip("maintenance gate needs configured argv hooks: " + ", ".join(missing))
    for name in names:
        command = maintenance[name]
        assert isinstance(command, list) and command and all(isinstance(arg, str) for arg in command), f"maintenance.{name} must be an argv array"
    assert 0 < float(maintenance.get("hook_timeout", 120)) <= 3600
    if "rebalance_wait" in names:
        assert 0 < float(maintenance.get("rebalance_wait_timeout", 600)) <= 3600
    return maintenance


def _pending(artifacts, operation, hook=None, bucket=None):
    path = artifacts / "maintenance-pending-recovery.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if hook is None:
        state.pop(operation, None)
    else:
        state[operation] = {"hook": hook, "bucket": bucket}
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_maintenance_hook(settings, name, artifacts, bucket, require_progress=False):
    maintenance = settings["maintenance"]
    timeout = float(maintenance.get("rebalance_wait_timeout", 600) if name == "rebalance_wait" else maintenance.get("hook_timeout", 120))
    assert 0 < timeout <= 3600, "maintenance hook timeout must be between 0 and 3600 seconds"
    environment = os.environ.copy()
    environment.update(TMINIO_TEST_BUCKET=bucket, TMINIO_TEST_ARTIFACTS=str(artifacts))
    started = time.monotonic()
    event = {"hook": name}
    try:
        # A daemon started by a hook can inherit descriptors. Never use PIPE:
        # communicate() can wait forever after the hook itself has exited.
        with tempfile.TemporaryFile(mode="w+b") as output:
            try:
                completed = run_controlled(
                    maintenance[name], artifacts=artifacts, env=environment, timeout=timeout, label=name,
                    stdout=output if require_progress else subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                event["error"] = "timeout"
                raise AssertionError(f"maintenance hook {name} exceeded {timeout}s") from None
            except UnverifiedHookCleanup:
                event["error"] = "cleanup_uncertain"
                raise AssertionError("maintenance hook cleanup is unverified; manual descendant/service verification required") from None
            except OSError:
                event["error"] = "could_not_start"
                raise AssertionError(f"maintenance hook {name} could not start; arguments withheld") from None
            code = completed.returncode
            event["returncode"] = code
            assert code == 0, f"maintenance hook {name} failed (exit {code}); output withheld"
            if require_progress:
                output.seek(0)
                raw = output.read(1024 * 1024 + 1)
                assert len(raw) <= 1024 * 1024, "rebalance_wait JSON exceeds 1 MiB"
                try:
                    proof = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise AssertionError("rebalance_wait must emit one JSON object with moved_versions and failed_versions") from None
                assert isinstance(proof, dict), "rebalance_wait proof must be a JSON object"
                moved, failed = proof.get("moved_versions"), proof.get("failed_versions")
                assert type(moved) is int and moved > 0, "rebalance_wait must prove moved_versions > 0; an idle rebalance is not a pass"
                assert type(failed) is int and failed == 0, "rebalance_wait must prove failed_versions == 0"
                event["progress"] = {"moved_versions": moved, "failed_versions": failed}
                return event["progress"]
    finally:
        event["seconds"] = round(time.monotonic() - started, 3)
        with (artifacts / "maintenance-hooks.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(event) + "\n")


def _seed(client, bucket, prefix="oracle"):
    records = []
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0)
    for object_id in range(4):
        key = f"{prefix}/object-{object_id}"
        for generation in range(3):
            body = f"{prefix}:{object_id}:{generation}:".encode() * 4096
            tags = {"maintenance": "preserve", "generation": str(generation)}
            record = put_version(
                client, bucket, key, body,
                Metadata={"maintenance": "preserve", "generation": str(generation)},
                Tagging=f"maintenance=preserve&generation={generation}", Expires=expires,
            )
            record.update(tags=tags, expires=expires.isoformat())
            records.append(record)
        if object_id % 2:
            marker = client.delete_object(Bucket=bucket, Key=key)
            assert marker.get("DeleteMarker") and marker.get("VersionId")
    return records


def _identity(client, bucket, prefix="oracle/"):
    return sorted(
        (row["Key"], row["VersionId"], bool(row["IsDeleteMarker"]), bool(row["IsLatest"]))
        for row in list_versions(client, bucket, prefix=prefix)
    )


def _verify(client, bucket, records, identity, prefix="oracle/"):
    for record in records:
        head = assert_version(client, bucket, record)
        assert head.get("Expires") == datetime.fromisoformat(record["expires"]), f"Expires changed for {record['key']}"
        actual_tags = client.get_object_tagging(Bucket=bucket, Key=record["key"], VersionId=record["version"])
        assert {tag["Key"]: tag["Value"] for tag in actual_tags["TagSet"]} == record["tags"], f"tags changed for {record['key']}"
    assert _identity(client, bucket, prefix) == identity, "maintenance changed version IDs, delete markers, or latest flags"
    for key, version, is_marker, is_latest in identity:
        if is_latest and is_marker:
            assert_missing(client, bucket, key)
        elif is_latest:
            assert client.head_object(Bucket=bucket, Key=key).get("VersionId") == version, "latest GET/HEAD disagrees with version listing"


def _builds(client):
    servers = admin_info(client).get("servers", [])
    assert servers and all(server.get("state") in {"online", "ok"} for server in servers), "all source nodes must be online"
    result = {}
    for server in servers:
        endpoint = server.get("endpoint")
        identity = [server.get("version", ""), server.get("commitID", "")]
        assert endpoint and any(identity), "admin info must expose node and build identities"
        result[endpoint] = identity
    return result


def test_rebalance_stop_resume_preserves_versions(settings, src, bucket_factory, artifacts):
    maintenance = _require_hooks(settings, ("rebalance_start", "rebalance_stop", "rebalance_resume", "rebalance_wait"))
    workers = int(maintenance.get("workers", 4))
    rounds = int(maintenance.get("rounds_per_phase", 8))
    progress_timeout = float(settings.get("progress_timeout", 90))
    assert 1 <= workers <= 16 and 1 <= rounds <= 100
    assert 0 < progress_timeout <= 600
    bucket = bucket_factory()
    records = _seed(src, bucket)
    identity = _identity(src, bucket)
    _verify(src, bucket, records, identity)
    stop = threading.Event()
    lock = threading.Lock()
    phase = [0]
    completed = [set(), set(), set()]
    ready = [threading.Event() for _ in range(3)]
    acknowledged = []

    def traffic(worker):
        counts = [0, 0, 0]
        while not stop.is_set():
            with lock:
                current_phase = phase[0]
            if counts[current_phase] == rounds:
                stop.wait(0.02)
                continue
            iteration = counts[current_phase]
            key = f"traffic/phase-{current_phase}/worker-{worker}/object-{iteration}"
            body = (key + ":payload").encode() * 512
            record = put_version(src, bucket, key, body, Metadata={"phase": str(current_phase)})
            with lock:
                acknowledged.append(record)
            assert_version(src, bucket, record)
            counts[current_phase] += 1
            with lock:
                completed[current_phase].add(worker)
                if len(completed[current_phase]) == workers:
                    ready[current_phase].set()

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="s3-rebalance")
    futures = [executor.submit(traffic, worker) for worker in range(workers)]

    def wait_for_phase(number):
        deadline = time.monotonic() + progress_timeout
        while not ready[number].wait(min(1, max(0, deadline - time.monotonic()))):
            for future in futures:
                if future.done():
                    future.result()
            assert time.monotonic() < deadline, f"no acknowledged traffic from all workers during maintenance phase {number}"

    pending_stop = False
    phase_error = cleanup_error = None
    report = {"bucket": bucket, "records": records, "identity": identity}
    try:
        wait_for_phase(0)
        _pending(artifacts, "rebalance", "rebalance_stop", bucket)
        pending_stop = True
        run_maintenance_hook(settings, "rebalance_start", artifacts, bucket)
        with lock:
            phase[0] = 1
        wait_for_phase(1)
        run_maintenance_hook(settings, "rebalance_stop", artifacts, bucket)
        pending_stop = False
        _pending(artifacts, "rebalance")
        _verify(src, bucket, records, identity)
        _pending(artifacts, "rebalance", "rebalance_stop", bucket)
        pending_stop = True
        run_maintenance_hook(settings, "rebalance_resume", artifacts, bucket)
        with lock:
            phase[0] = 2
        wait_for_phase(2)
        report["progress"] = run_maintenance_hook(settings, "rebalance_wait", artifacts, bucket, require_progress=True)
        pending_stop = False
        _pending(artifacts, "rebalance")
    except Exception as error:
        phase_error = error
    finally:
        stop.set()
        try:
            if pending_stop:
                run_maintenance_hook(settings, "rebalance_stop", artifacts, bucket)
                _pending(artifacts, "rebalance")
        except Exception as error:
            cleanup_error = error
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            for future in futures:
                if not future.cancelled():
                    try:
                        future.result()
                    except Exception as error:
                        if phase_error is None:
                            phase_error = error
            report.update(acknowledged=acknowledged, phase_workers=[len(group) for group in completed],
                          phase_error=type(phase_error).__name__ if phase_error else None,
                          cleanup_error=type(cleanup_error).__name__ if cleanup_error else None)
            (artifacts / "rebalance-observations.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert cleanup_error is None, "rebalance stop failed; inspect pending recovery journal"
    if phase_error is not None:
        raise phase_error
    _verify(src, bucket, records, identity)
    for record in acknowledged:
        assert_version(src, bucket, record)
    report["oracle_verified"] = True
    (artifacts / "rebalance-observations.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def test_upgrade_and_rollback_preserve_old_and_new_versions(settings, src, bucket_factory, artifacts):
    maintenance = _require_hooks(settings, ("upgrade", "rollback"))
    timeout = float(maintenance.get("recovery_timeout", 180))
    assert 0 < timeout <= 900
    bucket = bucket_factory()
    before_records = _seed(src, bucket)
    before_identity = _identity(src, bucket)
    before_builds = _builds(src)
    _verify(src, bucket, before_records, before_identity)
    upgraded_records = []
    upgraded_identity = None
    phase_error = rollback_error = None
    report = {"bucket": bucket, "before_builds": before_builds, "before_records": before_records, "before_identity": before_identity}
    _pending(artifacts, "upgrade", "rollback", bucket)
    try:
        run_maintenance_hook(settings, "upgrade", artifacts, bucket)

        def upgraded_ready():
            builds = _builds(src)
            assert builds.keys() == before_builds.keys(), "upgrade changed the source node set"
            assert all(builds[node] != before_builds[node] for node in builds), "upgrade must actually change every node's version/commit identity"
            assert len({tuple(identity) for identity in builds.values()}) == 1, "upgrade left mixed candidate build identities"
            expected = settings.get("expected_upgrade_commit")
            if expected:
                assert all(identity[1] == expected for identity in builds.values()), "upgrade did not deploy expected_upgrade_commit"
            _verify(src, bucket, before_records, before_identity)
            report["upgraded_builds"] = builds

        eventually(upgraded_ready, timeout=timeout)
        upgraded_records = _seed(src, bucket, prefix="upgrade-oracle")
        upgraded_identity = _identity(src, bucket, prefix="upgrade-oracle/")
        _verify(src, bucket, upgraded_records, upgraded_identity, prefix="upgrade-oracle/")
    except Exception as error:
        phase_error = error
    finally:
        try:
            run_maintenance_hook(settings, "rollback", artifacts, bucket)
        except Exception as error:
            rollback_error = error
        report.update(upgraded_records=upgraded_records, upgraded_identity=upgraded_identity,
                      phase_error=type(phase_error).__name__ if phase_error else None,
                      rollback_error=type(rollback_error).__name__ if rollback_error else None)
        (artifacts / "upgrade-rollback-observations.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert rollback_error is None, "rollback hook failed; inspect pending recovery journal"

    def rolled_back_ready():
        assert _builds(src) == before_builds, "rollback did not restore every original node's version/commit identity"
        _verify(src, bucket, before_records, before_identity)
        if upgraded_identity is not None:
            _verify(src, bucket, upgraded_records, upgraded_identity, prefix="upgrade-oracle/")

    eventually(rolled_back_ready, timeout=timeout)
    _pending(artifacts, "upgrade")
    report["rollback_oracle_verified"] = True
    (artifacts / "upgrade-rollback-observations.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if phase_error is not None:
        raise phase_error
