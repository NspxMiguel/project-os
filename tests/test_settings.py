"""Settings that decide behaviour rather than looks.

Everything here uses the ``home`` fixture: these tests write config, and a test
that writes config without an isolated home writes it into the developer's real
~/.project_os -- which is exactly what happened the first time this file ran.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("home")

# --------------------------------------------------------------------------- timezone


def test_the_timezone_comes_from_the_network_when_nobody_set_one(monkeypatch):
    """"puxar fuso pela net ne..." -- a headless box has nobody to ask."""
    from project_os.config import load_config
    from project_os.core import clock

    monkeypatch.setattr(clock, "lookup", lambda url=None, timeout=None: "America/Sao_Paulo")
    config = load_config()
    # conftest turns the lookup off for the whole suite so no test touches the
    # network; this is the one test that is about the lookup.
    config.set("system.timezone_auto", True)
    assert config.get("system.timezone", "") == ""

    result = clock.ensure(config)
    assert result == {"timezone": "America/Sao_Paulo", "source": "network", "changed": True}
    assert config.get("system.timezone") == "America/Sao_Paulo"


def test_a_timezone_somebody_chose_is_never_overwritten(monkeypatch):
    from project_os.config import load_config
    from project_os.core import clock

    called = []
    monkeypatch.setattr(clock, "lookup", lambda *a, **k: called.append(1) or "Europe/Berlin")
    config = load_config()
    config.set("system.timezone", "America/Sao_Paulo")

    result = clock.ensure(config)
    assert result["source"] == "configured"
    assert result["changed"] is False
    assert not called, "a lookup must not even run once someone has chosen"


def test_a_lookup_that_fails_leaves_the_clock_alone(monkeypatch):
    from project_os.config import load_config
    from project_os.core import clock

    monkeypatch.setattr(clock, "lookup", lambda *a, **k: None)
    config = load_config()
    assert clock.ensure(config)["changed"] is False
    assert config.get("system.timezone", "") == ""


def test_rubbish_from_a_captive_portal_is_not_a_timezone():
    """A hotel wifi answering HTML must not end up in config."""
    from project_os.core import clock

    assert clock._plausible("America/Sao_Paulo")
    assert clock._plausible("UTC")
    assert not clock._plausible("<html><body>Sign in")
    assert not clock._plausible("BR")
    assert not clock._plausible("")


def test_lookup_reads_whatever_field_the_provider_uses(monkeypatch):
    """Swapping the URL for another service should not need a code change."""
    import io

    from project_os.core import clock

    for field in ("timezone", "time_zone", "timeZone", "tz"):
        payload = ('{"status":"success","%s":"America/Sao_Paulo"}' % field).encode()

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(
            "urllib.request.urlopen", lambda *a, **k: Response(payload))
        assert clock.lookup("http://example.invalid/tz") == "America/Sao_Paulo"


# ------------------------------------------------------------- system password


def test_setting_the_system_password_needs_the_helper(monkeypatch):
    """A box without the root helper says so; it does not pretend."""
    from project_os.core import syspass

    monkeypatch.setattr(syspass.os.path, "exists", lambda path: False)
    result = syspass.set_password("uma senha boa")
    assert result["ok"] is False
    assert result["code"] == "unavailable"


def test_the_helper_gets_the_password_on_stdin_never_as_an_argument(monkeypatch, tmp_path):
    """Arguments are readable in ps by every user on the machine."""
    from project_os.core import syspass

    helper = tmp_path / "set-password"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)

    seen = {}

    class Completed(object):
        returncode = 0
        stdout = b""

    def fake_run(argv, input=None, **kwargs):
        seen["argv"] = argv
        seen["input"] = input
        return Completed()

    monkeypatch.setattr(syspass.subprocess, "run", fake_run)
    monkeypatch.setattr(syspass.os, "geteuid", lambda: 0)

    result = syspass.set_password("uma senha boa", helper=str(helper))
    assert result["ok"] is True
    assert seen["argv"] == [str(helper)]
    assert seen["input"] == b"project-os:uma senha boa\n"
    assert not any("senha" in str(part) for part in seen["argv"])


def test_a_short_password_is_refused_before_root_is_involved(tmp_path):
    from project_os.core import syspass

    helper = tmp_path / "set-password"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)

    result = syspass.set_password("curta", helper=str(helper))
    assert result["code"] == "too_short"


def test_a_password_with_a_colon_is_refused(tmp_path):
    """chpasswd reads user:password, so a colon would split it in the wrong place."""
    from project_os.core import syspass

    helper = tmp_path / "set-password"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)

    assert syspass.set_password("senha:com:dois", helper=str(helper))["code"] == "bad_password"
