#!/usr/bin/env bash
set -euo pipefail
runner_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$runner_dir/_bootstrap.py" ]]; then
    exec python3 -I "$runner_dir/_bootstrap.py" runner "$@"
fi
exec python3 -I "$runner_dir/minio_test_runner.py" "$@"
