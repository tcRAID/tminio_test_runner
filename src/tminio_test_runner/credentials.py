"""One credential pair for both original-runner and release-suite callers."""
import getpass
import os
import sys


def credentials():
    pairs = [(os.getenv(prefix + "ACCESS_KEY"), os.getenv(prefix + "SECRET_KEY"))
             for prefix in ("MINIO_", "TMINIO_TEST_")]
    supplied = [pair for pair in pairs if any(pair)]
    if len(supplied) == 2 and supplied[0] != supplied[1]:
        raise ValueError("conflicting MINIO_* and TMINIO_TEST_* credentials; set just one complete pair")
    access, secret = supplied[0] if supplied else (None, None)
    if not access or not secret:
        if not sys.stdin.isatty():
            raise ValueError("set MINIO_ACCESS_KEY and MINIO_SECRET_KEY (or TMINIO_TEST_ACCESS_KEY and TMINIO_TEST_SECRET_KEY), or run interactively")
        access = access or input("MinIO access key: ").strip()
        secret = secret or getpass.getpass("MinIO secret key: ")
    if not access or not secret:
        raise ValueError("access key and secret key cannot be empty")
    return access, secret
