"""Visible, bounded retries of HTTP 503 responses for endpoint soaks."""
import random
import time
import uuid

from botocore.exceptions import ClientError

RETRY_EVENT_KINDS = {"http_503", "retry", "retry_recovered", "retry_exhausted", "retry_skipped", "retry_failed"}


def call_with_503_retries(suite, method, kwargs, invoke, *, expected_status=None):
    limit = suite.http503_retry_limit
    body = kwargs.get("Body")
    position, replayable = None, True
    if hasattr(body, "read"):
        try:
            position = body.tell()
            body.seek(position)
        except (AttributeError, OSError, ValueError):
            replayable = False
    group = None
    fields = {"operation": method, "bucket": kwargs.get("Bucket"), "key": kwargs.get("Key"),
              "max_attempts": limit + 1}

    def record(kind, counters=(), **details):
        with suite.state_lock:
            for counter in counters:
                suite.report["http_503_retry"][counter] += 1
            suite.event(kind, **fields, call_id=group, **details)

    for attempt in range(1, limit + 2):
        started = time.monotonic()
        try:
            result = invoke()
        except ClientError as error:
            response = error.response
            metadata = response.get("ResponseMetadata", {})
            if metadata.get("HTTPStatusCode") != 503:
                if group:
                    if expected_status is not None and metadata.get("HTTPStatusCode") == expected_status:
                        record("retry_recovered", ("recovered_requests",), attempt=attempt,
                               status=expected_status, request_id=metadata.get("RequestId"))
                    else:
                        record("retry_failed", ("failed_requests",), attempt=attempt,
                               error_type=type(error).__name__, error=str(error))
                raise
            group = group or uuid.uuid4().hex
            details = {"attempt": attempt, "status": 503,
                       "error_code": response.get("Error", {}).get("Code"),
                       "error_message": response.get("Error", {}).get("Message"),
                       "elapsed_seconds": round(time.monotonic() - started, 3),
                       "request_id": metadata.get("RequestId")}
            record("http_503", ("responses",), **details)
            # A proxy can return 503 after the backend committed a mutation.
            # The owning worker reconciles these exact keys before another cycle.
            if kwargs.get("Key") is not None and (
                    method in {"put_object", "copy_object", "create_multipart_upload"}
                    or method == "delete_object" and "VersionId" not in kwargs):
                with suite.state_lock:
                    suite.retry_reconcile_keys.add((kwargs["Bucket"], kwargs["Key"]))
            if attempt > limit:
                record("retry_exhausted", ("exhausted_requests", "failed_requests"), **details)
                raise
            if not replayable:
                record("retry_skipped", ("unreplayable_requests", "failed_requests"),
                       reason="request body cannot be rewound", **details)
                raise
            if position is not None:
                try:
                    body.seek(position)
                except (AttributeError, OSError, ValueError):
                    record("retry_skipped", ("unreplayable_requests", "failed_requests"),
                           reason="request body could not be rewound after failure", **details)
                    raise error
            delay = 2 ** (attempt - 1) + random.uniform(0, 0.25)
            time.sleep(delay)
            # Count only retries about to be issued, not an interrupted backoff.
            record("retry", ("retries",), next_attempt=attempt + 1, delay_seconds=round(delay, 3), **details)
            print(f"  RETRY {method}: HTTP 503, retry {attempt}/{limit} after {delay:.2f}s", flush=True)
        except Exception as error:
            if group:
                record("retry_failed", ("failed_requests",), attempt=attempt,
                       error_type=type(error).__name__, error=str(error))
            raise
        else:
            if group:
                record("retry_recovered", ("recovered_requests",), attempt=attempt,
                       request_id=result.get("ResponseMetadata", {}).get("RequestId"))
            return result
