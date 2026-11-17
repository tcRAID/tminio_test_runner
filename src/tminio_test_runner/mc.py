"""MinIO Client adapter shared by lab fixtures and recovery."""
import os
import subprocess
from urllib.parse import quote, urlsplit, urlunsplit


class Mc:
    source_alias = "src"
    target_alias = "dst"

    def __init__(self, executable, config_dir, settings):
        self.executable, self.config_dir, self.settings = executable, config_dir, settings

    def replicate(self, source_bucket, target_bucket):
        target = self.settings["target"]
        parsed = urlsplit(target.get("replication_endpoint", target["endpoint"]))
        access = quote(os.environ[target.get("access_key_env", "TMINIO_TEST_ACCESS_KEY")], safe="")
        secret = quote(os.environ[target.get("secret_key_env", "TMINIO_TEST_SECRET_KEY")], safe="")
        remote = urlunsplit((parsed.scheme, f"{access}:{secret}@{parsed.netloc}", "/" + target_bucket, "", ""))
        return self.run("replicate", "add", "src/" + source_bucket, "--remote-bucket", remote,
                        "--replicate", "delete,delete-marker,existing-objects,replica-metadata-sync", "--priority", "1")

    def run(self, *args, timeout=120):
        try:
            result = subprocess.run([self.executable, "--config-dir", str(self.config_dir), *args],
                                    shell=False, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            raise AssertionError("mc command timed out or could not start; arguments withheld") from None
        if result.returncode:
            # Args/stderr may contain a credential-bearing replication URL.
            raise AssertionError(f"mc {args[0] if args else ''} failed (exit {result.returncode}); output withheld to protect credentials")
        return result.stdout
