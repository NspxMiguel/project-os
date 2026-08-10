"""Finding what is on the network, and turning it into devices.

The user asked for this directly -- *"legal tbm autodetectar dispositivos e dar
sujestoes doq da pra fazer"* -- so the bar is not "list some IPs". It is: plug
project-os in, and it already knows there is an Apple TV in the living room and a
Chromecast in the bedroom, with enough detail that BirdTunes can offer them as
outputs without the user typing an address.

Sources
=======

============  ==============================================  ==============
source        how                                             needs
============  ==============================================  ==============
mDNS          browse every service type in SERVICE_KINDS at    zeroconf
              once with AsyncZeroconf/AsyncServiceBrowser
probe         TCP connect to well-known ports on the hosts     nothing
              listed in ``discovery.probe_hosts`` (+ unicast   (zeroconf for
              mDNS to the same host when zeroconf is there)    the unicast leg)
kasa          UDP broadcast on 9999, XOR-obfuscated JSON       nothing
tuya          listen for the UDP beacons they shout on         nothing
              6666/6667
serial        read /sys/class/tty, match USB vendor ids        nothing
============  ==============================================  ==============

Only the first needs a third-party package, and its absence is *reported*
(:meth:`DeviceRegistry.status`) rather than silently producing an empty screen.

Kinds
=====

Inferred from the service type plus TXT hints. ``am=`` on ``_airplay``/``_raop``
carries the Apple model (``AppleTV14,1``, ``AudioAccessory5,1`` = HomePod mini),
``md=`` carries the Cast model, and ``fn=`` the friendly name::

    _airplay/_raop  am=AppleTV*         -> apple_tv
                    am=AudioAccessory*  -> homepod
                    am=Macmini*/MacBook*/iMac*/Mac* -> computer
                    anything else       -> airplay_speaker
    _googlecast     md contains "group" -> cast_group
                    md is a known speaker, or the ``ca`` bitmask says
                    "audio out, no video out"                -> cast_audio
                    anything else       -> chromecast

Capabilities
============

Two layers, unioned. Per kind:

===============  ==========================================================
kind             capabilities
===============  ==========================================================
apple_tv         audio_out, video_out, airplay_audio, remote_control
homepod          audio_out, airplay_audio, remote_control
airplay_speaker  audio_out, airplay_audio
chromecast       audio_out, video_out, cast_media, remote_control
cast_audio       audio_out, cast_media, remote_control
cast_group       audio_out, cast_media, remote_control
speaker          audio_out
tv               audio_out, video_out
home_assistant   automation_hub, web_ui
homekit          automation_hub
mqtt_broker      automation_hub
esphome          automation_hub, web_ui
computer         shell
printer          (none)
web_service      web_ui
smart_plug       switch                    (extension, docs/HOME.md)
smart_light      light                     (extension, docs/HOME.md)
tuya_device      (none -- needs a local key it does not advertise)
microcontroller  serial_port, flashable    (extension, docs/SIMPLE-PROJECT-OS.md)
===============  ==========================================================

Per service actually answered: ``_ssh``/``_sftp-ssh`` adds ``shell``,
``_http``/``_https`` adds ``web_ui``, ``_raop``/``_airplay`` add
``airplay_audio`` + ``audio_out``, ``_googlecast`` adds ``cast_media`` +
``audio_out``, ``_companion-link``/``_touch-able`` add ``remote_control``.
So a Mac that answers ``_airplay`` *and* ``_ssh`` ends up with both
``airplay_audio`` and ``shell``, which is exactly what it can do.

Identity and merging
====================

One physical box answers on several service types: an Apple TV replies to
``_airplay``, ``_raop``, ``_companion-link`` and ``_touch-able``. Those must
become **one** device, and its id must be the same on every scan or the database
grows a new row each time.

Each resolved service becomes an :class:`Observation` carrying two sets of keys:

* **strong keys** -- something the device carries with it: ``mac:<12 hex>`` from
  the AirPlay ``deviceid`` TXT record or from the ``<MAC>@<name>`` form of a
  ``_raop`` instance name, ``uuid:<32 hex>`` from the Cast ``id``, ``id:<...>``
  from ``uuid``/``serialNumber``, ``id:<sha1>`` from the AirPlay ``pk``.
* **weak keys** -- ``host:<mDNS target host>`` and ``addr:<ip>``, which say
  "same box" but cannot tell two logical devices on one box apart.

Merging is a union-find in two passes:

1. union every observation that shares a strong key;
2. for each weak key, union the clusters that have *no* strong key of their own
   into the single strong cluster on that host. When a host carries **two**
   identified devices (the classic case: a Cast group is advertised from the
   same address as the speaker leading it, with a different ``id``), the
   anonymous observations are left with each other instead of being guessed
   into one of them.

The surviving cluster picks the strongest kind (:data:`KIND_PRIORITY`), unions
the capabilities, and derives the id from the best strong key it has -- falling
back to ``<kind>:<host-or-ip>:<port>`` only when the device advertises nothing
stable. The IP is never the key on its own.

Everything network-facing is async or runs in an executor: an mDNS browse takes
seconds and a UDP listen blocks, and freezing the web interface of the box doing
the scanning would be self-defeating.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from project_os.db import utcnow_iso

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# tunables
# ---------------------------------------------------------------------------
DEFAULT_SCAN_TIMEOUT = 6.0
DEFAULT_INTERVAL = 300.0
MIN_INTERVAL = 30.0
LOST_AFTER_INTERVALS = 3
RESOLVE_TIMEOUT_MS = 3000.0
RESOLVE_GRACE_SECONDS = 3.0
PROBE_CONNECT_TIMEOUT = 1.0
MDNS_PORT = 5353
MAX_PROPERTY_LENGTH = 512
MAX_PROPERTIES = 48

ZEROCONF_HINT = (
    "mDNS discovery needs the optional 'zeroconf' package: "
    "pip install zeroconf (or apt install python3-zeroconf)"
)

# ---------------------------------------------------------------------------
# capability vocabulary (ARCHITECTURE.md section 9)
# ---------------------------------------------------------------------------
AUDIO_OUT = "audio_out"
VIDEO_OUT = "video_out"
CAST_MEDIA = "cast_media"
AIRPLAY_AUDIO = "airplay_audio"
REMOTE_CONTROL = "remote_control"
AUTOMATION_HUB = "automation_hub"
SHELL = "shell"
WEB_UI = "web_ui"

#: The eight normative capabilities, in display order.
CAPABILITIES = (
    AUDIO_OUT,
    VIDEO_OUT,
    CAST_MEDIA,
    AIRPLAY_AUDIO,
    REMOTE_CONTROL,
    AUTOMATION_HUB,
    SHELL,
    WEB_UI,
)

#: Extensions used by the smart-home and ESP32 phases (docs/HOME.md,
#: docs/SIMPLE-PROJECT-OS.md). They never replace a normative capability.
SWITCH = "switch"
LIGHT = "light"
ENERGY_MONITOR = "energy_monitor"
SERIAL_PORT = "serial_port"
FLASHABLE = "flashable"

EXTRA_CAPABILITIES = (SWITCH, LIGHT, ENERGY_MONITOR, SERIAL_PORT, FLASHABLE)
CAPABILITY_ORDER = CAPABILITIES + EXTRA_CAPABILITIES

# ---------------------------------------------------------------------------
# services -> kind / capabilities
# ---------------------------------------------------------------------------
#: Every service type we browse, and the kind it means on its own. Ordering is
#: the browse order; it has no other significance.
SERVICE_KINDS = {
    "_airplay._tcp.local.": "airplay_speaker",
    "_raop._tcp.local.": "airplay_speaker",
    "_companion-link._tcp.local.": "apple_tv",
    "_touch-able._tcp.local.": "apple_tv",
    "_googlecast._tcp.local.": "chromecast",
    "_hap._tcp.local.": "homekit",
    "_home-assistant._tcp.local.": "home_assistant",
    "_hass._tcp.local.": "home_assistant",
    "_esphome._tcp.local.": "esphome",
    "_mqtt._tcp.local.": "mqtt_broker",
    "_spotify-connect._tcp.local.": "speaker",
    "_printer._tcp.local.": "printer",
    "_ipp._tcp.local.": "printer",
    "_ipps._tcp.local.": "printer",
    "_ssh._tcp.local.": "computer",
    "_sftp-ssh._tcp.local.": "computer",
    "_workstation._tcp.local.": "computer",
    "_http._tcp.local.": "web_service",
    "_https._tcp.local.": "web_service",
    # "ate impressora 3d" -- OctoPrint and Moonraker both advertise themselves,
    # so a printer in the garage shows up as a printer and not as "some web page".
    "_octoprint._tcp.local.": "printer_3d",
    "_moonraker._tcp.local.": "printer_3d",
    "_klipper._tcp.local.": "printer_3d",
    # File servers. A NAS is worth telling apart from a laptop: what you do with
    # it (mount it, back up to it) is completely different.
    "_smb._tcp.local.": "nas",
    "_afpovertcp._tcp.local.": "nas",
    "_nfs._tcp.local.": "nas",
    # Media servers, for the "hospedar filmes" half of the store.
    "_jellyfin._tcp.local.": "media_server",
    "_plexmediasvr._tcp.local.": "media_server",
    "_emby._tcp.local.": "media_server",
    # Phones. Android exposes these two when developer options or Google Cast
    # sharing is on; it is the only mDNS trace a phone usually leaves.
    "_adb-tls-connect._tcp.local.": "phone",
    "_androidtvremote2._tcp.local.": "tv",
}

SERVICE_TYPES = tuple(SERVICE_KINDS.keys())

#: What answering a given service *proves*, regardless of the final kind.
SERVICE_CAPABILITIES = {
    "_airplay._tcp.local.": (AIRPLAY_AUDIO, AUDIO_OUT),
    "_raop._tcp.local.": (AIRPLAY_AUDIO, AUDIO_OUT),
    "_companion-link._tcp.local.": (REMOTE_CONTROL,),
    "_touch-able._tcp.local.": (REMOTE_CONTROL,),
    "_googlecast._tcp.local.": (CAST_MEDIA, AUDIO_OUT),
    "_hap._tcp.local.": (AUTOMATION_HUB,),
    "_home-assistant._tcp.local.": (AUTOMATION_HUB, WEB_UI),
    "_hass._tcp.local.": (AUTOMATION_HUB, WEB_UI),
    "_esphome._tcp.local.": (AUTOMATION_HUB,),
    "_mqtt._tcp.local.": (AUTOMATION_HUB,),
    "_spotify-connect._tcp.local.": (AUDIO_OUT,),
    "_octoprint._tcp.local.": (WEB_UI,),
    "_moonraker._tcp.local.": (WEB_UI,),
    "_klipper._tcp.local.": (WEB_UI,),
    "_jellyfin._tcp.local.": (AUDIO_OUT, VIDEO_OUT, WEB_UI),
    "_plexmediasvr._tcp.local.": (AUDIO_OUT, VIDEO_OUT, WEB_UI),
    "_emby._tcp.local.": (AUDIO_OUT, VIDEO_OUT, WEB_UI),
    "_androidtvremote2._tcp.local.": (AUDIO_OUT, VIDEO_OUT, REMOTE_CONTROL),
    "_ssh._tcp.local.": (SHELL,),
    "_sftp-ssh._tcp.local.": (SHELL,),
    "_http._tcp.local.": (WEB_UI,),
    "_https._tcp.local.": (WEB_UI,),
}

#: What a device of a given kind can do even before we look at its services.
KIND_CAPABILITIES = {
    "apple_tv": (AUDIO_OUT, VIDEO_OUT, AIRPLAY_AUDIO, REMOTE_CONTROL),
    "homepod": (AUDIO_OUT, AIRPLAY_AUDIO, REMOTE_CONTROL),
    "airplay_speaker": (AUDIO_OUT, AIRPLAY_AUDIO),
    "chromecast": (AUDIO_OUT, VIDEO_OUT, CAST_MEDIA, REMOTE_CONTROL),
    "cast_audio": (AUDIO_OUT, CAST_MEDIA, REMOTE_CONTROL),
    "cast_group": (AUDIO_OUT, CAST_MEDIA, REMOTE_CONTROL),
    "speaker": (AUDIO_OUT,),
    "tv": (AUDIO_OUT, VIDEO_OUT),
    "home_assistant": (AUTOMATION_HUB, WEB_UI),
    "homekit": (AUTOMATION_HUB,),
    "mqtt_broker": (AUTOMATION_HUB,),
    "esphome": (AUTOMATION_HUB, WEB_UI),
    "computer": (SHELL,),
    "printer": (),
    "web_service": (WEB_UI,),
    # Kinds that only the SSDP and neighbour scanners produce (core/lan.py).
    "game_console": (AUDIO_OUT, VIDEO_OUT),
    "printer_3d": (WEB_UI,),
    "nas": (WEB_UI,),
    "router": (WEB_UI,),
    "media_server": (AUDIO_OUT, VIDEO_OUT, WEB_UI),
    # A phone is a thing you own, not a thing you drive from here. It is listed
    # so you can see it and name it; project-os claims nothing about it.
    "phone": (),
    "smart_plug": (SWITCH,),
    "smart_light": (LIGHT,),
    "tuya_device": (),
    "microcontroller": (SERIAL_PORT, FLASHABLE),
    "serial_device": (SERIAL_PORT,),
    "unknown": (),
}

#: Lower wins when several services describe one box. "This is an Apple TV"
#: beats "this is something with a web page".
KIND_PRIORITY = {
    "apple_tv": 10,
    "homepod": 11,
    "cast_group": 12,
    "cast_audio": 13,
    "chromecast": 14,
    "airplay_speaker": 15,
    "tv": 16,
    "home_assistant": 20,
    "homekit": 25,
    "mqtt_broker": 26,
    "esphome": 27,
    "smart_light": 30,
    "smart_plug": 31,
    "tuya_device": 32,
    "microcontroller": 33,
    "serial_device": 34,
    "printer": 40,
    "printer_3d": 39,
    "speaker": 41,
    "game_console": 42,
    "media_server": 43,
    "nas": 44,
    "router": 45,
    "phone": 48,
    "computer": 50,
    "web_service": 60,
    "unknown": 90,
}

#: Apple ``am=`` / ``rpMd=`` model prefixes, most specific first.
APPLE_MODEL_KINDS = (
    ("appletv", "apple_tv"),
    ("audioaccessory", "homepod"),
    ("macmini", "computer"),
    ("macbook", "computer"),
    ("macpro", "computer"),
    ("macstudio", "computer"),
    ("imac", "computer"),
    ("mac", "computer"),
    ("airport", "airplay_speaker"),
    ("ipad", "tablet"),
    ("iphone", "phone"),
)

#: Cast ``md=`` models that are speakers rather than screens.
CAST_AUDIO_MODELS = (
    "chromecast audio",
    "google home",
    "google nest mini",
    "nest mini",
    "nest audio",
    "home mini",
    "home max",
    "google cast speaker",
    "cast speaker",
    "soundbar",
    "speaker",
)

CAST_GROUP_HINTS = ("cast group", "google cast group")

#: Ports probed on ``discovery.probe_hosts``: (port, kind, capabilities, label).
PROBE_PORTS = (
    (8009, "chromecast", (CAST_MEDIA, AUDIO_OUT), "googlecast"),
    (7000, "airplay_speaker", (AIRPLAY_AUDIO, AUDIO_OUT), "airplay"),
    (5000, "airplay_speaker", (AIRPLAY_AUDIO, AUDIO_OUT), "raop"),
    (8123, "home_assistant", (AUTOMATION_HUB, WEB_UI), "home-assistant"),
    (1883, "mqtt_broker", (AUTOMATION_HUB,), "mqtt"),
    (8883, "mqtt_broker", (AUTOMATION_HUB,), "mqtt-tls"),
    (631, "printer", (), "ipp"),
    (9100, "printer", (), "jetdirect"),
    (22, "computer", (SHELL,), "ssh"),
    (80, "web_service", (WEB_UI,), "http"),
    (443, "web_service", (WEB_UI,), "https"),
    (8080, "web_service", (WEB_UI,), "http-alt"),
)

#: USB serial adapters that mean "there is probably a microcontroller here"
#: (docs/SIMPLE-PROJECT-OS.md section 8). (vendor, product) -> human name.
USB_SERIAL_IDS = {
    ("10c4", "ea60"): "CP2102 (ESP32 dev board)",
    ("10c4", "ea70"): "CP2105 (ESP32 dev board)",
    ("1a86", "7523"): "CH340 (ESP32/Arduino clone)",
    ("1a86", "55d4"): "CH9102 (ESP32 dev board)",
    ("0403", "6001"): "FTDI FT232 (serial adapter)",
    ("0403", "6015"): "FTDI FT231X (serial adapter)",
    ("303a", "1001"): "ESP32-S2/S3/C3 (native USB)",
    ("303a", "0002"): "ESP32-S2 (native USB)",
    ("2341", "0043"): "Arduino Uno",
}

KASA_PORT = 9999
KASA_DISCOVERY_PAYLOAD = '{"system":{"get_sysinfo":{}}}'
KASA_LISTEN_SECONDS = 2.0
TUYA_PORTS = (6666, 6667)
TUYA_LISTEN_SECONDS = 3.0

_HEX_RE = re.compile(r"^[0-9a-f]+$")
_MDNS_ESCAPE_RE = re.compile(r"\\(\d{3}|.)")
_SERVICE_SUFFIX_RE = re.compile(r"\._[a-z0-9-]+\._(tcp|udp)\.?(local\.?)?$", re.IGNORECASE)
_ID_LOOKING_RE = re.compile(r"[0-9a-f]{16,}")
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")

_UNSET = object()


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def unescape_mdns(value: str) -> str:
    r"""Undo DNS-SD escaping: ``Living\032Room`` -> ``Living Room``."""

    def _replace(match: "re.Match") -> str:
        token = match.group(1)
        if len(token) == 3 and token.isdigit():
            try:
                return chr(int(token))
            except ValueError:  # pragma: no cover - unreachable for 3 digits
                return token
        return token

    return _MDNS_ESCAPE_RE.sub(_replace, value or "")


def clean_name(name: str, service_type: Optional[str] = None) -> str:
    """Instance name without the service suffix and without mDNS escaping."""
    text = name or ""
    if service_type and text.endswith("." + service_type):
        text = text[: -(len(service_type) + 1)]
    else:
        text = _SERVICE_SUFFIX_RE.sub("", text)
    text = unescape_mdns(text)
    for suffix in (".local.", ".local"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip().strip(".").strip()


def decode_txt(raw: Any) -> Dict[str, str]:
    """TXT records as ``str -> str``; zeroconf hands them over as bytes."""
    out = {}  # type: Dict[str, str]
    if not raw:
        return out
    items = raw.items() if hasattr(raw, "items") else raw
    try:
        pairs = list(items)
    except TypeError:  # pragma: no cover - defensive
        return out
    for pair in pairs:
        try:
            key, value = pair
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        try:
            if isinstance(key, bytes):
                key = key.decode("utf-8", "replace")
            key = str(key).strip()
            if not key:
                continue
            if isinstance(value, bytes):
                value = value.decode("utf-8", "replace")
            text = "" if value is None else str(value)
            out[key] = text[:MAX_PROPERTY_LENGTH]
        except Exception:  # pragma: no cover - a bad TXT record is not fatal
            continue
        if len(out) >= MAX_PROPERTIES:
            break
    return out


def _lower_get(properties: Dict[str, str], *names: str) -> str:
    """TXT keys are case-inconsistent in the wild (``fn``, ``Name``, ``md``)."""
    if not properties:
        return ""
    lowered = dict((k.lower(), v) for k, v in properties.items())
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value
    return ""


def _slug(value: str, max_length: int = 64) -> str:
    text = _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-.")
    return text[:max_length] or "unknown"


def _is_hex(text: str, length: Optional[int] = None) -> bool:
    if not text:
        return False
    if length is not None and len(text) != length:
        return False
    return bool(_HEX_RE.match(text))


def identity_token(value: str) -> Optional[str]:
    """Namespace a raw identifier so equal devices produce equal tokens.

    ``AA:BB:CC:DD:EE:FF`` and ``aabbccddeeff`` are the same MAC, and the same
    device seen through ``_airplay`` (``deviceid``) and ``_raop`` (the
    ``<MAC>@<name>`` instance) must land on one row.
    """
    text = (value or "").strip()
    if not text:
        return None
    compact = re.sub(r"[^0-9A-Za-z]", "", text).lower()
    if not compact or len(compact) < 4:
        return None
    if _is_hex(compact, 12):
        return "mac:" + compact
    if _is_hex(compact, 32):
        return "uuid:" + compact
    if len(compact) > 32:
        return "id:" + hashlib.sha1(compact.encode("utf-8")).hexdigest()[:16]
    return "id:" + compact


def stable_id(kind: str, token: Optional[str], host: str = "", port: int = 0) -> str:
    """``<kind>:<uuid-or-mac-or-host>``, identical on every scan.

    Falls back to ``<kind>:<host-or-ip>:<port>`` only when the device
    advertises nothing stable -- never to the address alone.
    """
    kind = kind or "unknown"
    if token:
        return "%s:%s" % (kind, _slug(token.replace(":", "-")))
    if host:
        return "%s:%s:%d" % (kind, _slug(host), int(port or 0))
    return "%s:unidentified" % kind


def order_capabilities(values: Iterable[str]) -> List[str]:
    """Deduplicate and sort by the documented order, extras last."""
    unique = set(v for v in values if v)
    known = [c for c in CAPABILITY_ORDER if c in unique]
    extra = sorted(unique.difference(CAPABILITY_ORDER))
    return known + extra


# ---------------------------------------------------------------------------
# kind inference
# ---------------------------------------------------------------------------
def apple_kind(model: str) -> Optional[str]:
    """``AppleTV14,1`` -> ``apple_tv``; ``AudioAccessory5,1`` -> ``homepod``."""
    text = (model or "").strip().lower()
    if not text:
        return None
    for prefix, kind in APPLE_MODEL_KINDS:
        if text.startswith(prefix):
            return kind
    return None


def cast_kind(properties: Dict[str, str]) -> str:
    """``cast_group`` | ``cast_audio`` | ``chromecast`` from the TXT records."""
    model = _lower_get(properties, "md", "model").strip()
    friendly = _lower_get(properties, "fn", "friendlyname").strip()
    lowered_model = model.lower()
    lowered_friendly = friendly.lower()
    if any(hint in lowered_model for hint in CAST_GROUP_HINTS):
        return "cast_group"
    if not model and lowered_friendly.endswith(" group"):
        return "cast_group"
    if lowered_model:
        for hint in CAST_AUDIO_MODELS:
            if hint in lowered_model:
                return "cast_audio"
        return "chromecast"
    # No model: the ``ca`` capability bitmask still says whether the device has
    # a video output (bit 0). A speaker answers with audio out and no video.
    raw = _lower_get(properties, "ca")
    try:
        bitmask = int(raw)
    except (TypeError, ValueError):
        return "chromecast"
    if bitmask & 0x04 and not bitmask & 0x01:
        return "cast_audio"
    return "chromecast"


def infer_kind(service_type: str, properties: Optional[Dict[str, str]] = None,
               instance: str = "") -> str:
    """The device kind implied by one answered service."""
    properties = properties or {}
    service_type = service_type or ""
    base = SERVICE_KINDS.get(service_type, "unknown")
    if service_type in ("_airplay._tcp.local.", "_raop._tcp.local."):
        model = _lower_get(properties, "am", "model", "md")
        return apple_kind(model) or base
    if service_type in ("_companion-link._tcp.local.", "_touch-able._tcp.local."):
        model = _lower_get(properties, "rpMd", "am", "model")
        return apple_kind(model) or base
    if service_type == "_googlecast._tcp.local.":
        return cast_kind(properties)
    if service_type in ("_http._tcp.local.", "_https._tcp.local."):
        # Plenty of appliances only advertise a web page; the name is the
        # single hint we get about what is behind it.
        label = ("%s %s" % (instance, _lower_get(properties, "md", "model"))).lower()
        if "home assistant" in label or "homeassistant" in label:
            return "home_assistant"
        if "printer" in label:
            return "printer"
        return base
    return base


def capabilities_for(
    kind: str,
    service_types: Sequence[str] = (),
    properties: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Everything a device of this kind, seen through these services, can do."""
    found = set(KIND_CAPABILITIES.get(kind, ()))
    for service_type in service_types or ():
        found.update(SERVICE_CAPABILITIES.get(service_type, ()))
    if properties:
        raw = _lower_get(properties, "ca")
        try:
            bitmask = int(raw)
        except (TypeError, ValueError):
            bitmask = None
        if bitmask is not None and kind in ("chromecast", "cast_audio", "cast_group"):
            if bitmask & 0x01:
                found.add(VIDEO_OUT)
            else:
                found.discard(VIDEO_OUT)
    return order_capabilities(found)


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------
@dataclass
class Device(object):
    """One physical thing, however many protocols it answers on."""

    id: str
    kind: str = "unknown"
    name: str = ""
    custom_name: Optional[str] = None
    address: str = ""
    port: int = 0
    properties: Dict[str, str] = field(default_factory=dict)
    capabilities: List[str] = field(default_factory=list)
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    pinned: bool = False
    ignored: bool = False
    online: bool = True

    @property
    def display_name(self) -> str:
        return self.custom_name or self.name or self.id

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "custom_name": self.custom_name,
            "display_name": self.display_name,
            "address": self.address,
            "port": int(self.port or 0),
            "properties": dict(self.properties or {}),
            "capabilities": list(self.capabilities or []),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "pinned": bool(self.pinned),
            "ignored": bool(self.ignored),
            "online": bool(self.online),
        }

    @classmethod
    def from_row(cls, row: Any, online: bool = False) -> "Device":
        """Rebuild a device from its ``devices`` row (JSON columns included)."""
        data = dict(row)
        properties = data.get("properties")
        if isinstance(properties, str):
            try:
                properties = json.loads(properties)
            except ValueError:
                properties = {}
        capabilities = data.get("capabilities")
        if isinstance(capabilities, str):
            try:
                capabilities = json.loads(capabilities)
            except ValueError:
                capabilities = []
        return cls(
            id=str(data.get("id") or ""),
            kind=str(data.get("kind") or "unknown"),
            name=str(data.get("name") or ""),
            custom_name=data.get("custom_name") or None,
            address=str(data.get("address") or ""),
            port=int(data.get("port") or 0),
            properties=properties if isinstance(properties, dict) else {},
            capabilities=capabilities if isinstance(capabilities, list) else [],
            first_seen=data.get("first_seen"),
            last_seen=data.get("last_seen"),
            pinned=bool(data.get("pinned")),
            ignored=bool(data.get("ignored")),
            online=bool(online),
        )


# ---------------------------------------------------------------------------
# Observation -- one answered service, before merging
# ---------------------------------------------------------------------------
@dataclass
class Observation(object):
    """What a single source said about a single endpoint."""

    source: str = "mdns"
    service_type: str = ""
    instance: str = ""
    kind: str = "unknown"
    name_hint: str = ""
    address: str = ""
    host: str = ""
    port: int = 0
    properties: Dict[str, str] = field(default_factory=dict)
    capabilities: List[str] = field(default_factory=list)
    strong_keys: List[str] = field(default_factory=list)
    weak_keys: List[str] = field(default_factory=list)

    def label(self) -> str:
        """The best human name this one observation can offer."""
        friendly = _lower_get(self.properties, "fn", "friendlyname", "name", "n")
        if friendly:
            return clean_name(friendly)
        if self.name_hint:
            return self.name_hint
        return instance_label(self.instance)


def instance_label(instance: str) -> str:
    """``AABBCCDDEEFF@Living Room`` -> ``Living Room`` (the ``_raop`` form)."""
    text = clean_name(instance or "")
    if "@" in text:
        left, _, right = text.partition("@")
        if right and _is_hex(left.lower(), 12):
            return right.strip()
    return text


def _looks_like_id(text: str) -> bool:
    return bool(_ID_LOOKING_RE.search((text or "").lower()))


def observe_service(
    service_type: str,
    name: str,
    addresses: Sequence[str] = (),
    port: int = 0,
    server: str = "",
    properties: Optional[Dict[str, str]] = None,
    source: str = "mdns",
) -> Observation:
    """Turn one resolved mDNS service into an :class:`Observation`.

    This is the seam the tests use: no zeroconf object required, just the
    values a resolved service carries.
    """
    properties = dict(properties or {})
    instance = clean_name(name, service_type)
    kind = infer_kind(service_type, properties, instance)
    address = ""
    for candidate in addresses or ():
        if candidate and ":" not in candidate:  # prefer IPv4
            address = candidate
            break
    if not address and addresses:
        address = addresses[0]
    host = clean_name(server or "")

    strong = []  # type: List[str]
    for value in (
        _lower_get(properties, "deviceid"),
        _lower_get(properties, "id"),
        _lower_get(properties, "uuid"),
        _lower_get(properties, "serialnumber", "sn"),
        _lower_get(properties, "mac"),
        _lower_get(properties, "pk"),
    ):
        token = identity_token(value)
        if token and token not in strong:
            strong.append(token)
    # A ``_raop`` instance is "<MAC>@<name>": the MAC half is the same identity
    # the AirPlay service reports as ``deviceid``, which is what fuses them.
    if "@" in instance:
        token = identity_token(instance.partition("@")[0])
        if token and token.startswith("mac:") and token not in strong:
            strong.insert(0, token)

    weak = []  # type: List[str]
    if host:
        weak.append("host:" + host.lower())
    if address:
        weak.append("addr:" + address)

    return Observation(
        source=source,
        service_type=service_type,
        instance=instance,
        kind=kind,
        name_hint="",
        address=address,
        host=host,
        port=int(port or 0),
        properties=properties,
        capabilities=capabilities_for(kind, (service_type,), properties),
        strong_keys=strong,
        weak_keys=weak,
    )


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------
class _UnionFind(object):
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self._parent[max(a, b)] = min(a, b)


def _kind_rank(kind: str) -> int:
    return KIND_PRIORITY.get(kind, KIND_PRIORITY["unknown"])


def _service_rank(observation: Observation) -> int:
    try:
        return SERVICE_TYPES.index(observation.service_type)
    except ValueError:
        return len(SERVICE_TYPES)


def merge_observations(observations: Sequence[Observation]) -> List[Device]:
    """Fold observations of the same box into one :class:`Device` each.

    See the module docstring for the two-pass rule; the short version is
    "strong ids always merge, a shared host only merges the anonymous".
    """
    items = [o for o in observations if o is not None]
    if not items:
        return []

    groups = _UnionFind(len(items))
    first_by_token = {}  # type: Dict[str, int]
    for index, observation in enumerate(items):
        for token in observation.strong_keys:
            other = first_by_token.setdefault(token, index)
            groups.union(index, other)

    def strong_of(root: int) -> Set[str]:
        tokens = set()  # type: Set[str]
        for index, observation in enumerate(items):
            if groups.find(index) == root:
                tokens.update(observation.strong_keys)
        return tokens

    by_weak = {}  # type: Dict[str, List[int]]
    for index, observation in enumerate(items):
        for token in observation.weak_keys:
            by_weak.setdefault(token, []).append(index)

    for members in by_weak.values():
        roots = []  # type: List[int]
        for index in members:
            root = groups.find(index)
            if root not in roots:
                roots.append(root)
        if len(roots) < 2:
            continue
        identified = [r for r in roots if strong_of(r)]
        anonymous = [r for r in roots if not strong_of(r)]
        if not anonymous:
            continue
        if len(identified) == 1:
            for root in anonymous:
                groups.union(root, identified[0])
        else:
            # Zero or several identified devices on this host: guessing which
            # one owns the anonymous services would invent a fact. Keep the
            # anonymous ones together and leave the identified ones alone.
            for root in anonymous[1:]:
                groups.union(root, anonymous[0])

    clustered = {}  # type: Dict[int, List[Observation]]
    for index, observation in enumerate(items):
        clustered.setdefault(groups.find(index), []).append(observation)

    devices = [_build_device(members) for members in clustered.values()]
    devices.sort(key=lambda d: (_kind_rank(d.kind), (d.name or "").lower(), d.id))
    return devices


def _pick_primary(members: Sequence[Observation], kind: str) -> Observation:
    matching = [o for o in members if o.kind == kind] or list(members)
    matching.sort(key=lambda o: (0 if o.address else 1, _service_rank(o), o.port or 65535))
    return matching[0]


def _pick_name(members: Sequence[Observation], primary: Observation) -> str:
    ordered = sorted(members, key=lambda o: (o is not primary, _service_rank(o)))
    for observation in ordered:
        friendly = _lower_get(observation.properties, "fn", "friendlyname", "name")
        if friendly:
            return clean_name(friendly)
    for observation in ordered:
        label = observation.name_hint or instance_label(observation.instance)
        if label and not _looks_like_id(label):
            return label
    for observation in ordered:
        model = _lower_get(observation.properties, "am", "md", "model", "rpMd")
        if model:
            return model
    for observation in ordered:
        if observation.host:
            return observation.host
    return primary.address or primary.instance or "Unknown device"


def _best_token(members: Sequence[Observation]) -> Optional[str]:
    """The strongest identity in the cluster: MAC, then UUID, then anything."""
    tokens = []  # type: List[str]
    for observation in sorted(members, key=_service_rank):
        for token in observation.strong_keys:
            if token not in tokens:
                tokens.append(token)
    for prefix in ("mac:", "uuid:", "id:"):
        for token in tokens:
            if token.startswith(prefix):
                return token
    return None


def _build_device(members: Sequence[Observation]) -> Device:
    kind = min((o.kind for o in members), key=_kind_rank)
    primary = _pick_primary(members, kind)
    service_types = [o.service_type for o in members if o.service_type]

    capabilities = set(capabilities_for(kind, service_types, primary.properties))
    for observation in members:
        capabilities.update(observation.capabilities)
    if kind in ("cast_audio", "cast_group"):
        capabilities.discard(VIDEO_OUT)

    properties = {}  # type: Dict[str, str]
    for observation in sorted(members, key=_service_rank, reverse=True):
        properties.update(observation.properties)
    properties.update(
        {
            "services": ",".join(sorted(set(service_types))),
            "sources": ",".join(sorted(set(o.source for o in members))),
            "instance": primary.instance,
        }
    )
    if primary.host:
        properties["host"] = primary.host
    model = _lower_get(properties, "am", "md", "model", "rpMd")
    if model:
        properties["model"] = model
    properties = dict(list(properties.items())[:MAX_PROPERTIES])

    address = primary.address or next((o.address for o in members if o.address), "")
    host = primary.host or next((o.host for o in members if o.host), "")
    return Device(
        id=stable_id(kind, _best_token(members), host or address, primary.port),
        kind=kind,
        name=_pick_name(members, primary),
        address=address,
        port=int(primary.port or 0),
        properties=properties,
        capabilities=order_capabilities(capabilities),
        online=True,
    )


# ---------------------------------------------------------------------------
# mDNS (optional dependency)
# ---------------------------------------------------------------------------
def zeroconf_available() -> Tuple[bool, Optional[str]]:
    """``(available, reason)`` -- the reason is what the UI shows when not."""
    try:
        import zeroconf  # noqa: F401
    except ImportError as exc:
        return False, "%s (%s)" % (ZEROCONF_HINT, exc)
    try:
        from zeroconf.asyncio import AsyncZeroconf  # noqa: F401
    except ImportError as exc:  # pragma: no cover - very old zeroconf
        return False, "the installed zeroconf has no asyncio support (%s)" % exc
    return True, None


def _addresses_of(info: Any) -> List[str]:
    getter = getattr(info, "parsed_addresses", None)
    if callable(getter):
        try:
            return [str(a) for a in getter()]
        except Exception:  # pragma: no cover - defensive
            pass
    out = []  # type: List[str]
    for raw in getattr(info, "addresses", None) or ():
        try:
            if len(raw) == 4:
                out.append(socket.inet_ntoa(raw))
            elif len(raw) == 16:
                out.append(socket.inet_ntop(socket.AF_INET6, raw))
        except (OSError, ValueError, TypeError):  # pragma: no cover
            continue
    return out


def _observation_from_info(service_type: str, name: str, info: Any) -> Optional[Observation]:
    raw_properties = getattr(info, "properties", None)
    properties = decode_txt(raw_properties)
    if not properties:
        decoded = getattr(info, "decoded_properties", None)
        if isinstance(decoded, dict):
            properties = decode_txt(decoded)
    addresses = _addresses_of(info)
    server = getattr(info, "server", "") or ""
    if not addresses and not server:
        return None
    return observe_service(
        service_type,
        name,
        addresses=addresses,
        port=int(getattr(info, "port", 0) or 0),
        server=server,
        properties=properties,
    )


async def async_scan_mdns(
    timeout: float = DEFAULT_SCAN_TIMEOUT,
    service_types: Sequence[str] = SERVICE_TYPES,
    unicast_addresses: Sequence[str] = (),
) -> Tuple[List[Observation], Optional[str]]:
    """Browse every service type at once. Returns ``(observations, error)``.

    ``unicast_addresses`` additionally sends the same queries straight at those
    addresses, which is how a device that does not answer multicast (a different
    subnet, or an access point that eats it) can still be found.
    """
    available, reason = zeroconf_available()
    if not available:
        return [], reason

    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    try:
        azc = AsyncZeroconf()
    except Exception as exc:
        return [], "cannot open a multicast socket: %s" % exc

    observations = []  # type: List[Observation]
    tasks = {}  # type: Dict[Tuple[str, str], asyncio.Future]
    browsers = []  # type: List[Any]

    async def _resolve(service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        try:
            found = await info.async_request(azc.zeroconf, RESOLVE_TIMEOUT_MS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("cannot resolve %s: %s", name, exc)
            return
        if not found:
            return
        try:
            observation = _observation_from_info(service_type, name, info)
        except Exception:  # pragma: no cover - a weird TXT record is not fatal
            log.debug("cannot read %s", name, exc_info=True)
            return
        if observation is not None:
            observations.append(observation)

    def _on_change(zeroconf: Any = None, service_type: str = "", name: str = "",
                   state_change: Any = None, **_extra: Any) -> None:
        if state_change is ServiceStateChange.Removed:
            return
        key = (service_type, name)
        if key in tasks:
            return
        tasks[key] = asyncio.ensure_future(_resolve(service_type, name))

    error = None  # type: Optional[str]
    try:
        # Zeroconf's engine starts on its own task. A browser created before it
        # is up parks on async_wait_for_start(), and if the scan window closes
        # first that wait raises NotRunningException on a task nobody awaits --
        # which lands in the log as "Task exception was never retrieved" during
        # every boot. Waiting here costs milliseconds and removes it.
        waiter = getattr(azc.zeroconf, "async_wait_for_start", None)
        if waiter is not None:
            try:
                await asyncio.wait_for(waiter(), timeout=5.0)
            except Exception as exc:  # a slow start is not a reason to give up
                log.debug("zeroconf did not report itself started: %s", exc)
        types = [t for t in service_types]
        browsers.append(AsyncServiceBrowser(azc.zeroconf, types, handlers=[_on_change]))
        for address in unicast_addresses or ():
            try:
                browsers.append(
                    AsyncServiceBrowser(
                        azc.zeroconf, types, handlers=[_on_change],
                        addr=address, port=MDNS_PORT,
                    )
                )
            except Exception as exc:
                log.debug("unicast mDNS to %s is unavailable: %s", address, exc)
        await asyncio.sleep(max(0.5, float(timeout)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = "mDNS browse failed: %s" % exc
        log.warning("mDNS browse failed: %s", exc)
    finally:
        for browser in browsers:
            try:
                await browser.async_cancel()
            except Exception:  # pragma: no cover - shutdown noise
                log.debug("cancelling a browser failed", exc_info=True)
        pending = [t for t in tasks.values() if not t.done()]
        if pending:
            done, still_pending = await asyncio.wait(pending, timeout=RESOLVE_GRACE_SECONDS)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
        try:
            await azc.async_close()
        except Exception:  # pragma: no cover - shutdown noise
            log.debug("closing zeroconf failed", exc_info=True)
    return observations, error


# ---------------------------------------------------------------------------
# direct probing of configured hosts
# ---------------------------------------------------------------------------
async def _tcp_open(address: str, port: int, timeout: float) -> bool:
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout=timeout
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # pragma: no cover
                pass


async def resolve_host(host: str) -> Optional[str]:
    """Name -> IPv4 address, without blocking the loop."""
    text = (host or "").strip()
    if not text:
        return None
    try:
        socket.inet_aton(text)
        return text
    except OSError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(text, None, family=socket.AF_INET,
                                       type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        log.debug("cannot resolve probe host %s: %s", text, exc)
        return None
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0]:
            return str(sockaddr[0])
    return None


async def async_probe_host(
    host: str, timeout: float = PROBE_CONNECT_TIMEOUT
) -> Optional[Observation]:
    """Ask a host directly what it is, by knocking on well-known ports."""
    address = await resolve_host(host)
    if address is None:
        return None
    results = await asyncio.gather(
        *[_tcp_open(address, entry[0], timeout) for entry in PROBE_PORTS],
        return_exceptions=True,
    )
    open_ports = []  # type: List[Tuple[int, str, Tuple[str, ...], str]]
    for entry, ok in zip(PROBE_PORTS, results):
        if ok is True:
            open_ports.append(entry)
    if not open_ports:
        return None
    best = min(open_ports, key=lambda entry: _kind_rank(entry[1]))
    capabilities = set()  # type: Set[str]
    for entry in open_ports:
        capabilities.update(entry[2])
    capabilities.update(KIND_CAPABILITIES.get(best[1], ()))
    label = host if host != address else ""
    weak = ["addr:" + address]
    if label:
        weak.append("host:" + label.lower())
    return Observation(
        source="probe",
        service_type="",
        instance=host,
        kind=best[1],
        name_hint=label or address,
        address=address,
        host=label,
        port=best[0],
        properties={
            "probed": "true",
            "open_ports": ",".join(str(entry[0]) for entry in open_ports),
            "services_probed": ",".join(entry[3] for entry in open_ports),
            "configured_host": host,
        },
        capabilities=order_capabilities(capabilities),
        strong_keys=[],
        weak_keys=weak,
    )


# ---------------------------------------------------------------------------
# dependency-free extra scanners (blocking -- always run in an executor)
# ---------------------------------------------------------------------------
def kasa_encrypt(payload: str) -> bytes:
    """TP-Link's XOR-with-running-key "encryption". The key starts at 171."""
    key = 171
    out = bytearray()
    for char in payload.encode("utf-8"):
        key = key ^ char
        out.append(key)
    return bytes(out)


def kasa_decrypt(payload: bytes) -> str:
    key = 171
    out = bytearray()
    for char in payload:
        out.append(key ^ char)
        key = char
    return out.decode("utf-8", "replace")


def scan_kasa(timeout: float = KASA_LISTEN_SECONDS) -> List[Observation]:
    """Broadcast the Kasa discovery datagram and collect what answers.

    No dependency: the protocol is a UDP broadcast of an obfuscated JSON blob.
    ``python-kasa`` is only needed to *control* a plug, not to see it.
    """
    found = []  # type: List[Observation]
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        sock.sendto(kasa_encrypt(KASA_DISCOVERY_PAYLOAD), ("255.255.255.255", KASA_PORT))
        deadline = time.time() + timeout
        seen = set()  # type: Set[str]
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:  # pragma: no cover
                break
            if addr[0] in seen:
                continue
            seen.add(addr[0])
            try:
                info = json.loads(kasa_decrypt(data))["system"]["get_sysinfo"]
            except (ValueError, KeyError, TypeError):
                continue
            mac = (info.get("mac") or info.get("mic_mac") or "").replace(":", "").lower()
            model = str(info.get("model", ""))
            is_light = "bulb" in str(info.get("mic_type", "")) or model.upper().startswith("KL")
            kind = "smart_light" if is_light else "smart_plug"
            capabilities = set(KIND_CAPABILITIES.get(kind, ()))
            if "ENE" in str(info.get("feature", "")):
                capabilities.add(ENERGY_MONITOR)
            token = identity_token(mac)
            found.append(
                Observation(
                    source="kasa",
                    kind=kind,
                    instance=str(info.get("alias") or model or "Kasa device"),
                    name_hint=str(info.get("alias") or model or "Kasa device"),
                    address=addr[0],
                    port=KASA_PORT,
                    properties={
                        "vendor": "TP-Link Kasa",
                        "model": model,
                        "mac": mac,
                        "hw": str(info.get("hw_ver", "")),
                        "sw": str(info.get("sw_ver", "")),
                        "state": str(info.get("relay_state", info.get("light_state", ""))),
                    },
                    capabilities=order_capabilities(capabilities),
                    strong_keys=[token] if token else [],
                    weak_keys=["addr:" + addr[0]],
                )
            )
    except OSError as exc:
        log.debug("Kasa discovery unavailable: %s", exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass
    return found


def _tuya_payload(data: bytes) -> Optional[Dict[str, Any]]:
    """Pull the JSON out of a Tuya UDP beacon.

    Frame: 4-byte prefix 0x000055AA, sequence, command, length, payload,
    8-byte suffix. Protocol 3.3+ encrypts the payload with a fixed AES key,
    which would need a crypto dependency -- those come back as ``None`` and the
    device is still seen, because the beacon's source address is enough to know
    it exists.
    """
    if len(data) < 20 or data[:4] != b"\x00\x00U\xaa":
        return None
    try:
        length = struct.unpack(">I", data[12:16])[0]
    except struct.error:  # pragma: no cover
        return None
    body = data[20 : 20 + max(0, length - 8)]
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def scan_tuya(timeout: float = TUYA_LISTEN_SECONDS) -> List[Observation]:
    """Listen for the beacons Tuya devices broadcast every ~10 seconds.

    Purely passive -- nothing is sent. Controlling one needs a local key that is
    not discoverable at all, so the device is reported with no capabilities and
    a ``needs_local_key`` flag the suggestion engine can explain.
    """
    found = {}  # type: Dict[str, Observation]
    sockets = []  # type: List[socket.socket]
    try:
        for port in TUYA_PORTS:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.settimeout(0.5)
                sock.bind(("", port))
                sockets.append(sock)
            except OSError as exc:
                log.debug("cannot listen on UDP %d for Tuya: %s", port, exc)
        if not sockets:
            return []
        deadline = time.time() + timeout
        while time.time() < deadline:
            for sock in sockets:
                try:
                    data, addr = sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    continue
                payload = _tuya_payload(data)
                if payload is None:
                    continue
                gwid = str(payload.get("gwId") or payload.get("id") or addr[0])
                address = str(payload.get("ip") or addr[0])
                token = identity_token(gwid)
                found[gwid] = Observation(
                    source="tuya",
                    kind="tuya_device",
                    instance=gwid,
                    name_hint="Tuya device %s" % gwid[-6:],
                    address=address,
                    port=6668,
                    properties={
                        "vendor": "Tuya",
                        "gwId": gwid,
                        "version": str(payload.get("version", "")),
                        "productKey": str(payload.get("productKey", "")),
                        "needs_local_key": "true",
                    },
                    capabilities=[],
                    strong_keys=[token] if token else [],
                    weak_keys=["addr:" + address],
                )
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass
    return list(found.values())


def _sys_read(path: str) -> str:
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except (IOError, OSError):
        return ""


def scan_serial() -> List[Observation]:
    """Microcontrollers on the USB port, found without pyserial.

    Every USB tty in ``/sys/class/tty`` has the vendor and product id of its
    adapter a few directories up, which is enough to say "this is an ESP32 dev
    board" and offer to flash Simpleproject-os onto it.
    """
    found = []  # type: List[Observation]
    tty_root = "/sys/class/tty"
    if not os.path.isdir(tty_root):
        return found
    try:
        names = sorted(os.listdir(tty_root))
    except OSError:  # pragma: no cover
        return found
    for name in names:
        if not (name.startswith("ttyUSB") or name.startswith("ttyACM")):
            continue
        vendor = product = serial_number = manufacturer = ""
        current = os.path.join(tty_root, name, "device")
        # Walk up: ttyUSB0/device -> .../1-1.4:1.0 -> .../1-1.4 holds the ids.
        for _ in range(4):
            current = os.path.dirname(os.path.realpath(current))
            vendor = _sys_read(os.path.join(current, "idVendor"))
            if vendor:
                product = _sys_read(os.path.join(current, "idProduct"))
                serial_number = _sys_read(os.path.join(current, "serial"))
                manufacturer = _sys_read(os.path.join(current, "manufacturer"))
                break
        label = USB_SERIAL_IDS.get((vendor.lower(), product.lower()))
        kind = "microcontroller" if label else "serial_device"
        token = identity_token(serial_number or "%s%s%s" % (vendor, product, name))
        found.append(
            Observation(
                source="serial",
                kind=kind,
                instance=name,
                name_hint=label or (manufacturer and "%s (%s)" % (manufacturer, name)) or name,
                address="/dev/%s" % name,
                port=0,
                properties={
                    "vendor_id": vendor,
                    "product_id": product,
                    "serial": serial_number,
                    "manufacturer": manufacturer,
                    "flashable": "true" if label else "false",
                },
                capabilities=order_capabilities(KIND_CAPABILITIES.get(kind, ())),
                strong_keys=[token] if token else [],
                weak_keys=[],
            )
        )
    return found


EXTRA_SCANNERS = (("kasa", scan_kasa), ("tuya", scan_tuya), ("serial", scan_serial))

#: Scanners that live in :mod:`project_os.core.lan`, named rather than imported.
#: That module imports this one for the capability vocabulary, so importing it
#: back at the top would be a cycle; resolving the name at scan time is the
#: cheapest way out, and it costs one dict lookup per scan.
LAN_SCANNERS = ("ssdp", "neighbours")


def all_scanners() -> List[Any]:
    """``(name, callable)`` for every dependency-free scanner we have."""
    scanners = list(EXTRA_SCANNERS)
    try:
        from project_os.core import lan
    except Exception:  # pragma: no cover - only a broken install gets here
        log.warning("the LAN scanners are unavailable", exc_info=True)
        return scanners
    for name in LAN_SCANNERS:
        scanner = getattr(lan, "scan_" + name, None)
        if callable(scanner):
            scanners.append((name, scanner))
    return scanners


#: How long any one scanner gets. Past this its result is dropped and the scan
#: finishes without it -- a wedged UDP socket must not be the reason the device
#: list never appears.
SCANNER_TIMEOUT = 20.0


def run_extra_scanners() -> List[Observation]:
    """Every dependency-free scanner, at the same time.

    They were sequential once, and the scan took as long as all of them added
    together: five seconds of SSDP *then* however long the neighbour table's
    reverse lookups take *then* the Kasa broadcast. They wait on different
    sockets and never touch each other, so they run together and the scan costs
    the slowest one instead of the sum.

    One failing never stops the others; one hanging never stops the scan.
    """
    import concurrent.futures

    scanners = all_scanners()
    if not scanners:
        return []
    out = []  # type: List[Observation]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(scanners), thread_name_prefix="project_os-scan"
    ) as pool:
        futures = {pool.submit(scanner): name for name, scanner in scanners}
        for future in concurrent.futures.as_completed(futures, timeout=None):
            name = futures[future]
            try:
                found = future.result(timeout=SCANNER_TIMEOUT)
            except Exception:
                log.warning("the %s scanner failed", name, exc_info=True)
                continue
            log.debug("%s scanner found %d device(s)", name, len(found))
            out.extend(found)
    return out


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------
class DeviceRegistry(object):
    """Owns the scan loop, the ``devices`` table and the device events.

    A device is never deleted by a scan. Something that answered yesterday and
    is quiet today is marked **offline**, not removed -- an unplugged Chromecast
    silently vanishing from the list, taking the app settings that point at it
    along, is exactly the kind of bug that makes a box feel unreliable.
    """

    def __init__(self, db: Any = None, bus: Any = None, config: Any = None) -> None:
        self.db = db
        self.bus = bus
        self.config = config
        self._task = None  # type: Optional[asyncio.Task]
        self._scan_lock = asyncio.Lock()
        self._online = set()  # type: Set[str]
        self._seeded = False
        self.last_scan = None  # type: Optional[str]
        self.last_scan_seconds = None  # type: Optional[float]
        self.last_error = None  # type: Optional[str]
        self.scan_count = 0
        available, reason = zeroconf_available()
        self.backend_available = available
        self.backend_reason = reason

    # -- configuration ---------------------------------------------------
    def _cfg(self, path: str, default: Any) -> Any:
        if self.config is None:
            return default
        try:
            value = self.config.get(path, default)
        except Exception:  # pragma: no cover - a broken config is not fatal
            return default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("discovery.enabled", True))

    @property
    def interval(self) -> float:
        try:
            return max(MIN_INTERVAL, float(self._cfg("discovery.interval_seconds",
                                                     DEFAULT_INTERVAL)))
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL

    @property
    def offline_after(self) -> float:
        """A device is lost after three intervals without an answer."""
        return self.interval * LOST_AFTER_INTERVALS

    @property
    def probe_hosts(self) -> List[str]:
        raw = self._cfg("discovery.probe_hosts", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(h).strip() for h in raw if str(h).strip()]

    @property
    def extra_scanners_enabled(self) -> bool:
        return bool(self._cfg("discovery.extra_scanners", True))

    # -- lifecycle -------------------------------------------------------
    async def start_periodic(self) -> None:
        """Begin the periodic scan.

        The first scan is deliberately not awaited: an mDNS browse takes several
        seconds and making the web interface unreachable until the network has
        been catalogued would be the wrong trade on every single boot.
        """
        if self._task is not None or not self.enabled:
            return
        self._task = asyncio.ensure_future(self._loop())

    #: ``main.py`` looks for ``start``; the contract calls it ``start_periodic``.
    start = start_periodic

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - shutdown noise
            log.debug("the discovery loop ended badly", exc_info=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("device scan failed", exc_info=True)
            await asyncio.sleep(self.interval)

    # -- scanning --------------------------------------------------------
    async def scan(self, timeout: float = DEFAULT_SCAN_TIMEOUT) -> List[Device]:
        """Sweep every source, persist the result, emit what changed."""
        if not self.enabled:
            return self.list()
        if self._scan_lock.locked():
            # A manual scan on top of the periodic one: wait for the running
            # sweep instead of putting a second one on the wire behind it.
            async with self._scan_lock:
                return self.list()
        async with self._scan_lock:
            started = time.time()
            observations, errors = await self._collect(timeout)
            devices = merge_observations(observations)
            self.scan_count += 1
            self.last_scan = utcnow_iso()
            self.last_scan_seconds = round(time.time() - started, 2)
            self.last_error = "; ".join(errors) if errors else None
            found, updated = self._persist(devices)
            lost = self._sweep()
            log.info(
                "discovery: %d device(s) in %.1fs (%d new, %d updated, %d lost)",
                len(devices), self.last_scan_seconds, found, updated, lost,
            )
            return self.list()

    async def _collect(self, timeout: float) -> Tuple[List[Observation], List[str]]:
        """Run every source concurrently; one failing never stops the others."""
        timeout = max(1.0, float(timeout or DEFAULT_SCAN_TIMEOUT))
        errors = []  # type: List[str]
        hosts = self.probe_hosts

        unicast = []  # type: List[str]
        if hosts:
            resolved = await asyncio.gather(
                *[resolve_host(host) for host in hosts], return_exceptions=True
            )
            unicast = [r for r in resolved if isinstance(r, str) and r]

        jobs = [async_scan_mdns(timeout, SERVICE_TYPES, unicast)]
        jobs.extend(async_probe_host(host) for host in hosts)
        if self.extra_scanners_enabled:
            loop = asyncio.get_running_loop()
            jobs.append(loop.run_in_executor(None, run_extra_scanners))

        results = await asyncio.gather(*jobs, return_exceptions=True)
        observations = []  # type: List[Observation]
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                errors.append("%s: %s" % (type(result).__name__, result))
                log.warning("a discovery source failed: %s", result)
                continue
            if isinstance(result, tuple):  # async_scan_mdns
                found, error = result
                observations.extend(found)
                if error:
                    errors.append(error)
            elif isinstance(result, list):
                observations.extend(result)
            elif isinstance(result, Observation):
                observations.append(result)
        return observations, errors

    # -- persistence -----------------------------------------------------
    def _emit(self, topic: str, data: Dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish_nowait(topic, data)
        except Exception:  # pragma: no cover - the bus is not worth a crash
            log.debug("could not publish %s", topic, exc_info=True)

    def _persist(self, devices: Sequence[Device]) -> Tuple[int, int]:
        """Upsert every device found; returns ``(new, updated)`` counts.

        User-owned columns (``custom_name``, ``pinned``, ``ignored``) and
        ``first_seen`` are never overwritten by a scan.
        """
        if self.db is None or not devices:
            return (0, 0)
        now = self.last_scan or utcnow_iso()
        try:
            existing = dict(
                (row["id"], row) for row in self.db.query("SELECT * FROM devices")
            )
        except Exception:  # pragma: no cover - unmigrated database
            log.warning("cannot read the devices table", exc_info=True)
            return (0, 0)

        events = []  # type: List[Tuple[str, Dict[str, Any]]]
        new_count = 0
        updated_count = 0
        try:
            with self.db.transaction():
                for device in devices:
                    row = existing.get(device.id)
                    properties = json.dumps(device.properties, default=str)
                    capabilities = json.dumps(device.capabilities)
                    if row is None:
                        self.db.execute(
                            "INSERT INTO devices (id, kind, name, address, port,"
                            " properties, capabilities, first_seen, last_seen)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (device.id, device.kind, device.name, device.address,
                             int(device.port or 0), properties, capabilities, now, now),
                        )
                        device.first_seen = now
                        device.last_seen = now
                        device.online = True
                        new_count += 1
                        events.append(("device.found", device.to_dict()))
                        continue
                    self.db.execute(
                        "UPDATE devices SET kind = ?, name = ?, address = ?, port = ?,"
                        " properties = ?, capabilities = ?, last_seen = ? WHERE id = ?",
                        (device.kind, device.name, device.address, int(device.port or 0),
                         properties, capabilities, now, device.id),
                    )
                    changed = (
                        row["kind"] != device.kind
                        or row["name"] != device.name
                        or row["address"] != device.address
                        or int(row["port"] or 0) != int(device.port or 0)
                        or row["capabilities"] != capabilities
                        or row["properties"] != properties
                        or device.id not in self._online
                    )
                    if changed:
                        device.first_seen = row["first_seen"]
                        device.last_seen = now
                        device.custom_name = row["custom_name"] or None
                        device.pinned = bool(row["pinned"])
                        device.ignored = bool(row["ignored"])
                        device.online = True
                        updated_count += 1
                        events.append(("device.updated", device.to_dict()))
        except Exception:
            log.warning("could not store the discovered devices", exc_info=True)
            return (0, 0)

        self._online.update(d.id for d in devices)
        self._seeded = True
        for topic, payload in events:
            self._emit(topic, payload)
        return (new_count, updated_count)

    def _sweep(self) -> int:
        """Mark long-missing devices offline and emit ``device.lost`` once."""
        if self.db is None:
            return 0
        try:
            rows = self.db.query("SELECT * FROM devices")
        except Exception:  # pragma: no cover - unmigrated database
            return 0
        now = _utcnow()
        cutoff = now - timedelta(seconds=self.offline_after)
        lost = 0
        seeding = not self._seeded
        for row in rows:
            seen = _parse_iso(row["last_seen"])
            online = seen is not None and seen >= cutoff
            if online:
                self._online.add(row["id"])
                continue
            if seeding:
                # First sweep of this process: everything already stale was lost
                # before we started, and re-announcing it would be noise.
                self._online.discard(row["id"])
                continue
            if row["id"] in self._online:
                self._online.discard(row["id"])
                lost += 1
                self._emit("device.lost", Device.from_row(row, online=False).to_dict())
        self._seeded = True
        return lost

    # -- queries ---------------------------------------------------------
    def _row_to_device(self, row: Any, now: Optional[datetime] = None) -> Device:
        """``online`` is derived, never stored: a boolean column would go stale
        the moment a scan was skipped, and lie about it afterwards."""
        now = now or _utcnow()
        seen = _parse_iso(row["last_seen"] if hasattr(row, "keys") else row.get("last_seen"))
        online = seen is not None and (now - seen).total_seconds() <= self.offline_after
        return Device.from_row(row, online=online)

    def list(
        self,
        kind: Optional[str] = None,
        capability: Optional[str] = None,
        include_ignored: bool = False,
    ) -> List[Device]:
        if self.db is None:
            return []
        sql = "SELECT * FROM devices"
        clauses = []  # type: List[str]
        params = []  # type: List[Any]
        if not include_ignored:
            clauses.append("ignored = 0")
        if kind:
            clauses.append("kind = ?")
            params.append(str(kind))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY pinned DESC, kind, COALESCE(custom_name, name) COLLATE NOCASE"
        try:
            rows = self.db.query(sql, params)
        except Exception:  # pragma: no cover - unmigrated database
            log.debug("cannot read the devices table", exc_info=True)
            return []
        now = _utcnow()
        devices = [self._row_to_device(row, now) for row in rows]
        if capability:
            devices = [d for d in devices if capability in d.capabilities]
        return devices

    def get(self, device_id: str) -> Optional[Device]:
        if self.db is None or not device_id:
            return None
        try:
            row = self.db.one("SELECT * FROM devices WHERE id = ?", (device_id,))
        except Exception:  # pragma: no cover
            return None
        return self._row_to_device(row) if row is not None else None

    def by_capability(self, capability: str, include_ignored: bool = False) -> List[Device]:
        """BirdTunes and the suggestion engine ask "what can play audio?"."""
        return self.list(capability=capability, include_ignored=include_ignored)

    def devices(self, include_ignored: bool = False) -> List[Dict[str, Any]]:
        """The same list as :meth:`list`, already in wire shape."""
        return [device.to_dict() for device in self.list(include_ignored=include_ignored)]

    def counts_by_kind(self, include_ignored: bool = True) -> Dict[str, int]:
        counts = {}  # type: Dict[str, int]
        for device in self.list(include_ignored=include_ignored):
            counts[device.kind] = counts.get(device.kind, 0) + 1
        return counts

    def status(self) -> Dict[str, Any]:
        """Why the device list looks the way it does.

        The UI renders this instead of an empty screen: "discovery unavailable,
        zeroconf is not installed" is a fixable problem, silence is not.
        """
        available, reason = zeroconf_available()
        self.backend_available = available
        self.backend_reason = reason
        devices = self.list(include_ignored=True)
        by_kind = {}  # type: Dict[str, int]
        for device in devices:
            by_kind[device.kind] = by_kind.get(device.kind, 0) + 1
        return {
            "available": available,
            "backend": "zeroconf" if available else None,
            "reason": reason,
            "install_hint": None if available else ZEROCONF_HINT,
            "enabled": self.enabled,
            "scanning": self._scan_lock.locked(),
            "running": self._task is not None and not self._task.done(),
            "last_scan": self.last_scan,
            "last_scan_seconds": self.last_scan_seconds,
            "last_error": self.last_error,
            "scan_count": self.scan_count,
            "interval_seconds": self.interval,
            "offline_after_seconds": self.offline_after,
            "probe_hosts": self.probe_hosts,
            "extra_scanners": (
                [name for name, _ in all_scanners()] if self.extra_scanners_enabled else []
            ),
            "service_types": [t for t in SERVICE_TYPES],
            "count": len(devices),
            "online": sum(1 for d in devices if d.online),
            "ignored": sum(1 for d in devices if d.ignored),
            "pinned": sum(1 for d in devices if d.pinned),
            "by_kind": by_kind,
        }

    #: The dashboard called this ``summary`` before ``status`` existed.
    summary = status

    # -- mutations -------------------------------------------------------
    def update_flags(
        self,
        device_id: str,
        custom_name: Any = _UNSET,
        pinned: Optional[bool] = None,
        ignored: Optional[bool] = None,
    ) -> Optional[Device]:
        """Apply the user's own decisions. Omitted arguments are left alone;
        ``custom_name=None`` explicitly clears the name back to the discovered
        one, which is why it needs a sentinel rather than ``None`` as default."""
        if self.db is None:
            return None
        current = self.get(device_id)
        if current is None:
            return None
        assignments = []  # type: List[str]
        params = []  # type: List[Any]
        if custom_name is not _UNSET:
            text = (str(custom_name).strip() or None) if custom_name is not None else None
            assignments.append("custom_name = ?")
            params.append(text)
        if pinned is not None:
            assignments.append("pinned = ?")
            params.append(1 if pinned else 0)
        if ignored is not None:
            assignments.append("ignored = ?")
            params.append(1 if ignored else 0)
        if not assignments:
            return current
        params.append(device_id)
        try:
            self.db.execute(
                "UPDATE devices SET %s WHERE id = ?" % ", ".join(assignments), params
            )
        except Exception:
            log.warning("could not update device %s", device_id, exc_info=True)
            return current
        updated = self.get(device_id)
        if updated is not None:
            self._emit("device.updated", updated.to_dict())
        return updated

    def rename(self, device_id: str, name: Optional[str]) -> Optional[Device]:
        return self.update_flags(device_id, custom_name=name)

    def set_flag(self, device_id: str, flag: str, value: bool) -> Optional[Device]:
        if flag == "pinned":
            return self.update_flags(device_id, pinned=bool(value))
        if flag == "ignored":
            return self.update_flags(device_id, ignored=bool(value))
        return None

    def forget(self, device_id: str) -> bool:
        """Delete a row outright. It comes back if the device is still there."""
        if self.db is None:
            return False
        try:
            cursor = self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        except Exception:  # pragma: no cover
            return False
        self._online.discard(device_id)
        return bool(cursor.rowcount)

    def __repr__(self) -> str:
        return "DeviceRegistry(scans=%d, last=%s)" % (self.scan_count, self.last_scan)


__all__ = [
    "AIRPLAY_AUDIO",
    "AUDIO_OUT",
    "AUTOMATION_HUB",
    "CAPABILITIES",
    "CAST_MEDIA",
    "Device",
    "DeviceRegistry",
    "EXTRA_CAPABILITIES",
    "KIND_CAPABILITIES",
    "KIND_PRIORITY",
    "Observation",
    "REMOTE_CONTROL",
    "SERVICE_CAPABILITIES",
    "SERVICE_KINDS",
    "SERVICE_TYPES",
    "SHELL",
    "USB_SERIAL_IDS",
    "VIDEO_OUT",
    "WEB_UI",
    "ZEROCONF_HINT",
    "apple_kind",
    "async_probe_host",
    "async_scan_mdns",
    "capabilities_for",
    "cast_kind",
    "clean_name",
    "decode_txt",
    "identity_token",
    "infer_kind",
    "instance_label",
    "kasa_decrypt",
    "kasa_encrypt",
    "merge_observations",
    "observe_service",
    "order_capabilities",
    "resolve_host",
    "all_scanners",
    "run_extra_scanners",
    "scan_kasa",
    "scan_serial",
    "scan_tuya",
    "stable_id",
    "unescape_mdns",
    "zeroconf_available",
]
