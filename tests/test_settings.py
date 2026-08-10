"""Settings that decide behaviour rather than looks.

Everything here uses the ``home`` fixture: these tests write config, and a test
that writes config without an isolated home writes it into the developer's real
~/.projectos -- which is exactly what happened the first time this file ran.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("home")

# --------------------------------------------------------------------------- timezone


def test_the_timezone_comes_from_the_network_when_nobody_set_one(monkeypatch):
    """"puxar fuso pela net ne..." -- a headless box has nobody to ask."""
    from projectos.config import load_config
    from projectos.core import clock

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
    from projectos.config import load_config
    from projectos.core import clock

    called = []
    monkeypatch.setattr(clock, "lookup", lambda *a, **k: called.append(1) or "Europe/Berlin")
    config = load_config()
    config.set("system.timezone", "America/Sao_Paulo")

    result = clock.ensure(config)
    assert result["source"] == "configured"
    assert result["changed"] is False
    assert not called, "a lookup must not even run once someone has chosen"


def test_a_lookup_that_fails_leaves_the_clock_alone(monkeypatch):
    from projectos.config import load_config
    from projectos.core import clock

    monkeypatch.setattr(clock, "lookup", lambda *a, **k: None)
    config = load_config()
    assert clock.ensure(config)["changed"] is False
    assert config.get("system.timezone", "") == ""


def test_rubbish_from_a_captive_portal_is_not_a_timezone():
    """A hotel wifi answering HTML must not end up in config."""
    from projectos.core import clock

    assert clock._plausible("America/Sao_Paulo")
    assert clock._plausible("UTC")
    assert not clock._plausible("<html><body>Sign in")
    assert not clock._plausible("BR")
    assert not clock._plausible("")


def test_lookup_reads_whatever_field_the_provider_uses(monkeypatch):
    """Swapping the URL for another service should not need a code change."""
    import io

    from projectos.core import clock

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
