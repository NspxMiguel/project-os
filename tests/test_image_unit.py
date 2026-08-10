"""The systemd unit the image ships, read as the contract it is.

Nothing in Python can catch what this file gets wrong: the code was correct, the
sudoers was correct, and Advanced mode still could not install a package on the
real box because two hardening lines in the unit made `sudo` impossible. It cost
a shell on the machine to find. These assertions are cheap by comparison.
"""

from __future__ import annotations

import os

import pytest

IMAGE_FILES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "image",
    "stage-project-os",
    "00-project-os",
    "files",
)
UNIT = os.path.join(IMAGE_FILES, "etc", "systemd", "system", "project-os.service")


@pytest.fixture(scope="module")
def unit():
    with open(UNIT, "r", encoding="utf-8") as handle:
        return handle.read()


def directives(text):
    """Every KEY=VALUE line, comments dropped -- comments here discuss the flags."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            out.setdefault(key.strip(), []).append(value.strip())
    return out


def test_no_new_privileges_is_not_set(unit):
    """It blocks setuid, which means it blocks sudo, which means it blocks apt.

    sudo's answer on the shipped image: "The 'no new privileges' flag is set,
    which prevents sudo from running as root."
    """
    assert "NoNewPrivileges" not in directives(unit)


def test_the_capability_bounding_set_is_not_narrowed(unit):
    """apt as root still needs CAP_CHOWN and CAP_DAC_OVERRIDE to unpack."""
    assert "CapabilityBoundingSet" not in directives(unit)


def test_port_80_is_still_reachable_without_root(unit):
    """The one capability that is granted, and the reason the box has no :port."""
    assert directives(unit).get("AmbientCapabilities") == ["CAP_NET_BIND_SERVICE"]
    assert "--port 80" in unit


def test_it_runs_as_the_service_user_not_root(unit):
    found = directives(unit)
    assert found.get("User") == ["project-os"]
    assert found.get("Group") == ["project-os"]


def test_the_unit_is_named_the_same_way_the_code_restarts_it():
    """updates.restart() and the file on disk have to agree, hyphen and all."""
    from project_os.core import updates

    assert updates.UNIT_NAME == "project-os.service"
    assert os.path.basename(UNIT) == updates.UNIT_NAME


def test_the_services_screen_can_see_project_os_itself():
    """The managed-unit prefixes kept the pre-rename underscore and matched none."""
    from project_os.api import system

    assert any(
        "project-os.service"[: -len(".service")].startswith(prefix)
        for prefix in system.MANAGED_UNIT_PREFIXES
    )
    assert "project_os" not in system.MANAGED_UNIT_PREFIXES
