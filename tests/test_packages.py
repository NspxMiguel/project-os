"""The Advanced-mode package manager: search, install jobs, and the refusals.

Nothing here installs anything. The subprocess boundary is the seam: the tests
check the argv that *would* run, which is where the dangerous mistakes live
(a package name that is really an option, a missing privilege prefix), and drive
the job runner with a harmless command to prove the log and state machine work.
"""

from __future__ import annotations

import sys
import time

import pytest

from project_os.core import packages


# --------------------------------------------------------------------- names
@pytest.mark.parametrize("name", ["firefox", "python3-pip", "libreoffice-calc", "gcc-12:arm64"])
def test_valid_apt_names_pass(name: str) -> None:
    assert packages.check_name("apt", name) == name


@pytest.mark.parametrize(
    "name",
    [
        "--reinstall",          # an option wearing a package's clothes
        "firefox; rm -rf /",    # only dangerous with a shell, refused anyway
        "fire fox",
        "../../etc/passwd",
        "",
        "  ",
    ],
)
def test_dangerous_apt_names_are_refused(name: str) -> None:
    with pytest.raises(packages.PackageError) as excinfo:
        packages.check_name("apt", name)
    assert excinfo.value.code == "bad_package_name"


def test_flatpak_ids_pass() -> None:
    assert packages.check_name("flatpak", "org.mozilla.firefox") == "org.mozilla.firefox"


# ------------------------------------------------------------------- commands
def test_apt_install_never_uses_a_shell_and_is_noninteractive(monkeypatch) -> None:
    monkeypatch.setattr(packages, "_is_root", lambda: True)
    argv = packages.install_argv("apt", "firefox")
    assert argv[0] == "apt-get"
    assert "-y" in argv
    assert argv[-1] == "firefox"
    assert all(isinstance(part, str) for part in argv)


def test_apt_uses_sudo_when_not_root(monkeypatch) -> None:
    monkeypatch.setattr(packages, "_is_root", lambda: False)
    monkeypatch.setattr(packages, "_sudo_without_password", lambda *_: True)
    assert packages.install_argv("apt", "firefox")[:2] == ["sudo", "-n"]


def test_apt_refuses_before_running_when_root_is_impossible(monkeypatch) -> None:
    """Better a refusal with a reason than a permission error halfway through."""
    monkeypatch.setattr(packages, "_is_root", lambda: False)
    monkeypatch.setattr(packages, "_sudo_without_password", lambda *_: False)
    with pytest.raises(packages.PackageError) as excinfo:
        packages.install_argv("apt", "firefox")
    assert excinfo.value.code == "needs_root"
    assert "sudo" in excinfo.value.hint


def test_sudo_probe_asks_about_the_command_not_about_true(monkeypatch) -> None:
    """The image grants sudo for apt-get and nothing else, on purpose.

    The probe used to run ``sudo -n true``, which that sudoers denies, so the
    package manager declared itself powerless on the one machine where it
    actually works. It has to ask about the command it means to run.
    """
    asked = []

    class Result:
        def __init__(self, code): self.returncode = code

    def fake_run(argv, **kwargs):
        asked.append(argv)
        # A sudoers that permits apt-get and refuses everything else.
        return Result(0 if argv[-1] == "apt-get" else 1)

    monkeypatch.setattr(packages, "_have", lambda command: True)
    monkeypatch.setattr(packages.subprocess, "run", fake_run)
    packages.reset_privilege_cache()

    assert packages._sudo_without_password("apt-get") is True
    assert packages._sudo_without_password("flatpak") is False
    assert asked[0] == ["sudo", "-n", "-l", "apt-get"]
    assert "true" not in [argv[-1] for argv in asked]

    # Asked once per command, not once per install.
    before = len(asked)
    packages._sudo_without_password("apt-get")
    assert len(asked) == before
    packages.reset_privilege_cache()


def test_apt_is_usable_under_the_images_narrow_sudoers(monkeypatch) -> None:
    """backends() must report apt as usable there, not as impossible."""
    class Result:
        def __init__(self, code): self.returncode = code

    monkeypatch.setattr(packages, "_is_root", lambda: False)
    monkeypatch.setattr(packages, "_have", lambda command: command != "flatpak")
    monkeypatch.setattr(
        packages.subprocess, "run",
        lambda argv, **kwargs: Result(0 if argv[-1] == "apt-get" else 1),
    )
    packages.reset_privilege_cache()
    apt = [item for item in packages.backends() if item["id"] == "apt"][0]
    packages.reset_privilege_cache()
    assert apt["can_install"] is True
    assert apt["reason"] == ""


def test_flatpak_needs_no_privilege(monkeypatch) -> None:
    monkeypatch.setattr(packages, "_is_root", lambda: False)
    monkeypatch.setattr(packages, "_sudo_without_password", lambda *_: False)
    argv = packages.install_argv("flatpak", "org.mozilla.firefox")
    assert argv[0] == "flatpak" and "-y" in argv


def test_unknown_source_is_refused() -> None:
    with pytest.raises(packages.PackageError) as excinfo:
        packages.install_argv("brew", "firefox")
    assert excinfo.value.code == "unknown_source"


def test_environment_stops_apt_from_waiting_for_a_keypress() -> None:
    env = packages._environment()
    assert env["DEBIAN_FRONTEND"] == "noninteractive"


# --------------------------------------------------------------------- search
def test_search_needs_something_to_search_for() -> None:
    with pytest.raises(packages.PackageError) as excinfo:
        packages.search("f")
    assert excinfo.value.code == "query_too_short"


def test_apt_search_parses_and_flags_installed(monkeypatch) -> None:
    monkeypatch.setattr(packages, "_have", lambda cmd: True)
    monkeypatch.setattr(
        packages, "_run",
        lambda argv, timeout=0: "firefox - Mozilla Firefox web browser\nbroken line\n",
    )
    monkeypatch.setattr(packages, "_apt_installed_names", lambda: {"firefox"})
    found = packages.search_apt("firefox")
    assert len(found) == 1
    assert found[0]["name"] == "firefox"
    assert found[0]["summary"] == "Mozilla Firefox web browser"
    assert found[0]["installed"] is True


def test_search_says_which_backends_are_missing(monkeypatch) -> None:
    """"0 results" and "there is no apt here" look the same and are not."""
    monkeypatch.setattr(packages, "_have", lambda cmd: False)
    result = packages.search("firefox")
    assert result["items"] == []
    sources = [entry["source"] for entry in result["skipped"]]
    assert "apt" in sources and "flatpak" in sources
    assert all(entry["reason"] for entry in result["skipped"])


def test_backends_report_honestly_on_a_machine_without_apt(monkeypatch) -> None:
    monkeypatch.setattr(packages, "_have", lambda cmd: False)
    apt = [b for b in packages.backends() if b["id"] == "apt"][0]
    assert apt["present"] is False
    assert apt["can_install"] is False
    assert apt["reason"]


# ------------------------------------------------------------------- the runner
def _fake_job(runner: packages.JobRunner, argv, action="install"):
    """Start a job whose command is a harmless local process."""
    job = packages.Job(action, "apt", "fake-package", argv)
    with runner._lock:
        runner._jobs[job.id] = job
        runner._order.append(job.id)
        runner._current = job.id
    runner._run(job)
    return job


def test_a_successful_job_keeps_its_log_and_ends_done() -> None:
    runner = packages.JobRunner()
    job = _fake_job(runner, [sys.executable, "-c", "print('unpacking bird seed')"])
    assert job.state == packages.STATE_DONE
    assert job.returncode == 0
    assert any("bird seed" in line for line in job.log_lines())
    assert job.finished_at
    assert runner.busy() is False


def test_a_failing_job_reports_the_last_line_it_printed() -> None:
    runner = packages.JobRunner()
    job = _fake_job(
        runner,
        [sys.executable, "-c", "import sys; print('E: package not found'); sys.exit(100)"],
    )
    assert job.state == packages.STATE_ERROR
    assert job.returncode == 100
    assert "package not found" in job.message


def test_the_log_is_capped(monkeypatch) -> None:
    """A long apt run prints thousands of lines; a Pi 3B has 1 GB."""
    monkeypatch.setattr(packages, "LOG_LINES", 10)
    job = packages.Job("install", "apt", "x", [])
    for index in range(50):
        job.append("line %d" % index)
    lines = job.log_lines()
    assert len(lines) == 10
    assert lines[-1] == "line 49"


def test_two_jobs_at_once_are_refused(monkeypatch) -> None:
    """apt takes a machine-wide lock; a second run would fail on the first."""
    runner = packages.JobRunner()
    monkeypatch.setattr(packages, "_require_backend", lambda source: {"id": source})
    monkeypatch.setattr(packages, "install_argv", lambda source, name: [sys.executable, "-c", "import time; time.sleep(2)"])

    runner.start("install", "apt", "slow-package")
    deadline = time.monotonic() + 2
    while not runner.busy() and time.monotonic() < deadline:
        time.sleep(0.01)

    with pytest.raises(packages.PackageError) as excinfo:
        runner.start("install", "apt", "another-package")
    assert excinfo.value.code == "busy"
    assert "slow-package" in excinfo.value.message


def test_events_are_emitted_for_start_and_finish() -> None:
    seen = []
    runner = packages.JobRunner(on_event=lambda topic, payload: seen.append((topic, payload["kind"])))
    _fake_job(runner, [sys.executable, "-c", "pass"])
    assert ("packages", "started") in seen
    assert ("packages", "finished") in seen


def test_a_broken_listener_does_not_kill_the_job() -> None:
    def explode(topic, payload):
        raise RuntimeError("no")

    runner = packages.JobRunner(on_event=explode)
    job = _fake_job(runner, [sys.executable, "-c", "pass"])
    assert job.state == packages.STATE_DONE


# ----------------------------------------------------------------------- HTTP
def test_overview_lists_backends(auth_client) -> None:
    response = auth_client.get("/api/packages")
    assert response.status_code == 200
    body = response.json()
    assert [b["id"] for b in body["backends"]] == ["apt", "flatpak"]
    assert body["enabled"] is True


def test_reads_need_a_session(client) -> None:
    client.post("/api/setup", json={"username": "miguel", "password": "correct horse battery"})
    client.post("/api/auth/logout")
    assert client.get("/api/packages").status_code == 401


def test_install_is_refused_when_the_setting_is_off(auth_client) -> None:
    auth_client.put("/api/settings", json={"values": {"security.allow_package_management": False}})
    response = auth_client.post("/api/packages/install", json={"source": "apt", "package": "firefox"})
    assert response.status_code == 403
    assert response.json()["error"] == "package_management_disabled"


def test_install_refuses_a_bad_name_before_touching_apt(auth_client) -> None:
    response = auth_client.post(
        "/api/packages/install", json={"source": "apt", "package": "--reinstall"}
    )
    assert response.status_code in (400, 403, 409)
    assert response.json()["error"] in ("bad_package_name", "backend_unavailable", "needs_root")


def test_search_rejects_a_one_letter_query(auth_client) -> None:
    assert auth_client.get("/api/packages/search?q=f").status_code == 422


def test_unknown_job_is_a_404(auth_client) -> None:
    response = auth_client.get("/api/packages/jobs/does-not-exist")
    assert response.status_code == 404
