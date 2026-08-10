"""From "there is a PS5 on your network" to "here is how you use it".

    "e te ensina como adicionar o app, (deixando tudo o maximo automatizado
    possivel sempre)"

Discovery finds things. Suggestions say a thing is worth doing. This module is
the part in between: the actual steps, in order, with as many of them as
possible carried out by the box instead of by the person reading them.

A recipe has a **match** (which devices it applies to) and **steps**. Every step
is one of four kinds, and the kind is what decides how automatic it can be:

``install``  install a catalog app. One button, no typing.
``config``   write a settings key. One button.
``open``     open a URL on the device -- its own web interface. One click.
``manual``   the residue: things only the person can do, because they involve a
             password, a physical cable, or a menu on someone else's television.

The split matters more than it looks. A wall of instructions where three of the
seven lines are actually buttons feels like work; the same list with those three
already done feels like the machine helping. So ``manual`` is a last resort, and
a step that *could* be automated but is not is a bug in the recipe, not a style
choice.

Recipes live in :file:`project_os/data/recipes.yaml` -- data, like the catalog,
so adding support for a new gadget is editing a text file.

**Steps never run themselves.** :func:`for_device` returns a plan; running it is
a separate, explicit call from the interface. Discovering a device must never be
the reason something got installed.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from project_os.core import catalog

log = logging.getLogger(__name__)

RECIPES_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "recipes.yaml"
)

STEP_INSTALL = "install"
STEP_CONFIG = "config"
STEP_OPEN = "open"
STEP_MANUAL = "manual"
STEP_KINDS = (STEP_INSTALL, STEP_CONFIG, STEP_OPEN, STEP_MANUAL)

#: Steps project-os can carry out on its own.
AUTOMATIC_KINDS = (STEP_INSTALL, STEP_CONFIG, STEP_OPEN)

REQUIRED_FIELDS = ("id", "title", "steps")

_cache = None  # type: Optional[List[Dict[str, Any]]]
_TEMPLATE_RE = re.compile(r"\{([a-z_]+)\}")


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _load() -> List[Dict[str, Any]]:
    """Read the recipe file, dropping anything malformed with a log line.

    Same rule as the catalog: one bad block must not be the reason the box has
    no recipes at all.
    """
    import yaml

    try:
        with open(RECIPES_FILE, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or []
    except (OSError, yaml.YAMLError) as exc:
        log.error("recipes: could not read %s: %s", RECIPES_FILE, exc)
        return []
    if not isinstance(raw, list):
        log.error("recipes: %s must be a list", RECIPES_FILE)
        return []

    out = []  # type: List[Dict[str, Any]]
    seen = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            log.error("recipes: entry #%d is not a mapping", index)
            continue
        missing = [field for field in REQUIRED_FIELDS if not entry.get(field)]
        if missing:
            log.error("recipes: %r is missing %s", entry.get("id"), ", ".join(missing))
            continue
        if entry["id"] in seen:
            log.error("recipes: duplicate id %r", entry["id"])
            continue
        steps = _clean_steps(entry["id"], entry.get("steps") or [])
        if not steps:
            log.error("recipes: %r has no usable steps", entry["id"])
            continue
        seen.add(entry["id"])
        entry = dict(entry)
        entry["steps"] = steps
        entry.setdefault("match", {})
        entry.setdefault("summary", "")
        out.append(entry)
    return out


def _clean_steps(recipe_id: str, steps: Sequence[Any]) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    for step in steps:
        if not isinstance(step, dict) or not step.get("text"):
            log.error("recipes: %r has a step with no text", recipe_id)
            continue
        kind = step.get("kind", STEP_MANUAL)
        if kind not in STEP_KINDS:
            log.error("recipes: %r has a step of unknown kind %r", recipe_id, kind)
            continue
        if kind == STEP_INSTALL and not step.get("app"):
            log.error("recipes: %r has an install step with no app", recipe_id)
            continue
        if kind == STEP_CONFIG and not step.get("key"):
            log.error("recipes: %r has a config step with no key", recipe_id)
            continue
        out.append(dict(step, kind=kind))
    return out


def all_recipes() -> List[Dict[str, Any]]:
    global _cache
    if _cache is None:
        _cache = _load()
    return list(_cache)


def reload() -> List[Dict[str, Any]]:
    global _cache
    _cache = None
    return all_recipes()


def get(recipe_id: str) -> Optional[Dict[str, Any]]:
    for recipe in all_recipes():
        if recipe["id"] == recipe_id:
            return recipe
    return None


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
def matches(recipe: Dict[str, Any], device: Dict[str, Any]) -> bool:
    """Does this recipe apply to this device?

    Every clause present must match; absent clauses are not constraints. An
    empty ``match`` therefore matches everything, which is what a recipe like
    "name this device" wants.
    """
    match = recipe.get("match") or {}

    kinds = match.get("kinds")
    if kinds and device.get("kind") not in kinds:
        return False

    needed = match.get("capabilities")
    if needed and not set(needed).issubset(set(device.get("capabilities") or [])):
        return False

    services = match.get("services")
    if services:
        seen = set(device.get("service_types") or [])
        if not seen & set(services):
            return False

    for key, wanted in (match.get("properties") or {}).items():
        value = str((device.get("properties") or {}).get(key, "")).lower()
        if str(wanted).lower() not in value:
            return False

    #: A substring of the device's name, for the cases where the protocol says
    #: nothing useful -- half the SSDP devices in a house are "Linux/UPnP 1.0".
    name_contains = match.get("name_contains")
    if name_contains:
        name = (device.get("name") or "").lower()
        if not any(str(needle).lower() in name for needle in name_contains):
            return False

    return True


def for_device(device: Dict[str, Any], installed: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Every recipe that applies, most specific first.

    "Most specific" is simply the number of clauses a recipe had to satisfy: a
    recipe that asked for kind *and* a property knows more about this device
    than one that asked for nothing, so it goes first.
    """
    already = set(installed or ())
    found = []
    for recipe in all_recipes():
        if not matches(recipe, device):
            continue
        found.append((_specificity(recipe), recipe))
    found.sort(key=lambda pair: -pair[0])
    return [render(recipe, device, already) for _, recipe in found]


def _specificity(recipe: Dict[str, Any]) -> int:
    match = recipe.get("match") or {}
    score = 0
    for key in ("kinds", "capabilities", "services", "name_contains"):
        if match.get(key):
            score += 1
    score += len(match.get("properties") or {})
    return score


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def _fields(device: Dict[str, Any]) -> Dict[str, str]:
    properties = device.get("properties") or {}
    return {
        "name": str(device.get("name") or ""),
        "address": str(device.get("address") or ""),
        "host": str(device.get("host") or device.get("address") or ""),
        "port": str(device.get("port") or ""),
        "id": str(device.get("id") or ""),
        "kind": str(device.get("kind") or ""),
        "mac": str(properties.get("mac", "")),
        "vendor": str(properties.get("vendor", "")),
    }


def fill(text: str, device: Dict[str, Any]) -> str:
    """Substitute ``{address}`` and friends.

    An unknown placeholder is left exactly as written rather than blanked. A
    step that reads ``open http://{addres}`` is a typo someone can see and fix;
    ``open http://`` is a mystery.
    """
    fields = _fields(device)
    return _TEMPLATE_RE.sub(lambda m: fields.get(m.group(1), m.group(0)), text or "")


def render(
    recipe: Dict[str, Any], device: Dict[str, Any], installed: Optional[Iterable[str]] = None
) -> Dict[str, Any]:
    """A recipe with this device's values filled in and each step's state known.

    ``automatic`` counts what the box can do by itself, and the interface leads
    with it -- "4 of 5 steps are one button" is the sentence that makes someone
    actually start.
    """
    already = set(installed or ())
    steps = []  # type: List[Dict[str, Any]]
    for index, step in enumerate(recipe["steps"]):
        rendered = {
            "index": index,
            "kind": step["kind"],
            "text": fill(step["text"], device),
            "automatic": step["kind"] in AUTOMATIC_KINDS,
            "done": False,
        }
        if step.get("note"):
            rendered["note"] = fill(step["note"], device)
        if step["kind"] == STEP_INSTALL:
            app_id = step["app"]
            rendered["app"] = app_id
            rendered["app_entry"] = _app_summary(app_id)
            rendered["done"] = app_id in already
        elif step["kind"] == STEP_CONFIG:
            rendered["key"] = step["key"]
            rendered["value"] = fill(str(step.get("value", "")), device)
        elif step["kind"] == STEP_OPEN:
            rendered["url"] = fill(step.get("url", "http://{address}"), device)
        elif step.get("command"):
            # Shown to be copied, never run for you: a command in a recipe came
            # from a text file, and text files are edited by people.
            rendered["command"] = fill(step["command"], device)
        steps.append(rendered)

    automatic = sum(1 for step in steps if step["automatic"])
    return {
        "id": recipe["id"],
        "title": fill(recipe["title"], device),
        "summary": fill(recipe.get("summary", ""), device),
        "icon": recipe.get("icon", "bulb"),
        "steps": steps,
        "automatic": automatic,
        "total": len(steps),
        "manual_only": automatic == 0,
        "device_id": device.get("id"),
    }


def _app_summary(app_id: str) -> Optional[Dict[str, Any]]:
    """The catalog entry a step installs, so the card can show its cost.

    A recipe that quietly pulls in 400 MB should say 400 MB on the button.
    """
    for entry in catalog.all_entries():
        if entry.get("id") == app_id:
            return {
                "id": entry["id"],
                "name": entry.get("name", app_id),
                "summary": entry.get("summary", ""),
                "ram_mb": entry.get("ram_mb"),
                "disk_mb": entry.get("disk_mb"),
                "icon": entry.get("icon", "apps"),
            }
    log.warning("recipes: install step points at %r, which is not in the catalog", app_id)
    return None


def apps_for_device(device: Dict[str, Any]) -> List[str]:
    """Catalog ids any recipe for this device would install.

    The store uses it for the reverse question -- "what is this app good for" --
    without having to render every recipe.
    """
    found = []  # type: List[str]
    for recipe in all_recipes():
        if not matches(recipe, device):
            continue
        for step in recipe["steps"]:
            if step["kind"] == STEP_INSTALL and step["app"] not in found:
                found.append(step["app"])
    return found


__all__ = [
    "AUTOMATIC_KINDS",
    "STEP_CONFIG",
    "STEP_INSTALL",
    "STEP_KINDS",
    "STEP_MANUAL",
    "STEP_OPEN",
    "all_recipes",
    "apps_for_device",
    "fill",
    "for_device",
    "get",
    "matches",
    "reload",
    "render",
]
