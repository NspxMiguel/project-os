"""Two more ways of seeing the network: SSDP, and the kernel's neighbour table.

    "ele detecta praticamente tudo na sua rede, celular, impressora, pc, xbox,
    ps5, qualquer coisa msm, ate impressora 3d"

mDNS (in :mod:`project_os.core.discovery`) finds the things that *announce*
themselves, which is mostly the Apple and Google half of a house. The other half
never says a word on mDNS, so:

**SSDP** -- one multicast question, and consoles, TVs, routers, NAS boxes and
media servers all answer. This is how a PS5 and an Xbox show up at all.

**The neighbour table** -- every address the Pi has exchanged a packet with,
straight out of ``ip neigh`` / ``/proc/net/arp``. A phone that answers nothing
still had to ARP for the router, so it is in here. This is the closest thing to
"everything on the network" that does not involve scanning ports on machines
that are not ours.

**On vendor names.** A MAC's first three bytes identify the manufacturer, but
only against IEEE's registry -- a file we do not ship. We read whichever copy the
machine already has (``ieee-data``, ``arp-scan``, ``nmap``); with none of them,
devices come back without a vendor and the interface offers installing one. An
invented vendor name is worse than a blank: "Sony Interactive" next to your
neighbour's fridge is a bug you cannot even see.

Everything here is passive or single-packet. There is no port sweep of the
subnet: it is slow, it lights up other people's security software, and on a
shared network it is not ours to run.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from project_os.core.discovery import (
    AUDIO_OUT,
    AUTOMATION_HUB,
    KIND_CAPABILITIES,
    Observation,
    VIDEO_OUT,
    WEB_UI,
    clean_name,
    identity_token,
    order_capabilities,
)

log = logging.getLogger(__name__)

SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
SSDP_LISTEN_SECONDS = 4.0
SSDP_MX = 2

#: One M-SEARCH per target. ``ssdp:all`` alone misses devices that only answer
#: their own type, and the root-device query is the one a PS5 replies to.
SSDP_TARGETS = (
    "ssdp:all",
    "upnp:rootdevice",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:device:MediaServer:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
)

NEIGHBOUR_FILE = "/proc/net/arp"
#: Wherever a copy of the IEEE registry might already be on this machine.
OUI_FILES = (
    "/usr/share/ieee-data/oui.txt",
    "/var/lib/ieee-data/oui.txt",
    "/usr/share/arp-scan/ieee-oui.txt",
    "/usr/share/nmap/nmap-mac-prefixes",
    "/usr/share/hwdata/oui.txt",
)
OUI_HINT = "sudo apt install ieee-data"

COMMAND_TIMEOUT = 4.0
REVERSE_DNS_TIMEOUT = 0.4
#: Lookups in flight at once. Enough to cover a full house in one timeout,
#: small enough that a Pi 3 is not running sixteen resolver threads.
REVERSE_DNS_WORKERS = 8

# --------------------------------------------------------------------- kinds
GAME_CONSOLE = "game_console"
PHONE = "phone"
PRINTER_3D = "printer_3d"
NAS = "nas"
ROUTER = "router"
MEDIA_SERVER = "media_server"

#: What a SERVER/ST/USN header has to contain for us to call it something.
#: Ordered: the first match wins, so put the specific strings above the vague
#: ones ("playstation" before "media renderer").
SSDP_SIGNATURES = (
    ("xbox", GAME_CONSOLE, "Xbox"),
    ("playstation", GAME_CONSOLE, "PlayStation"),
    ("ps5", GAME_CONSOLE, "PlayStation 5"),
    ("ps4", GAME_CONSOLE, "PlayStation 4"),
    ("nintendo", GAME_CONSOLE, "Nintendo"),
    ("steam", GAME_CONSOLE, "Steam"),
    ("octoprint", PRINTER_3D, "OctoPrint"),
    ("moonraker", PRINTER_3D, "Klipper/Moonraker"),
    ("klipper", PRINTER_3D, "Klipper"),
    ("bambu", PRINTER_3D, "Bambu Lab"),
    ("prusa", PRINTER_3D, "Prusa"),
    ("creality", PRINTER_3D, "Creality"),
    ("jellyfin", MEDIA_SERVER, "Jellyfin"),
    ("plex", MEDIA_SERVER, "Plex"),
    ("emby", MEDIA_SERVER, "Emby"),
    ("kodi", MEDIA_SERVER, "Kodi"),
    ("synology", NAS, "Synology"),
    ("qnap", NAS, "QNAP"),
    ("truenas", NAS, "TrueNAS"),
    ("freenas", NAS, "TrueNAS"),
    ("openmediavault", NAS, "OpenMediaVault"),
    ("internetgatewaydevice", ROUTER, "Roteador"),
    ("openwrt", ROUTER, "OpenWrt"),
    ("asuswrt", ROUTER, "ASUS"),
    ("mikrotik", ROUTER, "MikroTik"),
    ("roku", "tv", "Roku"),
    ("bravia", "tv", "Sony Bravia"),
    ("webos", "tv", "LG webOS"),
    ("tizen", "tv", "Samsung Tizen"),
    ("aquos", "tv", "Sharp"),
    ("philips tv", "tv", "Philips"),
    ("sonos", "speaker", "Sonos"),
    ("chromecast", "chromecast", "Chromecast"),
    ("mediarenderer", "tv", ""),
    ("mediaserver", MEDIA_SERVER, ""),
)

#: Capabilities for the kinds this module introduces. discovery.py owns the rest.
EXTRA_KIND_CAPABILITIES = {
    GAME_CONSOLE: (AUDIO_OUT, VIDEO_OUT),
    PHONE: (),
    PRINTER_3D: (WEB_UI,),
    NAS: (WEB_UI,),
    ROUTER: (WEB_UI,),
    MEDIA_SERVER: (AUDIO_OUT, VIDEO_OUT, WEB_UI),
}

_HEADER_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_oui_cache = None  # type: Optional[Dict[str, str]]


# ---------------------------------------------------------------------------
# SSDP
# ---------------------------------------------------------------------------
def _search_payload(target: str) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: %s:%d\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: %d\r\n"
        "ST: %s\r\n"
        "\r\n" % (SSDP_ADDRESS, SSDP_PORT, SSDP_MX, target)
    ).encode("ascii")


def parse_ssdp(payload: bytes) -> Dict[str, str]:
    """Headers of one SSDP reply, lower-cased keys.

    Split out so the classifier can be tested without a network.
    """
    text = payload.decode("utf-8", "replace")
    headers = {}  # type: Dict[str, str]
    for line in text.splitlines()[1:]:
        match = _HEADER_RE.match(line.strip())
        if match:
            headers[match.group(1).lower()] = match.group(2).strip()
    return headers


def classify_ssdp(headers: Dict[str, str]) -> Tuple[str, str]:
    """``(kind, vendor)`` from an SSDP reply's headers.

    Falls back to ``web_service`` rather than ``unknown``: everything that
    answers SSDP has an HTTP description document, so there is always at least a
    page to open, and "unknown" would hide that.
    """
    blob = " ".join(
        headers.get(key, "") for key in ("server", "st", "usn", "nt", "location", "x-user-agent")
    ).lower()
    for needle, kind, vendor in SSDP_SIGNATURES:
        if needle in blob:
            return kind, vendor
    return "web_service", ""


def _location_port(location: str) -> int:
    match = re.search(r"://[^/]*?:(\d+)", location or "")
    return int(match.group(1)) if match else 0


def scan_ssdp(timeout: float = SSDP_LISTEN_SECONDS) -> List[Observation]:
    """Ask the subnet who is there, over UPnP.

    One socket, several questions, and whatever answers within ``timeout``. The
    replies are unicast back to us, so this works without joining the multicast
    group.
    """
    found = {}  # type: Dict[str, Observation]
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        for target in SSDP_TARGETS:
            try:
                sock.sendto(_search_payload(target), (SSDP_ADDRESS, SSDP_PORT))
            except OSError as exc:
                log.debug("SSDP search for %s failed: %s", target, exc)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:  # pragma: no cover - interface went away
                break
            observation = _ssdp_observation(data, addr[0])
            if observation is None:
                continue
            # One box answers several times, once per target it matches. Keep the
            # most specific answer rather than whichever arrived last.
            existing = found.get(observation.address)
            if existing is None or (existing.kind == "web_service" and
                                    observation.kind != "web_service"):
                found[observation.address] = observation
    except OSError as exc:
        log.debug("SSDP unavailable: %s", exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass
    return list(found.values())


def _ssdp_observation(payload: bytes, address: str) -> Optional[Observation]:
    headers = parse_ssdp(payload)
    if not headers:
        return None
    kind, vendor = classify_ssdp(headers)
    usn = headers.get("usn", "")
    uuid_match = re.search(r"uuid:([0-9a-fA-F-]{8,})", usn)
    token = identity_token(uuid_match.group(1)) if uuid_match else None
    name = _ssdp_name(headers, vendor, address)
    capabilities = set(EXTRA_KIND_CAPABILITIES.get(kind, ())) or set(
        KIND_CAPABILITIES.get(kind, ())
    )
    return Observation(
        source="ssdp",
        kind=kind,
        instance=name,
        name_hint=name,
        address=address,
        port=_location_port(headers.get("location", "")),
        properties={
            "vendor": vendor,
            "server": headers.get("server", ""),
            "location": headers.get("location", ""),
            "st": headers.get("st", ""),
        },
        capabilities=order_capabilities(capabilities),
        strong_keys=[token] if token else [],
        weak_keys=["addr:" + address],
    )


def _ssdp_name(headers: Dict[str, str], vendor: str, address: str) -> str:
    """SSDP has no name field, so build the least-bad one we can.

    The full description document *does* carry a friendly name, but fetching it
    means an HTTP request per device on every scan, and a device that has just
    told us it is a PlayStation is already useful enough to list.
    """
    if vendor:
        return vendor
    server = headers.get("server", "")
    parts = [piece for piece in re.split(r"[,/]", server) if piece.strip()]
    for piece in parts:
        piece = piece.strip()
        if piece and not re.match(r"^(upnp|http)", piece, re.I) and not piece[0].isdigit():
            return clean_name(piece)
    return address


# ---------------------------------------------------------------------------
# the neighbour table
# ---------------------------------------------------------------------------
def _read_proc_arp() -> List[Tuple[str, str]]:
    """``(ip, mac)`` from /proc/net/arp. Linux only, no command needed."""
    out = []  # type: List[Tuple[str, str]]
    try:
        with open(NEIGHBOUR_FILE, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        fields = line.split()
        if len(fields) < 4:
            continue
        ip, mac = fields[0], fields[3].lower()
        if _MAC_RE.match(mac) and mac != "00:00:00:00:00:00":
            out.append((ip, mac))
    return out


def _read_ip_neigh() -> List[Tuple[str, str]]:
    """Same thing via ``ip neigh``/``arp -an`` -- covers IPv6 and macOS."""
    for argv in (["ip", "-4", "neigh", "show"], ["arp", "-an"]):
        if not shutil.which(argv[0]):
            continue
        try:
            result = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=COMMAND_TIMEOUT, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        text = result.stdout.decode("utf-8", "replace")
        pairs = []  # type: List[Tuple[str, str]]
        for line in text.splitlines():
            if "failed" in line or "incomplete" in line.lower():
                continue
            ip = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
            mac = re.search(r"([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})", line)
            if not ip or not mac:
                continue
            normalised = ":".join(part.zfill(2) for part in mac.group(1).lower().split(":"))
            if normalised != "00:00:00:00:00:00":
                pairs.append((ip.group(1), normalised))
        if pairs:
            return pairs
    return []


def is_real_host(address: str) -> bool:
    """False for addresses that are not a machine you could talk to.

    The ARP table holds multicast groups too -- 224.0.0.0/4, which is how mDNS
    and SSDP work in the first place -- and the neighbour scan listed every one
    of them as a device. "232.17.191.193, unknown, online" on the Devices screen
    is noise standing exactly where a real find should be.
    """
    parts = address.split(".")
    if len(parts) != 4:
        return True  # IPv6 and anything unexpected: not ours to judge here
    try:
        octets = [int(part) for part in parts]
    except ValueError:
        return False
    if not all(0 <= octet <= 255 for octet in octets):
        return False
    if 224 <= octets[0] <= 239:          # multicast
        return False
    if octets[0] == 255 or octets == [0, 0, 0, 0]:   # broadcast / unspecified
        return False
    if octets[3] == 255:                 # subnet broadcast, /24 in practice
        return False
    if octets[0] == 169 and octets[1] == 254:        # link-local autoconf
        return False
    return True


def neighbours() -> List[Tuple[str, str]]:
    """Every ``(ip, mac)`` this machine has spoken to recently."""
    pairs = _read_proc_arp() or _read_ip_neigh()
    seen = set()  # type: Set[str]
    unique = []
    for ip, mac in pairs:
        if ip in seen or not is_real_host(ip):
            continue
        seen.add(ip)
        unique.append((ip, mac))
    return unique


def _load_oui() -> Dict[str, str]:
    """Parse whichever IEEE registry copy this machine happens to have.

    Three formats, one loop: ``AABBCC<tab>Vendor`` (nmap, arp-scan) and
    ``AA-BB-CC   (hex)		Vendor`` (ieee-data). Both reduce to six hex digits
    plus a name.
    """
    global _oui_cache
    if _oui_cache is not None:
        return _oui_cache
    table = {}  # type: Dict[str, str]
    for path in OUI_FILES:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    match = re.match(
                        r"^([0-9A-Fa-f]{2}[-:]?[0-9A-Fa-f]{2}[-:]?[0-9A-Fa-f]{2})"
                        r"\s+(?:\(hex\)\s+)?(.+)$",
                        line,
                    )
                    if not match:
                        continue
                    prefix = re.sub(r"[-:]", "", match.group(1)).lower()
                    name = match.group(2).strip()
                    if len(prefix) == 6 and name:
                        table.setdefault(prefix, name)
        except OSError:
            continue
        if table:
            log.debug("OUI table loaded from %s (%d entries)", path, len(table))
            break
    _oui_cache = table
    return table


def oui_available() -> Dict[str, Any]:
    """Whether vendor names are possible here, and how to make them possible."""
    table = _load_oui()
    return {
        "available": bool(table),
        "entries": len(table),
        "install_hint": None if table else OUI_HINT,
        "reason": None if table else (
            "Sem a base OUI da IEEE não dá pra dizer o fabricante de um MAC, e "
            "chutar seria pior que deixar em branco."
        ),
    }


def mac_privado(mac: str) -> bool:
    """Se este MAC é sorteado pelo aparelho em vez de vir de fábrica.

    O segundo bit menos significativo do primeiro byte é o "locally
    administered": ligado, o endereço não pertence a fabricante nenhum. É o que
    todo celular e todo notebook moderno faz por padrão para não ser seguido de
    rede em rede -- e a consequência aqui é direta: **nenhuma base de dados vai
    dizer o fabricante desse aparelho, nem a da IEEE**.

    Sem isto, seis dos treze aparelhos sem nome da casa dele apareciam com o
    convite de instalar a base OUI, que para eles não muda nada.
    """
    digitos = re.sub(r"[^0-9a-f]", "", (mac or "").lower())
    if len(digitos) < 2:
        return False
    try:
        primeiro = int(digitos[:2], 16)
    except ValueError:  # pragma: no cover - filtrado pelo regex acima
        return False
    return bool(primeiro & 0b10)


def vendor_of(mac: str) -> Optional[str]:
    prefix = re.sub(r"[^0-9a-f]", "", (mac or "").lower())[:6]
    if len(prefix) < 6:
        return None
    return _load_oui().get(prefix)


def _lookup(address: str) -> str:
    try:
        name = socket.gethostbyaddr(address)[0]
    except (OSError, socket.herror, socket.gaierror):
        return ""
    return clean_name(name.split(".")[0]) if name else ""


def _reverse_name(address: str) -> str:
    """Reverse DNS, briefly.

    Worth the wait: a router that knows its clients answers with
    ``Miguels-iPhone``, which beats every guess this module could make.
    """
    with _dns_timeout():
        return _lookup(address)


@contextlib.contextmanager
def _dns_timeout():
    """Cap resolver waits, once.

    ``setdefaulttimeout`` is process-wide, so it is set around the whole batch
    rather than inside each lookup -- threads setting and restoring the same
    global in turn is a race that ends with the timeout permanently applied to
    something else.
    """
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(REVERSE_DNS_TIMEOUT)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


def _reverse_names(addresses: List[str]) -> Dict[str, str]:
    """Reverse-resolve a whole list at once.

    One at a time, a house with thirty things on it spends thirty timeouts in a
    row -- twelve seconds of a scan, all of it waiting. They are independent
    lookups, so they go together.
    """
    if not addresses:
        return {}
    import concurrent.futures

    workers = min(REVERSE_DNS_WORKERS, len(addresses))
    names = {}  # type: Dict[str, str]
    with _dns_timeout():
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for address, name in zip(addresses, pool.map(_lookup, addresses)):
                if name:
                    names[address] = name
    return names


def scan_neighbours() -> List[Observation]:
    """Everything with an address on this network, named as well as we can.

    Deliberately reports ``unknown`` for most of them. A MAC and a vendor are not
    enough to tell a phone from a laptop, and a wrong label on the dashboard is
    worse than an honest "something from Apple at 192.168.0.31" that you can name
    yourself.
    """
    found = []  # type: List[Observation]
    own = set(_own_addresses())
    pairs = [(address, mac) for address, mac in neighbours() if address not in own]
    names = _reverse_names([address for address, _ in pairs])
    for address, mac in pairs:
        vendor = vendor_of(mac) or ""
        hostname = names.get(address, "")
        name = hostname or vendor or address
        token = identity_token(mac.replace(":", ""))
        found.append(
            Observation(
                source="neighbour",
                kind="unknown",
                instance=name,
                name_hint=name,
                address=address,
                port=0,
                properties={
                    "mac": mac,
                    "vendor": vendor,
                    "hostname": hostname,
                },
                capabilities=[],
                strong_keys=[token] if token else [],
                weak_keys=["addr:" + address],
                # The MAC is for staying the same row between scans, not for
                # claiming to know what this is. See Observation.anonymous.
                anonymous=True,
            )
        )
    return found


def _own_addresses() -> List[str]:
    from project_os.core import sysinfo

    try:
        return list(sysinfo.local_ips())
    except Exception:  # pragma: no cover - sysinfo degrades on odd platforms
        return []


def reset_cache() -> None:
    """Forget the parsed OUI table. Used after installing ``ieee-data``."""
    global _oui_cache
    _oui_cache = None


__all__ = [
    "GAME_CONSOLE",
    "MEDIA_SERVER",
    "NAS",
    "OUI_HINT",
    "PHONE",
    "PRINTER_3D",
    "ROUTER",
    "SSDP_SIGNATURES",
    "classify_ssdp",
    "neighbours",
    "oui_available",
    "parse_ssdp",
    "reset_cache",
    "scan_neighbours",
    "scan_ssdp",
    "vendor_of",
]
