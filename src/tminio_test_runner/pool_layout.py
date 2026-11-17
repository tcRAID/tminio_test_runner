"""Read-only placement evidence and reversible fixture configuration."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

from .admin_config import parse_config


def pool_roots(settings):
    roots = settings.get("pool_roots")
    assert isinstance(roots, list) and len(roots) == settings["expected_pools"] and len(roots) >= 2
    assert all(isinstance(drives, list) and drives for drives in roots)
    assert all(Path(root).is_dir() for drives in roots for root in drives), "pool_roots must be readable"
    return roots


def metadata_snapshot(roots, bucket, key):
    result = {}
    for index, drives in enumerate(roots):
        found = []
        for root in drives:
            path = Path(root) / bucket / key / "xl.meta"
            if path.is_file():
                found.append(hashlib.sha256(path.read_bytes()).hexdigest())
        if found:
            result[index] = tuple(found)
    return result


def changed_pool(before, after):
    changed = [pool for pool in set(before) | set(after) if before.get(pool) != after.get(pool)]
    assert len(changed) == 1, f"expected one metadata writer pool, observed {changed}"
    return changed[0]


@contextmanager
def newest_pool_min_free(mc, artifacts, value):
    assert isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 99, "newest_pool_min_free must be 1..99"
    original = parse_config(mc.run("admin", "config", "get", "src", "drive"), "drive",
                            mutable_env={"MINIO_DRIVE_NEWEST_POOL_MIN_FREE": None})
    pending = artifacts / "pending-admin-restore.json"
    assert not pending.exists(), "an earlier admin change still needs recovery"
    pending.write_text(json.dumps({"subsystem": "drive", "arguments": original[1:]}), encoding="utf-8")
    try:
        mc.run("admin", "config", "set", "src", "drive", f"newest_pool_min_free={value}")
        active = parse_config(mc.run("admin", "config", "get", "src", "drive"), "drive")
        assert f"newest_pool_min_free={value}" in active
        yield
    finally:
        mc.run("admin", "config", "set", "src", *original)
        assert sorted(parse_config(mc.run("admin", "config", "get", "src", "drive"), "drive")) == sorted(original)
        pending.unlink()
