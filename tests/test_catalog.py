"""The catalog is data, so these tests guard the data.

A typo in :file:`projectos/data/catalog.yaml` cannot be caught by the type
checker or by importing anything -- it shows up as a store entry with a blank
icon, or an app the install endpoint cannot act on. That is what this file is
for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from projectos.core import catalog

WEB = Path(__file__).resolve().parent.parent / "web"


def icon_names():
    """Every icon name defined in web/lib/icons.js.

    Parsed from the source rather than duplicated here, so adding an icon to the
    frontend is enough -- there is no second list to keep in step.
    """
    source = (WEB / "lib" / "icons.js").read_text(encoding="utf-8")
    body = source.split("export const ICONS = {", 1)[1]
    return set(re.findall(r"^  '?([a-zA-Z0-9_-]+)'?:", body, re.M))


def test_the_catalog_file_loads() -> None:
    entries = catalog.all_entries()
    assert entries, "the catalog file produced no entries at all"


def test_every_entry_has_what_the_store_renders() -> None:
    for entry in catalog.all_entries():
        for field in ("id", "name", "kind", "category", "summary", "description", "icon"):
            assert entry.get(field), "%s is missing %s" % (entry.get("id"), field)
        assert entry["kind"] in catalog.KINDS
        assert isinstance(entry["ram_mb"], int)
        assert isinstance(entry["disk_mb"], int)


def test_ids_are_unique() -> None:
    ids = [entry["id"] for entry in catalog.all_entries()]
    assert len(ids) == len(set(ids))


def test_no_emoji_anywhere_in_the_catalog() -> None:
    """> "evita emojis por favor" """
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")
    for entry in catalog.all_entries():
        blob = " ".join(str(value) for value in entry.values())
        found = emoji.findall(blob)
        assert not found, "%s contains %r" % (entry["id"], found)


def test_every_icon_exists_in_the_frontend_set() -> None:
    known = icon_names()
    for entry in catalog.all_entries():
        assert entry["icon"] in known, (
            "%s uses icon %r, which web/lib/icons.js does not define"
            % (entry["id"], entry["icon"])
        )


def test_entries_that_do_not_fit_are_still_listed() -> None:
    """A store that hides the big ones teaches you it is incomplete."""

    class TinyBoard(object):
        ram_total_mb = 400
        ram_mb = 512
        model = "test"
        tier = "minimal"

    listed = catalog.entries(TinyBoard())
    assert len(listed) == len(catalog.all_entries())
    too_big = [item for item in listed if not item["fits"]]
    assert too_big, "on a 400 MB board something must be marked as not fitting"
    for item in too_big:
        assert item["fit_reason"], "%s is refused without saying why" % item["id"]


@pytest.mark.parametrize("app_id", ["birdtunes", "jellyfin", "whatsapp-bot", "home-assistant"])
def test_the_apps_he_asked_for_are_in_the_store(app_id: str) -> None:
    assert catalog.get(app_id) is not None


def test_a_clean_install_has_no_apps_enabled() -> None:
    """> "quero q por padrao ele venha so com apps normais po, sem nada, q nem o ha" """
    from projectos import config

    assert config.DEFAULTS["apps"]["enabled"] == []
