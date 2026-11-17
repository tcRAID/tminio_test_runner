"""Offline safety/reporting regression tests. Run with python -m unittest discover."""
import hashlib
import contextlib
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from botocore.stub import Stubber

from tminio_test_runner import binary_check as check


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.parent = tempfile.TemporaryDirectory()
        self.suite = check.Suite(sys.executable, self.parent.name, timeout=60)
        self.suite.endpoint = "http://127.0.0.1:1"
        self.suite.client = self.suite.make_client()

    def tearDown(self):
        self.suite.stop()
        for client in self.suite.clients:
            client.close()
        self.parent.cleanup()

    def test_client_never_retries_or_uses_environment_proxy(self):
        config = self.suite.client.meta.config
        self.assertEqual(config.retries["total_max_attempts"], 1)
        self.assertEqual(config.proxies, {})
        with self.assertRaisesRegex(AssertionError, "retry"):
            self.suite.record_response("get_object", {"ResponseMetadata": {"RetryAttempts": 1}}, {})

    def test_batch_delete_http_200_with_per_object_error_fails(self):
        response = {"Deleted": [{"Key": "one"}], "Errors": [{"Key": "two", "Code": "AccessDenied"}],
                    "ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0}}
        with Stubber(self.suite.client) as stub:
            stub.add_response("delete_objects", response, {"Bucket": check.BUCKET, "Delete": {
                "Objects": [{"Key": "one"}, {"Key": "two"}], "Quiet": False}})
            with self.assertRaisesRegex(AssertionError, "per-object Errors"):
                self.suite.batch_delete(check.BUCKET, ["one", "two"])
            stub.assert_no_pending_responses()

    def test_batch_delete_omitted_result_fails(self):
        with Stubber(self.suite.client) as stub:
            stub.add_response("delete_objects", {"Deleted": [{"Key": "one"}]})
            with self.assertRaisesRegex(AssertionError, "omitted/duplicated"):
                self.suite.batch_delete(check.BUCKET, ["one", "two"])

    def test_list_repeated_continuation_fails_instead_of_looping(self):
        with Stubber(self.suite.client) as stub:
            for _ in range(2):
                stub.add_response("list_objects_v2", {"IsTruncated": True, "NextContinuationToken": "same"})
            with self.assertRaisesRegex(AssertionError, "repeated continuation"):
                list(self.suite.pages("list_objects_v2", Bucket=check.BUCKET))

    def test_403_is_not_accepted_as_deleted(self):
        with Stubber(self.suite.client) as stub:
            stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
            with self.assertRaisesRegex(AssertionError, "AccessDenied"):
                self.suite.missing(check.BUCKET, "one")

    def test_successful_get_is_not_accepted_as_denied(self):
        with Stubber(self.suite.client) as stub:
            stub.add_response("list_objects_v2", {"KeyCount": 0})
            with self.assertRaisesRegex(AssertionError, "expected rejection"):
                self.suite.error("list_objects_v2", {"AccessDenied"}, Bucket=check.BUCKET)

    def test_environment_cannot_point_child_at_external_integrations(self):
        with patch.dict(os.environ, {"MINIO_VOLUMES": "/production", "MINIO_CONFIG_ENV_FILE": "/secrets",
                                    "MINIO_KMS_KES_ENDPOINT": "https://outside", "CI": "1", "HTTP_PROXY": "http://proxy"}):
            env = check.server_environment("random-access", "random-secret")
        self.assertNotIn("MINIO_VOLUMES", env)
        self.assertNotIn("MINIO_CONFIG_ENV_FILE", env)
        self.assertNotIn("MINIO_KMS_KES_ENDPOINT", env)
        self.assertNotIn("CI", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertEqual(env["MINIO_ROOT_USER"], "random-access")

    def test_cleanup_refuses_replaced_data_directory(self):
        outside = Path(self.parent.name) / "unrelated"
        outside.mkdir()
        (outside / "important").write_text("keep")
        self.suite.data.rmdir()
        self.suite.data.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(AssertionError, "escaped"):
            self.suite.cleanup_data()
        self.assertEqual((outside / "important").read_text(), "keep")

    def test_cleanup_refuses_foreign_owner_and_running_process(self):
        marker = self.suite.directory / ".owned-by-check"
        marker.write_text("someone-else")
        with self.assertRaisesRegex(AssertionError, "ownership"):
            self.suite.cleanup_data()
        marker.write_text(self.suite.owner_token)
        self.suite.process = object()
        with self.assertRaisesRegex(AssertionError, "running"):
            self.suite.cleanup_data()
        self.suite.process = None

    def test_cleanup_keeps_parent_files_and_reports(self):
        outside = Path(self.parent.name) / "important"
        outside.write_text("keep")
        self.suite.cleanup_data()
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue((self.suite.directory / "results.json").exists())
        self.assertFalse(self.suite.data.exists())

    def test_reports_redact_secrets_and_fail_exit_and_junit(self):
        def failure():
            raise AssertionError(f"secret={self.suite.secret} url=http://localhost/path?X-Amz-Signature=secret")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(self.suite.run_case("intentional failure", failure))
            self.assertEqual(self.suite.finish(), 1)
        for name in ("results.json", "summary.txt", "junit.xml"):
            text = (self.suite.directory / name).read_text()
            self.assertNotIn(self.suite.secret, text)
            self.assertNotIn("X-Amz-Signature=secret", text)
        root = ET.parse(self.suite.directory / "junit.xml").getroot()
        self.assertEqual(root.attrib["failures"], str(len(check.CHECK_NAMES) + 1))
        self.assertIn("NOT RUN", (self.suite.directory / "results.json").read_text())
        self.assertTrue(self.suite.data.exists())  # Failed evidence is retained.

    def test_version_pagination_carries_both_markers(self):
        with Stubber(self.suite.client) as stub:
            stub.add_response("list_object_versions", {"IsTruncated": True, "NextKeyMarker": "k", "NextVersionIdMarker": "v"},
                              {"Bucket": check.VERSIONED})
            stub.add_response("list_object_versions", {"IsTruncated": False},
                              {"Bucket": check.VERSIONED, "KeyMarker": "k", "VersionIdMarker": "v"})
            self.assertEqual(len(list(self.suite.pages("list_object_versions", Bucket=check.VERSIONED))), 2)
            stub.assert_no_pending_responses()

    def test_fixture_streaming_and_dataset_boundaries(self):
        self.assertEqual(len(set(check.dataset_keys())), 2105)
        self.assertTrue(any("<>" in k for k in check.dataset_keys()))
        stream, expected = check.fixture(self.suite.payloads, 8 * check.MIB + 13, "repeatable")
        with stream:
            self.assertEqual(check.stream_hash(stream), (expected, 8 * check.MIB + 13))
        stream, expected = check.fixture(self.suite.payloads, 0, "empty")
        with stream:
            self.assertEqual(expected, hashlib.sha256(b"").hexdigest())
            self.assertEqual(stream.read(), b"")

    def test_minio_environment_does_not_inherit_user_config(self):
        env = check.server_environment("a", "b")
        self.assertEqual({k for k in env if k.startswith("MINIO_")},
                         {"MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "MINIO_BROWSER", "MINIO_UPDATE"})
        self.assertEqual(env["MINIO_UPDATE"], "off")

    def test_owned_child_is_stopped(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.suite.process = child
        self.suite.stop()
        self.assertIsNotNone(child.poll())
        self.assertIsNone(self.suite.process)

    def test_unexpected_server_exit_is_not_clean_shutdown(self):
        child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(23)"], start_new_session=True)
        child.wait(timeout=5)
        self.suite.process = child
        with self.assertRaisesRegex(AssertionError, "exit 23"):
            self.suite.stop(require_clean=True)

    def test_mutation_intent_precedes_client_request(self):
        def verify(**_):
            events = [json.loads(s) for s in (self.suite.directory / "requests.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["kind"], "mutation")
            self.assertEqual(events[-1]["key"], "assigned")
            return {"ResponseMetadata": {"RetryAttempts": 0}}
        with patch.object(self.suite.client, "put_object", side_effect=verify):
            self.suite.call("put_object", Bucket=check.BUCKET, Key="assigned", Body=b"data")

    def test_deadline_stops_later_tests_and_reports_incomplete(self):
        with patch.object(check.Suite, "artifact"), patch.object(check.Suite, "startup"), \
             patch.object(check.Suite, "objects", side_effect=check.Deadline("test deadline")), \
             patch.object(check.Suite, "cycles") as later, contextlib.redirect_stdout(io.StringIO()):
            result = check.main([sys.executable, "--work-dir", self.parent.name])
        self.assertEqual(result, 1)
        later.assert_not_called()


if __name__ == "__main__":
    unittest.main()
