

def test_a_fresh_machine_runs_nothing_until_you_install_something() -> None:
    """"ele seria um sistema base, sem nada por padrao".

    An empty apps.enabled used to read as "not configured", and not-configured
    meant "run every bundled app" -- so a new install came up with a WhatsApp bot
    nobody asked for. Empty means empty.
    """
    from projectos.core.plugins import PluginManager

    class Config(object):
        def __init__(self, value):
            self.value = value

        def get(self, path, default=None):
            return self.value if path == "apps.enabled" else default

    class Fake(object):
        """Just enough of a PluginManager for the two methods under test."""

        def __init__(self, value):
            self.config = Config(value)

        enabled_ids = PluginManager.enabled_ids
        is_enabled = PluginManager.is_enabled

    assert Fake([]).enabled_ids() == []
    assert not Fake([]).is_enabled("whatsapp-bot")
    assert Fake(["birdtunes"]).is_enabled("birdtunes")
    # A config file written before the key existed keeps the old meaning, so an
    # upgrade does not silently turn someone's apps off.
    assert Fake(None).is_enabled("birdtunes")
