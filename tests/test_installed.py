"""Reading what is already on the machine, with the commands faked.

Every test replaces ``installed._run`` with a dict lookup, so these run the same
on a Mac laptop and on a Pi with docker, flatpak and four hundred packages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_os.core import installed


@pytest.fixture()
def fake_commands(monkeypatch):
    """Answer specific argv[0]s, and "the tool is not installed" for the rest."""
    outputs = {}

    def run(argv):
        return outputs.get(argv[0], "")

    monkeypatch.setattr(installed, "_run", run)
    return outputs


# -------------------------------------------------------------------- sources
def test_apt_lists_what_a_person_chose_and_skips_the_libraries(fake_commands) -> None:
    fake_commands["apt-mark"] = "jellyfin\nlibfoo1\npython3-yaml\nvim\n"
    fake_commands["dpkg-query"] = "jellyfin\t10.8.13\nvim\t2:9.0\n"
    found = installed.apt_packages()
    assert [item["id"] for item in found] == ["jellyfin", "vim"]
    assert found[0]["version"] == "10.8.13"
    assert found[0]["kind"] == installed.KIND_PACKAGE


def test_a_package_that_is_in_the_store_is_linked_to_it(fake_commands) -> None:
    """So the store says "already installed" instead of offering a second copy."""
    fake_commands["apt-mark"] = "jellyfin\n"
    assert installed.apt_packages()[0]["catalog_id"] == "jellyfin"


def test_something_not_in_the_store_is_still_listed(fake_commands) -> None:
    fake_commands["apt-mark"] = "some-thing-nobody-packaged\n"
    found = installed.apt_packages()
    assert len(found) == 1
    assert found[0]["catalog_id"] is None


def test_flatpak_columns_are_read_in_order(fake_commands) -> None:
    fake_commands["flatpak"] = "org.mozilla.firefox\tFirefox\t124.0\n"
    found = installed.flatpaks()
    assert found[0]["id"] == "org.mozilla.firefox"
    assert found[0]["name"] == "Firefox"
    assert found[0]["version"] == "124.0"


def test_the_snap_header_row_is_not_an_app(fake_commands) -> None:
    fake_commands["snap"] = "Name  Version  Rev\ncore22  20240111  1122\n"
    assert [item["id"] for item in installed.snaps()] == ["core22"]


def test_containers_include_the_stopped_ones(fake_commands) -> None:
    """Something you installed and turned off is still installed."""
    fake_commands["docker"] = (
        '{"Names":"zigbee2mqtt","Image":"koenkk/zigbee2mqtt:1.35","State":"running"}\n'
        '{"Names":"old-thing","Image":"debian:12","State":"exited"}\n'
    )
    found = installed.containers()
    assert [item["id"] for item in found] == ["zigbee2mqtt", "old-thing"]
    assert found[0]["version"] == "1.35"
    assert found[0]["catalog_id"] == "zigbee2mqtt"


def test_base_system_units_are_not_apps(fake_commands) -> None:
    """A box has forty enabled units out of the box. Those are the system, not
    things somebody installed."""
    fake_commands["systemctl"] = (
        "ssh.service                enabled\n"
        "systemd-timesyncd.service  enabled\n"
        "postgresql.service         enabled\n"
    )
    assert [item["id"] for item in installed.services()] == ["postgresql"]


# ------------------------------------------------------------ desktop entries
def desktop_file(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(body, encoding="utf-8")


def test_a_desktop_entry_becomes_an_app(tmp_path: Path, monkeypatch) -> None:
    desktop_file(
        tmp_path,
        "firefox.desktop",
        "[Desktop Entry]\nType=Application\nName=Firefox\nExec=firefox\n",
    )
    monkeypatch.setattr(installed, "DESKTOP_DIRS", (str(tmp_path),))
    found = installed.desktop_entries()
    assert [item["name"] for item in found] == ["Firefox"]


def test_only_the_first_section_names_the_app(tmp_path: Path, monkeypatch) -> None:
    """Actions further down the file have their own Name= keys; reading them all
    makes Firefox show up called "New Window"."""
    desktop_file(
        tmp_path,
        "firefox.desktop",
        "[Desktop Entry]\nType=Application\nName=Firefox\n\n"
        "[Desktop Action new-window]\nName=New Window\n",
    )
    monkeypatch.setattr(installed, "DESKTOP_DIRS", (str(tmp_path),))
    assert installed.desktop_entries()[0]["name"] == "Firefox"


def test_hidden_entries_and_non_applications_are_skipped(tmp_path: Path, monkeypatch) -> None:
    desktop_file(tmp_path, "a.desktop", "[Desktop Entry]\nType=Application\nName=A\nNoDisplay=true\n")
    desktop_file(tmp_path, "b.desktop", "[Desktop Entry]\nType=Link\nName=B\n")
    monkeypatch.setattr(installed, "DESKTOP_DIRS", (str(tmp_path),))
    assert installed.desktop_entries() == []


# -------------------------------------------------------------------- dedupe
def test_the_same_app_from_two_sources_is_one_row() -> None:
    items = [
        installed.entry("desktop", "firefox", "Firefox"),
        installed.entry("flatpak", "org.mozilla.firefox", "Firefox", "124.0"),
    ]
    merged = installed.dedupe(items)
    assert len(merged) == 1
    # the flatpak wins: it knows its version, the exported .desktop file does not
    assert merged[0]["source"] == "flatpak"
    assert merged[0]["version"] == "124.0"
    assert merged[0]["also"] == ["desktop"]


def test_two_different_apps_stay_two_rows() -> None:
    items = [
        installed.entry("apt", "vim", "vim"),
        installed.entry("apt", "nano", "nano"),
    ]
    assert len(installed.dedupe(items)) == 2


# --------------------------------------------------------------------- scan
def test_a_source_that_blows_up_does_not_take_the_page_down(monkeypatch) -> None:
    def boom():
        raise RuntimeError("dpkg is wedged")

    monkeypatch.setattr(
        installed,
        "SOURCES",
        (("apt", boom), ("snap", lambda: [installed.entry("snap", "core22", "core22")])),
    )
    result = installed.scan()
    assert "apt" in result["errors"]
    assert [item["id"] for item in result["items"]] == ["core22"]


def test_a_machine_with_none_of_these_tools_answers_empty(monkeypatch) -> None:
    """A Mac has no dpkg. That is a fact about the machine, not an error."""
    monkeypatch.setattr(installed, "_run", lambda argv: "")
    monkeypatch.setattr(installed, "DESKTOP_DIRS", ())
    result = installed.scan()
    assert result["items"] == []
    assert result["errors"] == {}


def test_scan_can_be_narrowed_to_one_source(fake_commands) -> None:
    fake_commands["snap"] = "Name  Version\ncore22  1\n"
    result = installed.scan(sources=["snap"])
    assert set(result["counts"]) == {"snap"}
