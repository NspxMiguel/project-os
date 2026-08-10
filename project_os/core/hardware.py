"""What machine is this, and can project-os honestly run on it?

The rule the user gave is a memory floor, not a list of boards:

    "quero suporte a todas as raspberry pi possiveis, apenas n colocar aquelas
     com sla, 512 de ram"

So that is what this module implements. A model table exists, but only to put a
nice name on the screen -- it is never what decides whether the system runs. A
Raspberry Pi model released after this file was written works fine; an unknown
board with 4 GB is supported, and a known board with 512 MB is not.

Two traps this module exists to avoid:

**The GPU split.** A "1 GB" Pi does not report 1 GB. The VideoCore firmware
carves out its share before Linux ever sees the memory, so a Pi 3B with the
default 64 MB split reports about 948 MB in ``MemTotal``, and a board configured
for a 256 MB split reports about 760 MB. Gating on ``MemTotal >= 1024`` would
refuse to run on the exact machine this project was written for. The floor sits
at 700 MB, comfortably above what any 512 MB board can report (~430-490 MB) and
below what any 1 GB board reports.

**Assuming Linux.** Developers work on macOS. Nothing here may raise on a
machine with no ``/proc``; it degrades to "unknown board, this much RAM".
"""

from __future__ import annotations

import os
import platform
import re
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# revision code tables
# ---------------------------------------------------------------------------
# Layout of a new-style revision code (bit 23 set):
#
#     NOQuuuWuFMMMCCCCPPPPTTTTTTTTRRRR
#
#     bits 0-3    revision      bits 16-19  manufacturer
#     bits 4-11   type          bits 20-22  memory size
#     bits 12-15  processor     bit  23     new-style flag
#
# Source: raspberrypi/documentation, computers/raspberry-pi/revision-codes.adoc

MEMORY_MB = {0: 256, 1: 512, 2: 1024, 3: 2048, 4: 4096, 5: 8192, 6: 16384}

PROCESSORS = {0: "BCM2835", 1: "BCM2836", 2: "BCM2837", 3: "BCM2711", 4: "BCM2712"}

MANUFACTURERS = {
    0: "Sony UK",
    1: "Egoman",
    2: "Embest",
    3: "Sony Japan",
    4: "Embest",
    5: "Stadium",
}

BOARD_TYPES = {
    0x00: "A",
    0x01: "B",
    0x02: "A+",
    0x03: "B+",
    0x04: "2B",
    0x05: "Alpha",
    0x06: "CM1",
    0x08: "3B",
    0x09: "Zero",
    0x0A: "CM3",
    0x0C: "Zero W",
    0x0D: "3B+",
    0x0E: "3A+",
    0x10: "CM3+",
    0x11: "4B",
    0x12: "Zero 2 W",
    0x13: "400",
    0x14: "CM4",
    0x15: "CM4S",
    0x17: "5",
    0x18: "CM5",
    0x19: "500",
    0x1A: "CM5 Lite",
    0x1B: "CM0",
}

# ---------------------------------------------------------------------------
# the floor
# ---------------------------------------------------------------------------

#: Physical RAM below which project-os refuses to install. Between the most a
#: 512 MB board can report and the least a 1 GB board can.
MINIMUM_RAM_MB = 700

#: What the board unlocks. The app store reads this; nothing else branches on it.
TIERS = (
    (3500, "roomy", "Everything in the catalog fits, including Home Assistant."),
    (1800, "comfortable", "Home Assistant fits alongside a couple of services."),
    (0, "minimal", "Built-in apps and light services. Home Assistant will be tight."),
)


class Board(object):
    """A description of the machine, safe to serialise straight to the UI."""

    def __init__(self, **fields):
        self.model = fields.get("model") or "Unknown machine"
        self.revision = fields.get("revision")
        self.soc = fields.get("soc")
        self.manufacturer = fields.get("manufacturer")
        self.ram_mb = int(fields.get("ram_mb") or 0)
        self.ram_total_mb = int(fields.get("ram_total_mb") or 0)
        self.arch = fields.get("arch") or platform.machine()
        self.is_raspberry_pi = bool(fields.get("is_raspberry_pi"))
        self.supported = bool(fields.get("supported"))
        self.reason = fields.get("reason") or ""
        self.tier = fields.get("tier") or "minimal"
        self.tier_note = fields.get("tier_note") or ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "revision": self.revision,
            "soc": self.soc,
            "manufacturer": self.manufacturer,
            "ram_mb": self.ram_mb,
            "ram_total_mb": self.ram_total_mb,
            "arch": self.arch,
            "is_raspberry_pi": self.is_raspberry_pi,
            "supported": self.supported,
            "reason": self.reason,
            "tier": self.tier,
            "tier_note": self.tier_note,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<Board %s %s MB %s>" % (self.model, self.ram_mb, self.tier)


# ---------------------------------------------------------------------------
# readers -- every one of these returns None rather than raising
# ---------------------------------------------------------------------------


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as handle:
            # device-tree strings are NUL-terminated.
            return handle.read().decode("utf-8", "replace").replace("\x00", "").strip()
    except (IOError, OSError):
        return None


def read_model(proc_root: str = "/proc", dt_root: str = "/proc/device-tree") -> Optional[str]:
    """The board's own name for itself.

    This is authoritative and needs no lookup table, which is why it is tried
    first: a board that does not exist yet still names itself correctly.
    """
    model = _read(os.path.join(dt_root, "model"))
    if model:
        return model
    cpuinfo = _read(os.path.join(proc_root, "cpuinfo")) or ""
    match = re.search(r"^Model\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
    return match.group(1).strip() if match else None


def read_revision(proc_root: str = "/proc") -> Optional[str]:
    cpuinfo = _read(os.path.join(proc_root, "cpuinfo")) or ""
    match = re.search(r"^Revision\s*:\s*([0-9a-fA-F]+)$", cpuinfo, re.MULTILINE)
    return match.group(1).strip().lower() if match else None


def read_mem_total_mb(proc_root: str = "/proc") -> Optional[int]:
    """What the kernel can actually see, in MB. Not the board's physical RAM."""
    meminfo = _read(os.path.join(proc_root, "meminfo"))
    if meminfo:
        match = re.search(r"^MemTotal:\s+(\d+)\s*kB", meminfo, re.MULTILINE)
        if match:
            return int(match.group(1)) // 1024
    try:  # macOS and anything else with sysconf
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * size // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        return None


def parse_revision(revision: Optional[str]) -> Dict[str, Any]:
    """Decode a revision code. Unparseable input yields an empty dict, not an error.

    Old-style codes (bit 23 clear) are from Pi 1 and Pi Zero hardware, all of
    which is below the floor anyway, so they are reported as un-decoded rather
    than given a second lookup table that would never select a supported board.
    """
    if not revision:
        return {}
    try:
        code = int(revision, 16)
    except (TypeError, ValueError):
        return {}

    if not code & (1 << 23):  # old-style
        return {"new_style": False}

    memory_code = (code >> 20) & 0x7
    return {
        "new_style": True,
        "type_code": (code >> 4) & 0xFF,
        "board": BOARD_TYPES.get((code >> 4) & 0xFF),
        "soc": PROCESSORS.get((code >> 12) & 0xF),
        "manufacturer": MANUFACTURERS.get((code >> 16) & 0xF),
        # Code 7 means "other"; treat it as unknown rather than inventing a size.
        "ram_mb": MEMORY_MB.get(memory_code),
        "board_revision": code & 0xF,
    }


# ---------------------------------------------------------------------------
# the answer
# ---------------------------------------------------------------------------


def _tier_for(ram_mb: int):
    for floor, name, note in TIERS:
        if ram_mb >= floor:
            return name, note
    return "minimal", TIERS[-1][2]


def detect(proc_root: str = "/proc", dt_root: str = "/proc/device-tree") -> Board:
    """Identify the machine. Never raises."""
    model = read_model(proc_root, dt_root)
    revision = read_revision(proc_root)
    decoded = parse_revision(revision)
    mem_total = read_mem_total_mb(proc_root) or 0

    is_pi = bool(model and "raspberry pi" in model.lower())

    # Physical RAM: the revision code knows it exactly, because it is a property
    # of the board rather than of how the firmware divided it up. MemTotal is the
    # fallback, rounded up to the nearest common size so that a 948 MB reading
    # from a GPU-split 1 GB board is understood as the 1 GB board it is.
    ram_mb = decoded.get("ram_mb") or _round_up_to_board_size(mem_total)

    if ram_mb and ram_mb < MINIMUM_RAM_MB:
        supported, reason = False, (
            "%s has %d MB of RAM. project-os needs at least 1 GB -- below that the "
            "web interface and a single app do not fit at the same time."
            % (model or "This machine", ram_mb)
        )
    elif not ram_mb:
        supported, reason = True, "Could not measure RAM; proceeding, but watch memory use."
    else:
        supported, reason = True, ""

    # The floor asks "is this board too small to bother"; the tier asks "how much
    # can I actually run". Those are different numbers on a Pi: memory the GPU
    # took is real memory the board has and no app will ever get. So the floor
    # uses physical RAM and the tier uses what the kernel can hand out.
    tier, tier_note = _tier_for(mem_total or ram_mb)

    return Board(
        model=model or _fallback_model(),
        revision=revision,
        soc=decoded.get("soc"),
        manufacturer=decoded.get("manufacturer"),
        ram_mb=ram_mb,
        ram_total_mb=mem_total,
        arch=platform.machine(),
        is_raspberry_pi=is_pi,
        supported=supported,
        reason=reason,
        tier=tier,
        tier_note=tier_note,
    )


#: Sizes real boards ship with. Anything measured is rounded up to one of these.
BOARD_SIZES = (256, 512, 1024, 2048, 4096, 8192, 16384)


def _round_up_to_board_size(mem_total_mb: int) -> int:
    """Undo the GPU split.

    A reading is attributed to the smallest real board size it fills at least
    70% of: 948 -> 1024, 760 -> 1024, 490 -> 512, 430 -> 512.

    70% is the loosest ratio that is still safe. The biggest split anyone
    sensibly configures is 256 MB, which leaves a 1 GB board reporting ~75%, and
    no 512 MB board can report more than 512 -- so nothing below the floor can
    be rounded up across it.
    """
    if not mem_total_mb:
        return 0
    for size in BOARD_SIZES:
        if mem_total_mb <= size and mem_total_mb >= size * 0.70:
            return size
    # Larger than any known board size, or an odd VM allocation: report as-is.
    return mem_total_mb


def _fallback_model() -> str:
    system = platform.system()
    if system == "Darwin":
        return "Mac (development machine)"
    return "%s %s" % (system or "Unknown", platform.machine())


def summary() -> Dict[str, Any]:
    """What ``GET /api/system/hardware`` returns."""
    return detect().as_dict()
