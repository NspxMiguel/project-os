"""Which timezone this box is in, asked over the network.

    "puxar fuso pela net ne..."

The image boots on UTC because an image cannot know where the card will end up,
and a headless box has nobody to ask. What it does have is an internet
connection, and the address it comes out of is enough to place it -- the same
trick a phone uses before you sign in to anything.

Three rules shape this module:

* **It is a guess, and it says so.** The result carries where it came from, so
  the screen can show "America/Sao_Paulo, from the network" rather than pretend
  somebody chose it.
* **It never overwrites a choice.** A timezone typed by a person, or picked from
  the browser, wins over an IP lookup forever.
* **The endpoint is configuration, not a constant.** This is a public project;
  someone will want a different provider, or none at all, and that must be a
  setting rather than a patch ("nada harcoded, tudo configuravel").

Failure is normal here: no internet on first boot, a provider that moved, a
captive portal answering HTML. Every one of those returns "no idea" and leaves
the clock alone, because a wrong timezone silently applied is worse than an
obvious UTC.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

#: Default provider. Plain HTTP JSON, no key, no account, and it answers the one
#: question asked. Overridable through ``system.timezone_url``.
DEFAULT_LOOKUP_URL = "http://ip-api.com/json/?fields=status,timezone"

TIMEOUT = 8.0

#: Keys different providers use for the same answer, so swapping the URL for
#: another service usually needs no code at all.
_FIELDS = ("timezone", "time_zone", "timeZone", "tz")


def _plausible(name: str) -> bool:
    """A zone name, not an error page or a country code.

    ``zoneinfo`` is the real judge, but it is not always installed with data on
    a minimal system, and a cheap shape check keeps rubbish out of config.
    """
    if not name or len(name) > 64 or " " in name:
        return False
    if name in ("UTC", "GMT"):
        return True
    return "/" in name and name.replace("/", "").replace("_", "").replace("-", "").replace("+", "").isalnum()


def lookup(url: str = DEFAULT_LOOKUP_URL, timeout: float = TIMEOUT) -> Optional[str]:
    """Ask the network where this machine is. ``None`` when it cannot say."""
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    if not url:
        return None
    request = Request(url, headers={"User-Agent": "project-os"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured URL
            raw = response.read(64 * 1024)
    except (URLError, OSError) as exc:
        log.info("timezone lookup failed (%s); leaving the clock alone", exc)
        return None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        log.info("timezone lookup did not answer JSON; leaving the clock alone")
        return None
    if not isinstance(payload, dict):
        return None

    for field in _FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and _plausible(value.strip()):
            return value.strip()
    log.info("timezone lookup answered without a timezone; leaving the clock alone")
    return None


def ensure(config: Any) -> Dict[str, Any]:
    """Fill in ``system.timezone`` from the network when nobody has set it.

    Returns what happened, so a caller can log or show it. Never raises: this
    runs during boot, and a box that will not start because a lookup service is
    down would be a poor trade for a convenience.
    """
    result = {"timezone": "", "source": "", "changed": False}  # type: Dict[str, Any]
    try:
        current = str(config.get("system.timezone", "") or "").strip()
    except Exception:  # pragma: no cover - config is never this broken
        return result
    if current:
        result["timezone"] = current
        result["source"] = "configured"
        return result

    try:
        if not bool(config.get("system.timezone_auto", True)):
            return result
        url = str(config.get("system.timezone_url", DEFAULT_LOOKUP_URL) or DEFAULT_LOOKUP_URL)
        found = lookup(url)
    except Exception as exc:  # pragma: no cover - urllib is wrapped already
        log.info("timezone lookup raised %s; leaving the clock alone", exc)
        return result

    if not found:
        return result

    try:
        config.set("system.timezone", found)
        config.save()
    except Exception as exc:  # pragma: no cover
        log.warning("could not store the timezone %r: %s", found, exc)
        return result

    log.info("timezone set to %s from the network", found)
    result.update({"timezone": found, "source": "network", "changed": True})
    return result


__all__ = ["DEFAULT_LOOKUP_URL", "ensure", "lookup"]
