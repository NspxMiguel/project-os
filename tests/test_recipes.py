"""The recipe engine, and the recipe file itself.

Half of these test the matcher; the other half read
:file:`projectos/data/recipes.yaml` and check the things a typo there would
break -- an install step pointing at an app that is not in the store, an icon
the interface cannot draw, a placeholder nobody substitutes.
"""

from __future__ import annotations

import re

import pytest

from projectos.core import catalog, recipes

APPLE_TV = {
    "id": "dev-appletv",
    "name": "Sala",
    "kind": "apple_tv",
    "address": "192.168.0.10",
    "port": 7000,
    "capabilities": ["audio_out", "video_out", "airplay_audio"],
    "properties": {},
}

UNKNOWN = {
    "id": "dev-x",
    "name": "192.168.0.44",
    "kind": "unknown",
    "address": "192.168.0.44",
    "port": 0,
    "capabilities": [],
    "properties": {"mac": "a4:83:e7:11:22:33", "vendor": ""},
}


def recipe(**kwargs):
    base = {"id": "t", "title": "T", "match": {}, "steps": [{"kind": "manual", "text": "x"}]}
    base.update(kwargs)
    return base


# ------------------------------------------------------------------ matching
def test_an_empty_match_applies_to_everything() -> None:
    assert recipes.matches(recipe(), APPLE_TV) is True


def test_kind_has_to_be_one_of_the_listed_ones() -> None:
    assert recipes.matches(recipe(match={"kinds": ["apple_tv"]}), APPLE_TV)
    assert not recipes.matches(recipe(match={"kinds": ["printer"]}), APPLE_TV)


def test_capabilities_are_required_all_of_them() -> None:
    """A recipe that needs casting *and* audio should not fire on a speaker that
    only does audio."""
    assert recipes.matches(recipe(match={"capabilities": ["airplay_audio"]}), APPLE_TV)
    assert not recipes.matches(
        recipe(match={"capabilities": ["airplay_audio", "cast_media"]}), APPLE_TV
    )


def test_a_property_clause_is_a_substring_and_ignores_case() -> None:
    device = dict(APPLE_TV, properties={"vendor": "TP-Link Corporation"})
    assert recipes.matches(recipe(match={"properties": {"vendor": "tp-link"}}), device)
    assert not recipes.matches(recipe(match={"properties": {"vendor": "sonos"}}), device)


def test_the_more_specific_recipe_comes_first() -> None:
    found = recipes.for_device(APPLE_TV)
    assert found, "an Apple TV should match something"
    scores = [len(r["steps"]) for r in found]  # smoke: rendering did not blow up
    assert all(scores)
    # the one that asked for a capability outranks the generic media one
    assert found[0]["id"] == "birdtunes-airplay"


# ----------------------------------------------------------------- rendering
def test_placeholders_are_filled_from_the_device() -> None:
    assert recipes.fill("http://{address}:{port}", APPLE_TV) == "http://192.168.0.10:7000"
    assert recipes.fill("em {name}", APPLE_TV) == "em Sala"


def test_an_unknown_placeholder_is_left_visible() -> None:
    """A typo you can see beats a blank you cannot."""
    assert recipes.fill("http://{addres}", APPLE_TV) == "http://{addres}"


def test_a_rendered_recipe_counts_what_the_box_can_do_itself() -> None:
    rendered = recipes.render(
        recipe(
            steps=[
                {"kind": "install", "app": "birdtunes", "text": "a"},
                {"kind": "config", "key": "x.y", "value": "{address}", "text": "b"},
                {"kind": "manual", "text": "c"},
            ]
        ),
        APPLE_TV,
    )
    assert rendered["automatic"] == 2
    assert rendered["total"] == 3
    assert rendered["manual_only"] is False
    assert rendered["steps"][1]["value"] == "192.168.0.10"


def test_an_install_step_carries_what_the_app_costs() -> None:
    """A recipe that quietly pulls in 400 MB should say 400 MB on the button."""
    rendered = recipes.render(
        recipe(steps=[{"kind": "install", "app": "zigbee2mqtt", "text": "a"}]), APPLE_TV
    )
    entry = rendered["steps"][0]["app_entry"]
    assert entry["name"] == "Zigbee2MQTT"
    assert entry["ram_mb"]


def test_an_app_already_installed_shows_as_done() -> None:
    rendered = recipes.render(
        recipe(steps=[{"kind": "install", "app": "birdtunes", "text": "a"}]),
        APPLE_TV,
        installed=["birdtunes"],
    )
    assert rendered["steps"][0]["done"] is True


def test_a_recipe_returns_a_plan_and_nothing_is_executed() -> None:
    """Finding a device must never be the reason something got installed."""
    for rendered in recipes.for_device(APPLE_TV):
        for step in rendered["steps"]:
            assert set(step) & {"kind", "text"}
            assert "result" not in step  # nothing here has run


# -------------------------------------------------------------- loading rules
def test_a_step_with_no_text_is_dropped(caplog) -> None:
    assert recipes._clean_steps("t", [{"kind": "manual"}]) == []


def test_an_install_step_with_no_app_is_dropped() -> None:
    assert recipes._clean_steps("t", [{"kind": "install", "text": "a"}]) == []


def test_a_step_of_an_invented_kind_is_dropped() -> None:
    assert recipes._clean_steps("t", [{"kind": "teleport", "text": "a"}]) == []


# ------------------------------------------------------ the file that ships
def test_the_shipped_file_loads() -> None:
    assert len(recipes.all_recipes()) >= 15


def test_every_install_step_points_at_a_real_catalog_app() -> None:
    known = {entry["id"] for entry in catalog.all_entries()}
    for item in recipes.all_recipes():
        for step in item["steps"]:
            if step["kind"] == "install":
                assert step["app"] in known, "%s installs %r" % (item["id"], step["app"])


def test_every_recipe_icon_can_be_drawn() -> None:
    body = open("web/lib/icons.js", encoding="utf-8").read()
    names = set(re.findall(r"^  '?([a-zA-Z0-9_-]+)'?:", body, re.M))
    for item in recipes.all_recipes():
        assert item.get("icon", "bulb") in names, item["id"]


def test_every_placeholder_used_is_one_we_substitute() -> None:
    """``{addres}`` in the shipped file would ship as visible junk."""
    known = set(recipes._fields(UNKNOWN))
    for item in recipes.all_recipes():
        blob = " ".join(
            [item["title"], item.get("summary", "")]
            + [str(value) for step in item["steps"] for value in step.values()]
        )
        for placeholder in re.findall(r"\{([a-z_]+)\}", blob):
            assert placeholder in known, "%s uses {%s}" % (item["id"], placeholder)


def test_no_emoji_in_the_recipes() -> None:
    for item in recipes.all_recipes():
        blob = repr(item)
        assert not any(ord(char) > 0x2100 for char in blob), item["id"]


def test_every_device_kind_we_can_detect_has_at_least_one_recipe() -> None:
    """The promise was "te ensina como adicionar o app". A kind with no recipe
    is a device the dashboard lists and then says nothing about."""
    from projectos.core import discovery

    without = []
    for kind in discovery.KIND_CAPABILITIES:
        device = dict(UNKNOWN, kind=kind, capabilities=list(discovery.KIND_CAPABILITIES[kind]))
        if not recipes.for_device(device):
            without.append(kind)
    assert without == [], "sem receita: %s" % ", ".join(without)
