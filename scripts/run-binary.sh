#!/usr/bin/env bash
# One command; Python dependencies are isolated from the system interpreter.
set -euo pipefail
suite_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -eq 0 ]]; then
    printf 'Usage: %s /path/to/minio [--work-dir /path/on/test-disk]\n' "$0"
    exit 2
fi
if [[ "${1}" == "--help" || "${1}" == "-h" ]]; then
    printf 'Usage: %s /path/to/minio [--work-dir DIRECTORY] [--timeout SECONDS]\n\n' "$0"
    printf 'Starts a private localhost MinIO, runs functional checks, restarts it, and verifies its data.\n'
    if [[ -f "$suite_dir/../bundle-manifest.json" ]]; then
        printf 'Needs Python 3.10+ and about 4 GiB free disk space. Dependencies are bundled; no internet is needed.\n'
    else
        printf 'Needs Python 3.10+, about 4 GiB free disk space, and network access for first-time dependency installation.\n'
    fi
    exit 0
fi
if ! command -v python3 >/dev/null; then
    printf 'Install Python first: sudo apt-get install python3 python3-venv\n' >&2
    exit 2
fi
if [[ ! -f "$1" || ! -x "$1" ]]; then
    printf 'MinIO binary must be an executable file: %s\n' "$1" >&2
    exit 2
fi
exec python3 -I "$suite_dir/bootstrap.py" binary "$@"
