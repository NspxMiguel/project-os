"""The app catalog, and whether each entry fits on this board.

The entries themselves live in :file:`project_os/data/catalog.yaml`, not here.
Adding an app to the store is editing a data file, which is the difference
between "someone who knows Python can extend the store" and "anyone can".

Every ``ram_mb`` in that file is measured idle RSS on a Raspberry Pi, not a
vendor figure -- the whole point of the ``fits`` calculation is to answer "will
this actually run on *my* board" before a 25-minute install, and a number copied
from a marketing page cannot answer that.

Two deliberate decisions:

**Entries that do not fit are still listed**, marked, with the reason. Frigate
needs ~1.5 GB and appears on a 1 GB Pi as too big rather than being hidden -- a
store that silently omits things teaches people it is incomplete, and they go
install it by hand anyway.

**``fits`` is advice, never a gate.** The install endpoint proceeds once the
caller acknowledges the numbers.

    "hospedar servidores (dependendo da rasp fica ruim, mas fds, usuario q
    escolhe ele q se fode)"
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from project_os.core import hardware

log = logging.getLogger(__name__)

#: Headroom the system keeps for itself: project-os, the kernel, page cache and
#: whatever else is already running. An app is offered only if it fits *under*
#: what remains after this.
RESERVED_MB = 250

CATALOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "catalog.yaml"
)

REQUIRED_FIELDS = ("id", "name", "kind", "category", "summary")
KINDS = ("builtin", "service", "container", "recipe")

_cache = None  # type: Optional[List[Dict[str, Any]]]


def _load() -> List[Dict[str, Any]]:
    """Read and validate the catalog file, once.

    A malformed entry is dropped with a log line rather than taking the store
    down: one bad block in a data file must not be the reason the box has no
    store at all.
    """
    import yaml

    try:
        with open(CATALOG_FILE, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or []
    except (OSError, yaml.YAMLError) as exc:
        log.error("catalog: could not read %s: %s", CATALOG_FILE, exc)
        return []

    if not isinstance(raw, list):
        log.error("catalog: %s must be a list of entries", CATALOG_FILE)
        return []

    out = []  # type: List[Dict[str, Any]]
    seen = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            log.error("catalog: entry #%d is not a mapping", index)
            continue
        missing = [field for field in REQUIRED_FIELDS if not entry.get(field)]
        if missing:
            log.error("catalog: entry #%d (%r) is missing %s", index,
                      entry.get("id"), ", ".join(missing))
            continue
        if entry["kind"] not in KINDS:
            log.error("catalog: %r has unknown kind %r", entry["id"], entry["kind"])
            continue
        if entry["id"] in seen:
            log.error("catalog: duplicate id %r; keeping the first", entry["id"])
            continue
        seen.add(entry["id"])
        item = dict(entry)
        item.setdefault("icon", "box")
        item.setdefault("description", entry["summary"])
        item.setdefault("tags", [])
        item.setdefault("ram_mb", 0)
        item.setdefault("disk_mb", 0)
        out.append(item)
    return out


def all_entries() -> List[Dict[str, Any]]:
    """The raw catalog, unannotated."""
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def reload() -> List[Dict[str, Any]]:
    """Re-read the file. Used by tests and by anyone editing it on a live box."""
    global _cache
    _cache = None
    return all_entries()


def budget_mb(board: Optional[Any] = None) -> int:
    """How much RAM an app may claim on this board.

    Based on kernel-visible memory (``ram_total_mb``), not the board's nominal
    size: memory the GPU took is real memory no app will ever get.
    """
    detected = board if board is not None else hardware.detect()
    visible = detected.ram_total_mb or detected.ram_mb or 0
    return max(0, visible - RESERVED_MB)


def annotate(entry: Dict[str, Any], board: Optional[Any] = None,
             budget: Optional[int] = None) -> Dict[str, Any]:
    """Add ``fits``/``fit_reason``/``tier_required`` to one catalog entry."""
    detected = board if board is not None else hardware.detect()
    allowance = budget if budget is not None else budget_mb(detected)
    item = dict(entry)
    needed = int(entry.get("ram_mb") or 0)
    item["fits"] = needed <= allowance
    if item["fits"]:
        item["fit_reason"] = ""
        if allowance and needed > allowance * 0.6:
            item["fit_reason"] = (
                "Cabe, mas ocupa boa parte do que sobra (%d MB de %d MB livres)."
                % (needed, allowance)
            )
    else:
        item["fit_reason"] = (
            "Precisa de ~%d MB e só há ~%d MB disponíveis nesta placa (%d MB no "
            "total). Dá pra instalar assim mesmo."
            % (needed, allowance, detected.ram_total_mb or detected.ram_mb)
        )
    item["tier_required"] = _tier_required(needed)
    return item


def _tier_required(ram_mb: int) -> str:
    """The smallest tier that can carry an app of this size."""
    for floor, name, _note in hardware.TIERS:
        if ram_mb + RESERVED_MB <= floor:
            return name
    return hardware.TIERS[-1][1]


def entries(board: Optional[Any] = None) -> List[Dict[str, Any]]:
    """The whole catalog, annotated for this board."""
    detected = board if board is not None else hardware.detect()
    allowance = budget_mb(detected)
    return [annotate(entry, detected, allowance) for entry in all_entries()]


def get(app_id: str, board: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    for entry in all_entries():
        if entry["id"] == app_id:
            return annotate(entry, board)
    return None


def categories() -> List[str]:
    return sorted(set(entry["category"] for entry in all_entries()))


__all__ = [
    "CATALOG_FILE",
    "RESERVED_MB",
    "all_entries",
    "annotate",
    "budget_mb",
    "categories",
    "entries",
    "get",
    "reload",
]
