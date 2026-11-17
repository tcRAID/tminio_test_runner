"""Optional fault/recovery checks against an explicitly configured dev cluster.

The node_down hook must leave write quorum and the configured S3 endpoint
available. The quorum_loss hook deliberately removes quorum. Both scenarios
require a matching recovery hook, which is attempted even if injection fails.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import pytest
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    ResponseStreamingError,
)

from tminio_test_runner.helpers import admin_info, assert_version, eventually, list_versions, put_version, run_hook


pytestmark = pytest.mark.integration


def _topology(client):
    info = admin_info(client)
    servers = info.get("servers", [])
    parity = int(info.get("backend", {}).get("standardSCParity", -1))
    assert servers and parity >= 0, "admin info must expose servers and erasure parity"
    sets = {}
    online_servers = 0
    for server in servers:
        online = server.get("state") in {"online", "ok"}
        online_servers += int(online)
        for drive in server.get("drives", []):
            pool, erasure_set = drive.get("pool_index", -1), drive.get("set_index", -1)
            if pool < 0 or erasure_set < 0:
                continue
            group = sets.setdefault(f"{pool}/{erasure_set}", {"total": 0, "online": 0})
            group["total"] += 1
            group["online"] += int(online and drive.get("state") == "ok")
    assert sets, "admin info must expose per-set drive state"
    return {"servers": len(servers), "online_servers": online_servers, "parity": parity, "sets": sets}


def _write_quorum(topology, group):
    data = topology["sets"][group]["total"] - topology["parity"]
    assert data > 0, "invalid erasure layout in baseline admin info"
    return data + int(data == topology["parity"])


def _online_in(topology, group):
    return topology["sets"].get(group, {}).get("online", 0)


def _error_summary(operation, error):
    """Record useful failure categories without endpoint URLs or credentials."""
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        response = {}
    details = response.get("Error")
    metadata = response.get("ResponseMetadata")
    if not isinstance(details, dict):
        details = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "operation": operation,
        "type": type(error).__name__,
        "code": details.get("Code", ""),
        "http_status": metadata.get("HTTPStatusCode"),
    }


def _availability_error(error):
    if isinstance(
        error,
        (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
            ResponseStreamingError,
        ),
    ):
        return True
    if isinstance(error, ClientError):
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return status >= 500 or error.response.get("Error", {}).get("Code") in {
            "RequestTimeout",
            "RequestTimeoutException",
            "SlowDown",
            "XMinioReadQuorum",
            "XMinioWriteQuorum",
        }
    return False


def _baseline_identity(client, bucket):
    return sorted(
        (
            item["Key"],
            item["VersionId"],
            bool(item["IsDeleteMarker"]),
            bool(item["IsLatest"]),
        )
        for item in list_versions(client, bucket)
        if item["Key"].startswith("oracle/")
    )


def _assert_baseline(client, bucket, records, expected):
    for record in records:
        assert_version(client, bucket, record)
    actual = _baseline_identity(client, bucket)
    assert actual == expected, "fault changed seeded version IDs, markers, or latest flags"
    for key, _version, is_marker, is_latest in expected:
        if is_marker and is_latest:
            try:
                client.head_object(Bucket=bucket, Key=key)
            except ClientError as error:
                assert error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
            else:
                raise AssertionError(f"deleted object became visible after fault: {key}")


class _Traffic:
    def __init__(self, client, bucket, records, scenario, rounds):
        self.client = client
        self.bucket = bucket
        self.records = records
        self.scenario = scenario
        self.rounds = rounds
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.acknowledged = []
        self.errors = []
        self.unexpected = []
        self.successful_operations = 0

    def attempt(self, operation, function):
        try:
            result = function()
        except Exception as error:
            summary = _error_summary(operation, error)
            with self.lock:
                self.errors.append(summary)
                if not _availability_error(error):
                    self.unexpected.append(summary)
            return None
        with self.lock:
            self.successful_operations += 1
        return result

    def worker(self, worker_id):
        for iteration in range(self.rounds):
            if self.stop.is_set():
                return
            existing = self.records[(worker_id + iteration) % len(self.records)]
            self.attempt("get_seed", lambda: assert_version(self.client, self.bucket, existing))
            if self.stop.is_set():
                return
            key = f"traffic/{self.scenario}/worker-{worker_id}/object-{iteration}"
            label = f"{self.scenario}:{worker_id}:{iteration}:".encode()
            body = (label * (16384 // len(label) + 1))[:16384]
            record = self.attempt(
                "put",
                lambda: put_version(
                    self.client, self.bucket, key, body,
                    Metadata={"fault-scenario": self.scenario},
                ),
            )
            if record is not None:
                # A returned success is a durability promise even if a later
                # read fails while the cluster is still unavailable.
                with self.lock:
                    self.acknowledged.append(record)
                if not self.stop.is_set():
                    self.attempt("get_write", lambda: assert_version(self.client, self.bucket, record))


@pytest.mark.fault
@pytest.mark.parametrize(
    "down_hook,up_hook,keeps_quorum",
    [
        pytest.param("node_down", "node_up", True, id="node-down-keeps-quorum"),
        pytest.param("quorum_loss", "quorum_restore", False, id="quorum-loss-and-recovery"),
    ],
)
def test_versions_survive_fault_and_recovery(
    settings, src, bucket_factory, artifacts, down_hook, up_hook, keeps_quorum,
):
    faults = settings.get("faults", {})
    missing = [name for name in (down_hook, up_hook) if not faults.get(name)]
    if missing:
        pytest.skip("fault scenario needs configured argv hooks: " + ", ".join(missing))
    settle = float(faults.get("settle_seconds", 5))
    recovery_timeout = float(faults.get("recovery_timeout", 180))
    traffic_timeout = float(faults.get("traffic_timeout", 45))
    workers = int(faults.get("workers", 4))
    rounds = int(faults.get("rounds", 4))
    assert 0 <= settle <= 60, "faults.settle_seconds must be between 0 and 60"
    assert 0 < recovery_timeout <= 900, "faults.recovery_timeout must be between 0 and 900"
    assert 0 < traffic_timeout <= 300, "faults.traffic_timeout must be between 0 and 300"
    assert 1 <= workers <= 16 and 1 <= rounds <= 100, "fault traffic must use 1..16 workers and 1..100 rounds"

    bucket = bucket_factory()
    records = []
    for object_id in range(4):
        key = f"oracle/object-{object_id}"
        for generation in range(3):
            body = f"immutable:{object_id}:{generation}:".encode() * 1024
            records.append(put_version(src, bucket, key, body))
        if object_id % 2:
            marker = src.delete_object(Bucket=bucket, Key=key)
            assert marker.get("DeleteMarker") and marker.get("VersionId"), "fixture requires versioned delete markers"
    baseline = _baseline_identity(src, bucket)
    assert len(baseline) == len(records) + 2, "seeded versions or markers are missing"
    _assert_baseline(src, bucket, records, baseline)
    topology_before = _topology(src)
    assert topology_before["servers"] == topology_before["online_servers"], "start faults only with all nodes online"
    assert all(group["online"] == group["total"] for group in topology_before["sets"].values()), "start faults only with all drives online"

    traffic = _Traffic(src, bucket, records, down_hook, rounds)
    executor = None
    futures = []
    phase_error = None
    restore_error = None
    traffic_timed_out = False
    topology_during = None
    report = {
        "scenario": down_hook, "bucket": bucket,
        "baseline": baseline, "baseline_records": records,
        "topology_before": topology_before,
    }
    report_path = artifacts / f"fault-{down_hook}-observations.json"
    started = time.monotonic()
    try:
        # Recovery is required even if the injection hook only partly succeeds.
        run_hook(settings, down_hook, artifacts)
        time.sleep(settle)
        try:
            topology_during = _topology(src)
            report["topology_during"] = topology_during
        except Exception as error:
            # With quorum lost, admin info may itself be unavailable. A
            # specific MinIO quorum response can establish the fault instead.
            report["topology_during_error"] = _error_summary("admin_info", error)
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="s3-fault")
        futures = [executor.submit(traffic.worker, worker) for worker in range(workers)]
        _done, pending = wait(futures, timeout=traffic_timeout)
        traffic_timed_out = bool(pending)
        for future in _done:
            future.result()  # expose failures outside the individual S3 calls
    except Exception as error:
        phase_error = error
    finally:
        traffic.stop.set()
        try:
            run_hook(settings, up_hook, artifacts)
        except Exception as error:
            restore_error = error
        finally:
            # Restore before joining clients whose requests may be awaiting the
            # failed nodes. SDK connect/read timeouts bound remaining requests.
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
                for future in futures:
                    if not future.cancelled():
                        try:
                            future.result()
                        except Exception as error:
                            if phase_error is None:
                                phase_error = error
            report.update(
                elapsed_seconds=round(time.monotonic() - started, 3),
                traffic_timed_out=traffic_timed_out,
                successful_operations=traffic.successful_operations,
                acknowledged=traffic.acknowledged,
                errors=traffic.errors,
                unexpected_errors=traffic.unexpected,
                phase_error=_error_summary("fault_phase", phase_error) if phase_error else None,
                restore_error=_error_summary("restore", restore_error) if restore_error else None,
            )
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    assert restore_error is None, f"recovery hook failed; inspect {report_path} and hook logs"

    def assert_recovered():
        try:
            src.head_bucket(Bucket=bucket)
            _assert_baseline(src, bucket, records, baseline)
            for record in traffic.acknowledged:
                assert_version(src, bucket, record)
            restored = _topology(src)
            assert restored["online_servers"] == topology_before["online_servers"]
            assert all(
                _online_in(restored, group) == before["total"]
                for group, before in topology_before["sets"].items()
            ), "all baseline drives must recover"
        except BotoCoreError as error:
            raise AssertionError("S3 endpoint is not ready after recovery") from error

    try:
        eventually(assert_recovered, timeout=recovery_timeout)
        report["recovered_oracle_verified"] = True
        report["recovered_baseline"] = _baseline_identity(src, bucket)
    finally:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    assert phase_error is None, (
        f"fault phase failed: {_error_summary('fault_phase', phase_error)}; "
        f"inspect {report_path} and hook logs"
    )
    assert not traffic.unexpected, f"fault caused unexpected S3 errors or content mismatch: {traffic.unexpected}"
    if keeps_quorum:
        assert topology_during is not None, "cannot qualify node_down without observing its topology"
        assert topology_during["online_servers"] < topology_before["online_servers"], "node_down did not take a node offline"
        assert all(
            _online_in(topology_during, group) >= _write_quorum(topology_before, group)
            for group in topology_before["sets"]
        ), "node_down removed write quorum; use the separate quorum_loss scenario"
        assert not traffic_timed_out and not traffic.errors, (
            "node_down must retain quorum and a reachable S3 endpoint; "
            f"traffic errors={traffic.errors}, timed_out={traffic_timed_out}"
        )
        assert len(traffic.acknowledged) == workers * rounds
    else:
        quorum_response = any(error["code"] in {
            "XMinioReadQuorum", "XMinioWriteQuorum",
            "XMinioInsufficientReadQuorum", "XMinioInsufficientWriteQuorum",
        } for error in traffic.errors)
        observed_lost_quorum = topology_during is not None and any(
            _online_in(topology_during, group) < _write_quorum(topology_before, group)
            for group in topology_before["sets"]
        )
        assert observed_lost_quorum or quorum_response, (
            "quorum_loss was not proven by per-set topology or a MinIO quorum error; "
            "an unreachable endpoint alone does not qualify"
        )
        assert traffic.errors or traffic_timed_out, (
            "quorum_loss produced no observable unavailability; check that the hook removes quorum"
        )
