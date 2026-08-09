"""What is already on this machine, whoever put it there.

    "adiciona tbm apps, detectados no proprio sistema, sla, o cara foi la, foi
    pro modo advanced, instalo um firefox, um flatpack store, e dps voltau pro
    modo simple. ai aparece em apps."

That is the whole idea: ProjectOS did not install it and does not own it, but it
is on the box, so it belongs in the apps list. A system that only sees what it
installed itself is lying about the machine it runs on.

Six places get read, each with the same shape (``source``, ``id``, ``name``,
``version``, ``kind``):

* **apt/dpkg** -- what a Debian box mostly is. Only *manually installed*
  packages: the full list is four thousand libraries and nobody wants that list.
* **flatpak** and **snap** -- both mentioned by name in the request.
* **desktop entries** -- ``.desktop`` files, which is how a graphical app says
  "I am an app" regardless of how it arrived.
* **systemd units** -- enabled services that are not part of the base system.
  A Postgres someone installed by hand is an app in every sense that matters.
* **containers** -- Docker/Podman, which is where the heavy third-party things
  live.

Everything is read-only and everything degrades to an empty list. A machine with
no dpkg (a Mac, say) reports what it does have and says nothing about the rest.

Where a find matches an entry in ``data/catalog.yaml``, it is linked by
``catalog_id``: the store then shows "already installed" instead of offering to
install a second copy over the top of it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Set

from projectos.core import catalog

log = logging.getLogger(__name__)

COMMAND_TIMEOUT = 10.0

DESKTOP_DIRS = (
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser("~/.local/share/applications"),
)

#: Units that ship with any Debian and say nothing about what the box is *for*.
#: Prefix match, so ``systemd-*`` covers a few dozen at once.
BASE_UNIT_PREFIXES = (
    "systemd-",
    "dbus",
    "getty",
    "user@",
    "rc-local",
    "console-setup",
    "keyboard-setup",
    "networking",
    "ssh",
    "cron",
    "rsyslog",
    "udisks2",
    "polkit",
    "apparmor",
    "e2scrub",
    "man-db",
    "apt-daily",
    "fstrim",
    "logrotate",
    "wpa_supplicant",
    "dhcpcd",
    "raspi-config",
    "rpi-",
    "avahi",
    "bluetooth",
    "triggerhappy",
    "hciuart",
    "nfs-",
    "rpcbind",
)

#: Packages so numerous and so uninteresting that listing them buries the rest.
BORING_PACKAGE_PREFIXES = ("lib", "python3-", "perl-", "fonts-", "gir1.2-", "x11-", "linux-")

#: What a find is called in the interface. Not the same axis as catalog ``kind``.
KIND_PACKAGE = "package"
KIND_APP = "app"
KIND_SERVICE = "service"
KIND_CONTAINER = "container"

_catalog_index = None  # type: Optional[Dict[str, str]]


# ---------------------------------------------------------------------------
def _run(argv: List[str]) -> str:
    """Run a read-only command, or return "" for any reason at all.

    Callers treat "the tool is missing", "the tool failed" and "the tool printed
    nothing" identically, because for a listing they are identical: there is
    nothing from that source.
    """
    if not shutil.which(argv[0]):
        return ""
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", argv[0], exc)
        return ""
    return result.stdout.decode("utf-8", "replace")


def entry(
    source: str,
    identifier: str,
    name: str,
    version: str = "",
    kind: str = KIND_APP,
    **extra: Any
) -> Dict[str, Any]:
    found = {
        "source": source,
        "id": identifier,
        "name": name or identifier,
        "version": version,
        "kind": kind,
        "catalog_id": match_catalog(identifier, name),
    }
    found.update(extra)
    return found


def _index() -> Dict[str, str]:
    """``name or id -> catalog id``, lower-cased, built once.

    Deliberately shallow: exact matches only. Fuzzy matching here would link
    ``docker-compose`` to ``docker`` and then offer to "update" one by installing
    the other, and being wrong about that is worse than not linking at all.
    """
    global _catalog_index
    if _catalog_index is not None:
        return _catalog_index
    index = {}  # type: Dict[str, str]
    for item in catalog.all_entries():
        catalog_id = item.get("id", "")
        if not catalog_id:
            continue
        index[catalog_id.lower()] = catalog_id
        name = (item.get("name") or "").lower()
        if name:
            index.setdefault(name, catalog_id)
        for alias in item.get("aliases", ()) or ():
            index.setdefault(str(alias).lower(), catalog_id)
    _catalog_index = index
    return index


def match_catalog(identifier: str, name: str = "") -> Optional[str]:
    index = _index()
    for candidate in (identifier, name):
        text = (candidate or "").strip().lower()
        if not text:
            continue
        if text in index:
            return index[text]
        # com.spotify.Client -> spotify ; jellyfin.service -> jellyfin
        tail = text.rsplit(".", 1)[-1]
        if tail in index:
            return index[tail]
    return None


def reset_cache() -> None:
    global _catalog_index
    _catalog_index = None


# ------------------------------------------------------------------ sources
def apt_packages(include_boring: bool = False) -> List[Dict[str, Any]]:
    """Manually installed packages, which is roughly "what someone chose".

    ``apt-mark showmanual`` is the honest answer to "what did a person install
    here" -- everything else on a Debian box arrived as a dependency.
    """
    text = _run(["apt-mark", "showmanual"])
    if not text:
        return []
    names = [line.strip() for line in text.splitlines() if line.strip()]
    if not include_boring:
        names = [
            name
            for name in names
            if not any(name.startswith(prefix) for prefix in BORING_PACKAGE_PREFIXES)
        ]
    versions = _dpkg_versions(names)
    return [
        entry("apt", name, name, versions.get(name, ""), KIND_PACKAGE)
        for name in sorted(names)
    ]


def _dpkg_versions(names: Iterable[str]) -> Dict[str, str]:
    """One dpkg-query call for every version, rather than one call per package."""
    wanted = list(names)
    if not wanted:
        return {}
    text = _run(["dpkg-query", "-W", "-f=${Package}\\t${Version}\\n"] + wanted)
    versions = {}  # type: Dict[str, str]
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            versions[parts[0].strip()] = parts[1].strip()
    return versions


def flatpaks() -> List[Dict[str, Any]]:
    text = _run(["flatpak", "list", "--app", "--columns=application,name,version"])
    found = []
    for line in text.splitlines():
        parts = [piece.strip() for piece in line.split("\t")]
        if not parts or not parts[0]:
            continue
        app_id = parts[0]
        name = parts[1] if len(parts) > 1 and parts[1] else app_id.rsplit(".", 1)[-1]
        version = parts[2] if len(parts) > 2 else ""
        found.append(entry("flatpak", app_id, name, version, KIND_APP))
    return found


def snaps() -> List[Dict[str, Any]]:
    text = _run(["snap", "list"])
    found = []
    for line in text.splitlines()[1:]:  # first line is the header
        parts = line.split()
        if len(parts) >= 2:
            found.append(entry("snap", parts[0], parts[0], parts[1], KIND_APP))
    return found


def _parse_desktop(path: str) -> Optional[Dict[str, Any]]:
    """Read the ``[Desktop Entry]`` section, and only that section.

    Actions further down the file have their own ``Name=`` keys; reading the
    whole file with a naive loop makes Firefox show up called "New Window".
    """
    name = ""
    no_display = False
    in_section = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("["):
                    if in_section:
                        break
                    in_section = line == "[Desktop Entry]"
                    continue
                if not in_section or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key == "Name" and not name:
                    name = value.strip()
                elif key in ("NoDisplay", "Hidden") and value.strip().lower() == "true":
                    no_display = True
                elif key == "Type" and value.strip() != "Application":
                    return None
    except OSError:
        return None
    if no_display or not name:
        return None
    app_id = os.path.basename(path)[: -len(".desktop")]
    return entry("desktop", app_id, name, "", KIND_APP, path=path)


def desktop_entries() -> List[Dict[str, Any]]:
    """Graphical apps, deduplicated by file name.

    A flatpak exports a ``.desktop`` file too, so the same app can appear twice;
    the flatpak listing is the better record, and the caller drops the duplicate.
    """
    found = {}  # type: Dict[str, Dict[str, Any]]
    for directory in DESKTOP_DIRS:
        if not os.path.isdir(directory):
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop") or name in found:
                continue
            parsed = _parse_desktop(os.path.join(directory, name))
            if parsed:
                found[name] = parsed
    return list(found.values())


def _is_base_unit(unit: str) -> bool:
    return any(unit.startswith(prefix) for prefix in BASE_UNIT_PREFIXES)


def services(include_base: bool = False) -> List[Dict[str, Any]]:
    """Enabled systemd services that are not part of the base system."""
    text = _run(["systemctl", "list-unit-files", "--type=service", "--state=enabled", "--no-legend"])
    found = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = parts[0]
        if not unit.endswith(".service"):
            continue
        stem = unit[: -len(".service")]
        if not include_base and _is_base_unit(stem):
            continue
        found.append(entry("systemd", stem, stem, "", KIND_SERVICE, unit=unit))
    return found


def containers() -> List[Dict[str, Any]]:
    """Docker or Podman containers, running or not.

    Stopped ones are included on purpose: something you installed last month and
    turned off is still installed, and hiding it makes the box look emptier than
    it is.
    """
    for command in ("docker", "podman"):
        text = _run([command, "ps", "-a", "--format", "{{json .}}"])
        if not text.strip():
            continue
        found = []
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            name = record.get("Names") or record.get("Name") or ""
            if isinstance(name, list):
                name = name[0] if name else ""
            image = record.get("Image", "")
            found.append(
                entry(
                    command,
                    str(name),
                    str(name) or str(image),
                    _image_tag(str(image)),
                    KIND_CONTAINER,
                    image=image,
                    state=record.get("State", record.get("Status", "")),
                )
            )
        if found:
            return found
    return []


def _image_tag(image: str) -> str:
    if ":" not in image:
        return ""
    tag = image.rsplit(":", 1)[-1]
    return "" if "/" in tag else tag


# ---------------------------------------------------------------------------
SOURCES = (
    ("apt", apt_packages),
    ("flatpak", flatpaks),
    ("snap", snaps),
    ("desktop", desktop_entries),
    ("systemd", services),
    ("containers", containers),
)


def scan(sources: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Everything installed here, with the duplicates collapsed.

    A source that blows up is reported in ``errors`` and skipped. Half a list is
    worth more than a stack trace, and the interface can say which half.
    """
    wanted = set(sources) if sources else None
    items = []  # type: List[Dict[str, Any]]
    errors = {}  # type: Dict[str, str]
    counts = {}  # type: Dict[str, int]
    for name, reader in SOURCES:
        if wanted is not None and name not in wanted:
            continue
        try:
            found = reader()
        except Exception as exc:  # a listing must never take the page down
            log.warning("the %s reader failed", name, exc_info=True)
            errors[name] = str(exc)
            continue
        counts[name] = len(found)
        items.extend(found)
    items = dedupe(items)
    return {
        "items": items,
        "counts": counts,
        "errors": errors,
        "catalog_matches": sorted({i["catalog_id"] for i in items if i.get("catalog_id")}),
    }


#: Which source wins when two of them describe the same thing. A flatpak knows
#: its own version; the ``.desktop`` file it exported knows nothing.
SOURCE_RANK = {"flatpak": 0, "snap": 1, "docker": 2, "podman": 2, "apt": 3, "systemd": 4, "desktop": 5}


def dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per thing, keyed by catalog id where we have one, else by name."""
    best = {}  # type: Dict[str, Dict[str, Any]]
    for item in items:
        key = item.get("catalog_id") or _normalise(item.get("name", "")) or item.get("id", "")
        current = best.get(key)
        if current is None or SOURCE_RANK.get(item["source"], 9) < SOURCE_RANK.get(
            current["source"], 9
        ):
            if current is not None:
                # Keep the fact that it was found more than once: "flatpak and
                # apt both have Firefox" is a real thing worth showing.
                item = dict(item)
                item["also"] = sorted(set(current.get("also", [])) | {current["source"]})
            best[key] = item
        else:
            current["also"] = sorted(set(current.get("also", [])) | {item["source"]})
    return sorted(best.values(), key=lambda i: (i["kind"], i["name"].lower()))


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def installed_catalog_ids() -> Set[str]:
    """Catalog entries already present on this machine, however they arrived.

    The store calls this so it can say "already installed" instead of offering
    a second copy of something that is right there.
    """
    return {item for item in scan()["catalog_matches"]}


__all__ = [
    "KIND_APP",
    "KIND_CONTAINER",
    "KIND_PACKAGE",
    "KIND_SERVICE",
    "SOURCES",
    "apt_packages",
    "containers",
    "dedupe",
    "desktop_entries",
    "flatpaks",
    "installed_catalog_ids",
    "match_catalog",
    "reset_cache",
    "scan",
    "services",
    "snaps",
]
