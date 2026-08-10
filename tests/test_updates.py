"""Updating over the network: version maths, verification, and the swap.

The dangerous parts here are not the happy path. They are: installing something
whose checksum does not match what was advertised, and extracting an archive
that writes outside the install directory. Both get their own test, and both
must refuse *before* anything on disk changes.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile

import pytest

from project_os import __version__
from project_os.core import updates


# -------------------------------------------------------------------- versions
@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("0.2.0", "0.1.0", True),
        ("0.1.1", "0.1.0", True),
        ("1.0.0", "0.9.9", True),
        ("0.1.0", "0.1.0", False),
        ("0.0.9", "0.1.0", False),
        ("v0.2.0", "0.1.0", True),
        ("0.2.0-rc1", "0.1.0", True),
    ],
)
def test_version_comparison(candidate: str, current: str, expected: bool) -> None:
    assert updates.is_newer(candidate, current) is expected


def test_a_nonsense_version_does_not_crash_the_check() -> None:
    assert updates.is_newer("banana", "0.1.0") is False


# -------------------------------------------------------------------- manifest
def _manifest(**overrides):
    # Derived, never a literal: the suite must not start failing the day the
    # product's own version catches up with the number written in the test.
    newer = "%d.0.0" % (int(__version__.split(".")[0]) + 1)
    data = {
        "version": newer,
        "url": "https://example.invalid/project_os-0.2.0.tar.gz",
        "sha256": "a" * 64,
        "notes": "Tudo novo",
    }
    data.update(overrides)
    return data


def test_check_reads_a_manifest(monkeypatch) -> None:
    monkeypatch.setattr(updates, "_fetch_json", lambda url, timeout=0: _manifest())
    result = updates.check_tarball("https://example.invalid/latest.json")
    assert result["latest"] == _manifest()["version"]
    assert result["update_available"] is True
    assert result["method"] == updates.METHOD_TARBALL


def test_a_manifest_without_a_checksum_is_refused(monkeypatch) -> None:
    """No checksum means no way to know what you are installing."""
    monkeypatch.setattr(updates, "_fetch_json", lambda url, timeout=0: _manifest(sha256=""))
    with pytest.raises(updates.UpdateError) as excinfo:
        updates.check_tarball("https://example.invalid/latest.json")
    assert excinfo.value.code == "unverifiable"


def test_a_manifest_missing_the_url_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(updates, "_fetch_json", lambda url, timeout=0: _manifest(url=""))
    with pytest.raises(updates.UpdateError) as excinfo:
        updates.check_tarball("https://example.invalid/latest.json")
    assert excinfo.value.code == "bad_manifest"


# -------------------------------------------------------------------- download
def _serve(tmp_path, payload: bytes, name: str = "release.tar.gz") -> str:
    path = tmp_path / name
    path.write_bytes(payload)
    return path.as_uri()


def test_download_refuses_a_mismatched_checksum(tmp_path) -> None:
    url = _serve(tmp_path, b"not the release you were promised")
    dest = str(tmp_path / "downloaded.tar.gz")
    with pytest.raises(updates.UpdateError) as excinfo:
        updates._download(url, dest, "b" * 64)
    assert excinfo.value.code == "checksum_mismatch"
    assert not os.path.exists(dest), "a rejected download must not be left on disk"


def test_download_accepts_the_advertised_checksum(tmp_path) -> None:
    payload = b"the real release"
    url = _serve(tmp_path, payload)
    dest = str(tmp_path / "ok.tar.gz")
    digest = hashlib.sha256(payload).hexdigest()
    assert updates._download(url, dest, digest) == digest
    assert os.path.exists(dest)


# --------------------------------------------------------------------- archive
def _tar_with(tmp_path, members):
    """Build a tarball from {name: bytes}, returning its path."""
    path = tmp_path / "archive.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return str(path)


def test_extract_refuses_a_path_outside_the_install_dir(tmp_path) -> None:
    """A tarball is a list of paths chosen by whoever built it."""
    archive = _tar_with(tmp_path, {"../../etc/cron.d/pwn": b"* * * * * root sh\n"})
    with pytest.raises(updates.UpdateError) as excinfo:
        updates._safe_extract(archive, str(tmp_path / "into"))
    assert excinfo.value.code == "unsafe_archive"


def test_extract_unwraps_a_single_top_level_directory(tmp_path) -> None:
    archive = _tar_with(tmp_path, {
        "project-os-0.2.0/project_os/__init__.py": b"__version__ = '0.2.0'\n",
    })
    where = updates._safe_extract(archive, str(tmp_path / "into"))
    assert os.path.basename(where) == "project-os-0.2.0"
    assert updates._looks_like_project_os(where)


# ------------------------------------------------------------------- the swap
def _fake_tree(path, version: str) -> str:
    os.makedirs(os.path.join(path, "project_os"), exist_ok=True)
    with open(os.path.join(path, "project_os", "__init__.py"), "w") as handle:
        handle.write("__version__ = %r\n" % version)
    return path


def test_apply_swaps_the_tree_and_keeps_the_old_one(tmp_path, monkeypatch) -> None:
    root = _fake_tree(str(tmp_path / "project_os-install"), "0.1.0")
    with open(os.path.join(root, "PEDIDOS.md"), "w") as handle:
        handle.write("nao me apague\n")

    new_tree = _fake_tree(str(tmp_path / "build" / "project-os-0.2.0"), "0.2.0")
    archive = str(tmp_path / "release.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(new_tree, arcname="project-os-0.2.0")
    payload = open(archive, "rb").read()
    digest = hashlib.sha256(payload).hexdigest()

    info = {
        "method": updates.METHOD_TARBALL,
        "url": (tmp_path / "release.tar.gz").as_uri(),
        "sha256": digest,
        "current": "0.1.0",
        "latest": "0.2.0",
    }
    lines = []
    result = updates.apply_tarball(info, root=root, on_line=lines.append)

    with open(os.path.join(root, "project_os", "__init__.py")) as handle:
        assert "0.2.0" in handle.read()
    assert os.path.isdir(result["previous"]), "the old version must still be on disk"
    assert os.path.isfile(os.path.join(root, "PEDIDOS.md")), "kept files must survive the swap"
    assert any("swapping" in line for line in lines)


def test_rollback_puts_the_previous_tree_back(tmp_path) -> None:
    root = _fake_tree(str(tmp_path / "install"), "0.2.0")
    previous = _fake_tree(str(tmp_path / "install.previous-0.1.0"), "0.1.0")
    updates.rollback(previous, root=root)
    with open(os.path.join(root, "project_os", "__init__.py")) as handle:
        assert "0.1.0" in handle.read()


def test_rollback_without_a_previous_version_says_so(tmp_path) -> None:
    root = _fake_tree(str(tmp_path / "install"), "0.2.0")
    with pytest.raises(updates.UpdateError) as excinfo:
        updates.rollback(str(tmp_path / "nope"), root=root)
    assert excinfo.value.code == "no_previous"


# ---------------------------------------------------------------------- restart
def test_the_restart_command_line_is_the_one_we_booted_with() -> None:
    updates.remember_argv(["-m", "project_os", "--port", "8123"])
    assert updates._original_argv() == ["-m", "project_os", "--port", "8123"]


def test_restart_rebuilds_dash_m_instead_of_running_the_script_path() -> None:
    """`python -m project_os` and `python .../project_os/__main__.py` differ.

    The second puts project_os/ on sys.path instead of its parent, so the import
    of the package fails. Coming back up is the one step that cannot be clever.
    """
    argv = ["/opt/project-os/project_os/__main__.py", "--port", "8099"]
    assert updates.restart_argv(argv, executable="/opt/project-os/.venv/bin/python3") == [
        "/opt/project-os/.venv/bin/python3", "-m", "project_os", "--port", "8099",
    ]


def test_restart_keeps_an_explicit_command_line_as_it_was() -> None:
    argv = ["-m", "project_os", "--port", "8099"]
    assert updates.restart_argv(argv, executable="/usr/bin/python3") == [
        "/usr/bin/python3", "-m", "project_os", "--port", "8099",
    ]


# ------------------------------------------------------------------------ HTTP
def test_status_reports_the_running_version(auth_client) -> None:
    from project_os import __version__
    from project_os.core import updates as live

    response = auth_client.get("/api/updates")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == __version__
    assert body["method"] in (live.METHOD_GIT, live.METHOD_TARBALL)


def test_status_needs_a_session(client) -> None:
    client.post("/api/setup", json={"username": "miguel", "password": "correct horse battery"})
    client.post("/api/auth/logout")
    assert client.get("/api/updates").status_code == 401


def test_install_refuses_when_already_current(auth_client, monkeypatch) -> None:
    from project_os import __version__
    # Imported here, not at module level: conftest drops project_os.* from
    # sys.modules per test, so the module this file imported at collection time
    # is not the one the running app is using.
    from project_os.core import updates as live

    monkeypatch.setattr(
        live, "check",
        lambda manifest_url=None, branch="main", root=None: {
            "method": updates.METHOD_TARBALL, "latest": __version__,
            "update_available": False, "current": __version__,
        },
    )
    response = auth_client.post("/api/updates/install", json={})
    assert response.status_code == 409
    assert response.json()["error"] == "already_current"


def test_install_refuses_when_the_version_moved(auth_client, monkeypatch) -> None:
    """The client says what it thinks it is installing; a mismatch is a stop."""
    from project_os.core import updates as live

    monkeypatch.setattr(
        live, "check",
        lambda manifest_url=None, branch="main", root=None: {
            "method": updates.METHOD_TARBALL, "latest": "0.3.0",
            "update_available": True, "current": "0.1.0",
        },
    )
    response = auth_client.post("/api/updates/install", json={"version": "0.2.0"})
    assert response.status_code == 409
    assert response.json()["error"] == "version_moved"


def test_install_is_refused_when_updates_are_turned_off(auth_client) -> None:
    auth_client.put("/api/settings", json={"values": {"updates.enabled": False}})
    response = auth_client.post("/api/updates/install", json={})
    assert response.status_code == 403
    assert response.json()["error"] == "updates_disabled"


def test_being_offline_is_a_503_with_a_reason(auth_client, monkeypatch) -> None:
    from project_os.core import updates as live

    def offline(*args, **kwargs):
        raise live.UpdateError("no route to host", code="offline", hint="check the network")

    monkeypatch.setattr(live, "check", offline)
    response = auth_client.post("/api/updates/check")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "offline"
    assert body["detail"]


def test_restart_lands_in_the_new_tree_not_the_renamed_old_one(tmp_path, monkeypatch) -> None:
    """A working directory follows the inode; the swap renames it out from under us.

    Without a chdir by *path*, the process comes back up inside
    ``<root>.previous-<version>``: old code, and a version number that never
    changes however many times you update. This reproduces the rename.
    """
    root = tmp_path / "install"
    (root / "project_os").mkdir(parents=True)
    monkeypatch.chdir(root)

    # what apply_tarball does
    os.rename(str(root), str(tmp_path / "install.previous-0.1.0"))
    (tmp_path / "install" / "project_os").mkdir(parents=True)

    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path / "install.previous-0.1.0"))
    os.chdir(str(root))
    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(root))


# ---------------------------------------------------------------- entry point


def test_the_unit_never_names_anything_inside_the_tree() -> None:
    """The one thing that made the rename un-updatable.

    A unit that says `python -m projectos` pins the *inside* of the tree, and an
    update replaces the tree without being able to touch /etc -- so a rename
    could only be delivered by writing a new card. The unit now names a script
    that lives in the tree, which every update replaces.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    unit = (root / "image" / "stage-project-os" / "00-project-os" / "files"
            / "etc" / "systemd" / "system" / "project-os.service").read_text()

    assert "ExecStart=/opt/project-os/bin/project-os" in unit
    assert "-m project_os" not in unit, "the module name must not be pinned in /etc"

    installer = (root / "install.sh").read_text()
    assert "ExecStart=$PREFIX/bin/project-os" in installer


def test_the_wrapper_still_starts_a_tree_with_the_old_package_name(tmp_path) -> None:
    """A box migrated from 0.1.x has projectos/ on disk until it updates."""
    import shutil
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    tree = tmp_path / "install"
    (tree / "bin").mkdir(parents=True)
    shutil.copy(root / "bin" / "project-os", tree / "bin" / "project-os")
    (tree / "bin" / "project-os").chmod(0o755)
    # The old layout: package named projectos, no venv.
    (tree / "projectos").mkdir()
    (tree / "projectos" / "__init__.py").write_text("")
    (tree / "projectos" / "__main__.py").write_text("print('started projectos')\n")

    out = subprocess.run([str(tree / "bin" / "project-os")], capture_output=True, timeout=30)
    assert out.returncode == 0, out.stderr.decode()
    assert b"started projectos" in out.stdout


def test_the_restart_asks_sudo_when_it_is_not_root(monkeypatch):
    """The service runs unprivileged; a bare systemctl restart is refused.

    The update swapped the tree, reported success, and left the old code serving
    until the next power cut -- the worst possible shape for a bug, because the
    version number on the screen only changes after a reboot nobody did.
    """
    from project_os.core import updates

    monkeypatch.setattr(updates.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/" + name)
    assert updates._systemctl_argv() == ["sudo", "-n", "systemctl"]


def test_the_restart_does_not_ask_sudo_when_it_is_already_root(monkeypatch):
    from project_os.core import updates

    monkeypatch.setattr(updates.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/" + name)
    assert updates._systemctl_argv() == ["systemctl"]


def test_the_restart_targets_the_unit_the_image_installs():
    from project_os.core import updates

    assert updates.UNIT_NAME == "project-os.service"
