"""Local bundle packaging and integrity guards; no downloads or MinIO processes."""
import hashlib
import json
import tarfile
import zipfile

import pytest

import release_bootstrap
import package_release


def wheel(path, members):
    with zipfile.ZipFile(path, "w") as output:
        for name, data in members.items():
            output.writestr(name, data)


@pytest.mark.parametrize("name", ["../outside", "/absolute", "package.data/purelib/anything"])
def test_wheel_rejects_paths_outside_plain_library_layout(tmp_path, name):
    path = tmp_path / "bad.whl"
    wheel(path, {name: "unsafe"})
    with pytest.raises(ValueError, match="unexpected wheel path"):
        package_release.unpack_wheel(path, tmp_path / "vendor")
    assert not (tmp_path / "outside").exists()


def test_manifest_rejects_tampering_and_path_escape(tmp_path):
    library = tmp_path / "library.py"
    library.write_text("expected")
    manifest = tmp_path / "bundle-manifest.json"
    manifest.write_text(json.dumps({"files": {"library.py": package_release.digest(library)}}))
    release_bootstrap.verify_bundle(tmp_path)
    library.write_text("modified")
    with pytest.raises(ValueError, match="integrity check failed"):
        release_bootstrap.verify_bundle(tmp_path)
    manifest.write_text(json.dumps({"files": {"../outside": "0" * 64}}))
    with pytest.raises(ValueError, match="invalid bundle manifest path"):
        release_bootstrap.verify_bundle(tmp_path)


def test_archive_omits_local_secrets_and_includes_verified_dependencies(tmp_path, monkeypatch):
    source = tmp_path / "source" / "suite"
    source.mkdir(parents=True)
    (source.parent / "LICENSE").write_text("license")
    (source.parent / "run.sh").write_text('exec python3 -I "$_bootstrap.py" runner')
    monkeypatch.setattr(package_release, "ROOT_FILES", ["LICENSE", "run.sh"])
    (source / "run.py").write_text("print('test fixture')")
    (source / "release_bootstrap.py").write_text("# fixture only")
    (source / "requirements-release.txt").write_text("example==1\npsutil==7.2.2\n")
    (source / "config.local.json").write_text('secret-access-key')
    (source / "server.private.log").write_text('secret-token')
    monkeypatch.setattr(package_release, "__file__", str(source / "package_release.py"))
    monkeypatch.setattr(package_release, "SOURCE_FILES", ["run.py", "requirements-release.txt"])

    def fake_download(requirements, destination, **options):
        destination.mkdir(parents=True, exist_ok=True)
        if options:
            name, member = "psutil-7.2.2-cp36-abi3-test.whl", "psutil/__init__.py"
        else:
            name, member = "example-1-py3-none-any.whl", "example/__init__.py"
        wheel(destination / name, {member: "# fixture dependency"})

    monkeypatch.setattr(package_release, "download", fake_download)
    output = tmp_path / "out" / "suite.tar.gz"
    package_release.build(output)
    with tarfile.open(output) as archive:
        names = archive.getnames()
        assert not any("config.local" in n or "server.private" in n for n in names)
        root = "tminio_test_runner/"
        assert root + "vendor/example/__init__.py" in names
        for arch in ("x86_64", "aarch64"):
            assert root + f"vendor-native/{arch}/psutil/__init__.py" in names
        manifest = json.load(archive.extractfile(root + "bundle-manifest.json"))
        for name, expected in manifest["files"].items():
            assert hashlib.sha256(archive.extractfile(root + name).read()).hexdigest() == expected
        assert archive.getmember(root + "run.sh").mode == 0o755
        assert b'_bootstrap.py" runner' in archive.extractfile(root + "run.sh").read()
        assert all(not m.issym() and not m.islnk() and m.uid == m.gid == 0 for m in archive.getmembers())
    assert output.with_suffix(".gz.sha256").read_text().startswith(package_release.digest(output))
    with pytest.raises(ValueError, match="output already exists"):
        package_release.build(output)


def dependency_bundle(root, *, version="1"):
    files = {}
    for directory, name, release in (("vendor", "example", version),
                                     ("vendor-native/x86_64", "psutil", "7.2.2"),
                                     ("vendor-native/aarch64", "psutil", "7.2.2")):
        for relative, content in ((f"{name}/__init__.py", "# library"),
                                  (f"{name}-{release}.dist-info/METADATA",
                                   f"Name: {name}\nVersion: {release}\n")):
            path = root / directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            files[str(path.relative_to(root))] = package_release.digest(path)
    (root / "bundle-manifest.json").write_text(json.dumps({"files": files}))


def test_reuses_only_verified_version_matched_dependencies(tmp_path):
    previous = tmp_path / "previous"
    dependency_bundle(previous)
    target = tmp_path / "rebuilt"
    package_release.reuse_dependencies(previous, target, {"example": "1", "psutil": "7.2.2"})
    assert (target / "vendor/example/__init__.py").read_text() == "# library"
    for arch in ("x86_64", "aarch64"):
        assert (target / f"vendor-native/{arch}/psutil/__init__.py").is_file()
    assert not (target / "bundle-manifest.json").exists()


@pytest.mark.parametrize("change", ["version", "extra", "modified", "symlink"])
def test_dependency_reuse_rejects_wrong_versions_and_unverified_files(tmp_path, change):
    previous = tmp_path / "previous"
    dependency_bundle(previous, version="2" if change == "version" else "1")
    if change == "extra":
        (previous / "vendor/injected.py").write_text("unlisted")
    if change == "modified":
        (previous / "vendor/example/__init__.py").write_text("changed")
    if change == "symlink":
        (previous / "vendor/alias").symlink_to(previous / "vendor/example", target_is_directory=True)
    with pytest.raises(ValueError):
        package_release.reuse_dependencies(previous, tmp_path / "rebuilt",
                                           {"example": "1", "psutil": "7.2.2"})


def test_packaged_sources_use_current_release_names():
    assert "test_endpoint_cases_unit.py" in package_release.SOURCE_FILES
    assert "test_endpoint_teardown_unit.py" in package_release.SOURCE_FILES
    assert not any("offline" in name or "migration" in name for name in package_release.SOURCE_FILES)
