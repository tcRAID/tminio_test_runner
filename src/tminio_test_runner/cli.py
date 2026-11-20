#!/usr/bin/env python3
"""One-command system tests for an already running MinIO endpoint."""
import subprocess
import sys

HELP = """Usage: ./run.sh [http://HOST:9000]

Runs all 20 current-service checks with automatic setup and teardown.
Endpoint and credentials are prompted when omitted. HTTP is supported.
Automation: MINIO_ENDPOINT, MINIO_ACCESS_KEY and MINIO_SECRET_KEY.

Other commands:
  ./run.sh soak http://HOST:9000   Bounded 5-minute, 32-client workload
  ./run.sh unit                   Local harness regression tests
  ./run.sh lab --list             Advanced tests requiring a disposable lab
  ./run.sh endpoint --help        Optional endpoint settings and defaults

Reports remain in the printed minio-check-* directory. No pre-created data,
MinIO binary, service restart or JSON configuration is needed for the default run.
The release archive includes dependencies and runs without internet access.
"""



def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(HELP)
        return 0
    if args[:1] in (["endpoint"], ["soak"]) and any(a in ("-h", "--help") for a in args[1:]):
        print(HELP + "\nS3 endpoint options (optional):\n"
              "  --timeout 1800 --cleanup-timeout 300 --lifecycle-timeout 600\n"
              "  --read-timeout 60 (SDK socket reads)\n"
              "  --work-dir . --region us-east-1 --expected-commit ID --ca-file CA.pem\n"
              "  soak only: --duration 300 --workers 32 (timeout grows automatically)\n"
              "  soak retries HTTP 503 at most twice and reports every retry\n")
        return 0
    if args and args[0] in ("source", "smoke"):
        print("Source builds and local-server smoke were retired. Use ./run.sh http://HOST:9000 against a running service.", file=sys.stderr)
        return 2
    mode = args.pop(0) if args and args[0] in ("endpoint", "soak", "unit", "lab") else "endpoint"
    if mode in ("endpoint", "soak"):
        from .endpoint_check import main as endpoint_main
        return endpoint_main(args, soak=mode == "soak")
    lab_args = ["unit", *args] if mode == "unit" else (args or ["--list"])
    if mode == "lab" and lab_args[:1] == ["all"]:
        lab_args.pop(0)
    sys.argv = ["tminio_test_runner.lab_runner", *lab_args]
    from .lab_runner import main as lab_main
    return lab_main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Cannot prepare test runtime: {error}. Use the bundled tar.gz on nodes without internet.", file=sys.stderr)
        raise SystemExit(2)
