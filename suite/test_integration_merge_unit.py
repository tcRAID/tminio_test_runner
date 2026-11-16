"""Regression checks for consolidated entrypoints, credentials and cleanup."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.stub import Stubber

import credentials
import endpoint_check
from lab_cleanup import LabBuckets


@pytest.mark.parametrize('values', [
    {'MINIO_ACCESS_KEY': 'new', 'TMINIO_TEST_ACCESS_KEY': 'old', 'TMINIO_TEST_SECRET_KEY': 'secret'},
    {'MINIO_ACCESS_KEY': 'new', 'TMINIO_TEST_SECRET_KEY': 'secret'},
    {'MINIO_ACCESS_KEY': 'new', 'MINIO_SECRET_KEY': 'secret', 'TMINIO_TEST_ACCESS_KEY': 'new'},
])
def test_credentials_never_mix_pairs_or_silently_override_conflicts(monkeypatch, values):
    for prefix in ('MINIO_', 'TMINIO_TEST_'):
        for key in ('ACCESS_KEY', 'SECRET_KEY'):
            monkeypatch.delenv(prefix + key, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match='conflicting'):
        credentials.credentials()


def test_credentials_accept_old_and_new_complete_pairs(monkeypatch):
    for prefix in ('MINIO_', 'TMINIO_TEST_'):
        monkeypatch.setenv(prefix + 'ACCESS_KEY', 'access')
        monkeypatch.setenv(prefix + 'SECRET_KEY', 'secret')
    assert credentials.credentials() == ('access', 'secret')


def test_public_default_dispatches_only_endpoint(monkeypatch):
    path = Path(__file__).resolve().parent.parent / 'minio_test_runner.py'
    spec = importlib.util.spec_from_file_location('public_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ensure_runtime', lambda _: None)
    monkeypatch.setattr(module, '_BUNDLE_VERIFIED', True, raising=False)
    calls = []
    monkeypatch.setattr(endpoint_check, 'main', lambda args, **kw: calls.append((args, kw)) or 0)
    assert module.main(['http://example:9000']) == 0
    assert calls == [(['http://example:9000'], {'soak': False})]
    calls.clear()
    assert module.main(['soak', 'http://example:9000']) == 0
    assert calls == [(['http://example:9000'], {'soak': True})]


def test_lab_cleanup_attempts_every_bucket_and_reports_remaining(tmp_path, monkeypatch):
    registry = LabBuckets(tmp_path, 'token')
    client = SimpleNamespace(meta=SimpleNamespace(endpoint_url='http://example:9000'))
    registry.record(client, 'failed')
    registry.record(client, 'cleaned')
    monkeypatch.setattr(registry, 'prepare', lambda _: True)
    attempted = []
    def purge(entry, _):
        attempted.append(entry['name'])
        if entry['name'] == 'failed':
            raise AssertionError('denied')
    monkeypatch.setattr(registry, 'purge', purge)
    with pytest.raises(AssertionError, match='failed'):
        registry.cleanup()
    assert attempted == ['failed', 'cleaned']
    assert [v['bucket'] for v in json.loads((tmp_path / 'remaining-test-buckets.json').read_text())] == ['failed']


def test_http_encryption_requires_exact_rejection_and_absence(tmp_path):
    suite = endpoint_check.EndpointSuite('http://example.invalid:9000', 'access', 'secret', tmp_path)
    suite.owned_buckets.add(suite.bucket)
    try:
        with Stubber(suite.client) as stub:
            stub.add_client_error('put_object', service_error_code='InvalidRequest', http_status_code=400)
            stub.add_client_error('get_object', service_error_code='NoSuchKey', http_status_code=404)
            suite.encryption_behavior()
            stub.assert_no_pending_responses()
        assert (suite.bucket, 'encryption/customer-key') in suite.known_keys
        assert 'HTTP rejection' in suite.report['capabilities']['sse_c']
    finally:
        suite.cleanup_data()
        for client in suite.clients:
            client.close()


def test_default_selection_is_complete_and_single_cluster():
    assert len(endpoint_check.CHECK_NAMES) == 20
    assert len(set(endpoint_check.CHECK_NAMES)) == 20
    assert 'Object lock configuration' in endpoint_check.CHECK_NAMES
    assert not any('replication' in name.lower() or 'soak' in name.lower() for name in endpoint_check.CHECK_NAMES)


@pytest.mark.parametrize('batch_error', [False, True])
def test_lab_cleanup_paginates_uploads_clears_holds_and_checks_batch_errors(tmp_path, batch_error):
    suite = endpoint_check.EndpointSuite('http://example.invalid:9000', 'access', 'secret', tmp_path)
    registry = LabBuckets(tmp_path, 'token')
    entry = registry.record(suite.client, 'lab-owned', locked=True)
    entry['tagged'] = True
    args = {'Bucket': 'lab-owned'}
    objects = [{'Key': 'data', 'VersionId': 'version'}, {'Key': 'data', 'VersionId': 'marker'}]
    try:
        with Stubber(suite.client) as stub:
            stub.add_response('get_bucket_tagging', {'TagSet': [{'Key': 'tminio-check-owner', 'Value': 'token'}]}, args)
            stub.add_client_error('delete_bucket_replication', service_error_code='XMinioAdminRemoteTargetNotFoundError',
                                  http_status_code=404, expected_params=args)
            for method in ('delete_bucket_lifecycle', 'delete_bucket_policy'):
                stub.add_response(method, {}, args)
            stub.add_response('list_multipart_uploads', {'Uploads': [{'Key': 'upload', 'UploadId': 'one'}],
                              'IsTruncated': True, 'NextKeyMarker': 'upload', 'NextUploadIdMarker': 'one'}, args)
            stub.add_response('abort_multipart_upload', {}, {**args, 'Key': 'upload', 'UploadId': 'one'})
            stub.add_response('list_multipart_uploads', {'Uploads': [{'Key': 'upload', 'UploadId': 'two'}]},
                              {**args, 'KeyMarker': 'upload', 'UploadIdMarker': 'one'})
            stub.add_response('abort_multipart_upload', {}, {**args, 'Key': 'upload', 'UploadId': 'two'})
            stub.add_response('list_object_versions', {'Versions': [objects[0]], 'DeleteMarkers': [objects[1]]}, args)
            stub.add_response('put_object_legal_hold', {}, {**args, **objects[0], 'LegalHold': {'Status': 'OFF'}})
            response = {'Errors': [{'Key': 'data', 'VersionId': 'version', 'Code': 'AccessDenied'}]} if batch_error else {'Deleted': objects}
            stub.add_response('delete_objects', response, {**args, 'Delete': {'Objects': objects, 'Quiet': False},
                                                           'BypassGovernanceRetention': True})
            if batch_error:
                with pytest.raises(AssertionError, match='cleanup failed'):
                    registry.cleanup()
                assert registry.entries
            else:
                stub.add_response('delete_bucket', {}, args)
                stub.add_client_error('head_bucket', service_error_code='404', http_status_code=404, expected_params=args)
                registry.cleanup()
                assert not registry.entries
            stub.assert_no_pending_responses()
    finally:
        suite.cleanup_data()
        for client in suite.clients:
            client.close()


def test_abnormal_case_exit_still_records_failure(tmp_path):
    suite = endpoint_check.EndpointSuite('http://example.invalid:9000', 'access', 'secret', tmp_path)
    def exit_early():
        raise SystemExit(3)
    try:
        with pytest.raises(SystemExit):
            suite.run_case('abnormal exit', exit_early)
        assert suite.report['results'][-1]['status'] == 'FAIL'
        assert json.loads((suite.directory / 'results.json').read_text())['results'][-1]['status'] == 'FAIL'
    finally:
        suite.cleanup_data()
        for client in suite.clients:
            client.close()


def test_lab_remaining_report_preserves_prior_fixture_failures(tmp_path, monkeypatch):
    client = SimpleNamespace(meta=SimpleNamespace(endpoint_url='http://example:9000'))
    previous = LabBuckets(tmp_path, 'first')
    previous.record(client, 'first-failed-bucket')
    following = LabBuckets(tmp_path, 'second')
    following.record(client, 'second-bucket')
    monkeypatch.setattr(following, 'prepare', lambda _: True)
    monkeypatch.setattr(following, 'purge', lambda *args: None)
    following.cleanup()
    remaining = json.loads((tmp_path / 'remaining-test-buckets.json').read_text())
    assert [e['bucket'] for e in remaining] == ['first-failed-bucket']
