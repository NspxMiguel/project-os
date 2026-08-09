"""What the machine is, and what it is doing right now.

Three rules shape this module.

**psutil is optional.** It is the obvious dependency and it is genuinely better,
but it needs a compiler, and a Pi that cannot install it must still show a
dashboard. So every reading has a ``/proc`` fallback and the payload shape is
identical either way -- the frontend never learns which path produced it. The
import happens inside :func:`_psutil`, never at module level, so importing this
module costs nothing.

**Nothing here raises.** This feeds a five-second polling loop and a status page.
A sensor that disappears (no thermal zone, no swap, an unplugged USB disk) is
worth a ``None``, never a traceback that blanks the whole dashboard. ``None``
means "not measurable here" and the UI renders it as ``--``; no field is ever
filled with a plausible-looking zero.

**It has to be cheap.** The target is a Pi 3B with 1 GB of RAM polling every five
seconds. So: static facts are computed once and cached, percentages are measured
against the previous sample instead of sleeping, and anything that costs a
process (``vcgencmd``) is behind a TTL.
"""

from __future__ import annotations

import copy
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

#: Shown by the API when a reading is missing only because psutil is absent.
PSUTIL_HINT = "pip install psutil"

#: Where the SoC temperature lives, most specific first.
THERMAL_PATHS = (
    "/sys/class/thermal/thermal_zone0/temp",
    "/sys/devices/virtual/thermal/thermal_zone0/temp",
)

#: Sensor labels worth preferring when a box exposes several thermal zones.
CPU_SENSOR_HINTS = ("cpu_thermal", "cpu-thermal", "coretemp", "k10temp", "soc", "cpu")

#: Filesystems that are not storage and must never appear as a disk.
PSEUDO_FILESYSTEMS = frozenset(
    (
        "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs", "debugfs",
        "devfs", "devpts", "devtmpfs", "efivarfs", "fuse.gvfsd-fuse", "fusectl",
        "hugetlbfs", "mqueue", "nsfs", "overlay", "proc", "pstore", "ramfs",
        "securityfs", "squashfs", "sysfs", "tmpfs", "tracefs",
    )
)

#: Mount points that belong to the OS plumbing rather than to the user's storage.
PSEUDO_MOUNT_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/snapd/snap", "/var/snap",
    "/System/Volumes/VM", "/System/Volumes/Preboot", "/System/Volumes/Update",
    "/System/Volumes/xarts", "/System/Volumes/iSCPreboot", "/System/Volumes/Hardware",
)

#: A dashboard that lists 200 filesystems is not a dashboard.
MAX_DISKS = 32

#: ``vcgencmd`` costs a fork; the flags it reports change slowly.
THROTTLED_TTL_SECONDS = 30.0
VCGENCMD_TIMEOUT = 3.0

#: bit -> human flag, from the Raspberry Pi firmware documentation.
THROTTLE_BITS = (
    (0, "undervoltage_now", "Under-voltage detected"),
    (1, "freq_capped_now", "Arm frequency capped"),
    (2, "throttled_now", "Currently throttled"),
    (3, "soft_temp_limit_now", "Soft temperature limit active"),
    (16, "undervoltage_since_boot", "Under-voltage has occurred since boot"),
    (17, "freq_capped_since_boot", "Arm frequency capping has occurred since boot"),
    (18, "throttled_since_boot", "Throttling has occurred since boot"),
    (19, "soft_temp_limit_since_boot", "Soft temperature limit has occurred since boot"),
)

_HAS_PROC = os.path.isdir("/proc")

_lock = threading.RLock()
_psutil_module = None  # type: Any  # module, False once we know it is absent
_host_info = None  # type: Optional[Dict[str, Any]]
_boot_epoch = None  # type: Any  # float, or False once we know we cannot tell
_cpu_sample = None  # type: Optional[Dict[str, Any]]
_cpu_primed = False
_net_sample = None  # type: Optional[Dict[str, Any]]
_proc_cpu_sample = None  # type: Optional[Dict[str, Any]]
_throttled_cache = None  # type: Optional[Tuple[float, Dict[str, Any]]]
_which_cache = {}  # type: Dict[str, Optional[str]]
_process_start = time.time()


# ---------------------------------------------------------------------------
# tiny helpers -- none of these raise
# ---------------------------------------------------------------------------
def _psutil() -> Any:
    """The psutil module, or ``None``. Imported on first use, never at import time."""
    global _psutil_module
    if _psutil_module is None:
        with _lock:
            if _psutil_module is None:
                try:
                    import psutil  # type: ignore
                except Exception:  # pragma: no cover - depends on the machine
                    _psutil_module = False
                else:
                    _psutil_module = psutil
    return _psutil_module or None


def have_psutil() -> bool:
    return _psutil() is not None


def _which(name: str) -> Optional[str]:
    """``shutil.which`` with a cache -- it walks $PATH on every call."""
    if name not in _which_cache:
        try:
            _which_cache[name] = shutil.which(name)
        except Exception:  # pragma: no cover - exotic PATH
            _which_cache[name] = None
    return _which_cache[name]


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except (IOError, OSError):
        return None


def _read(path: str) -> Optional[str]:
    """Text of a file, NULs stripped (device-tree strings are NUL terminated)."""
    raw = _read_bytes(path)
    if raw is None:
        return None
    return raw.decode("utf-8", "replace").replace("\x00", "")


def _run(argv: List[str], timeout: float) -> Optional[str]:
    """A short-lived helper process. ``None`` on any failure at all."""
    try:
        output = subprocess.check_output(argv, stderr=subprocess.DEVNULL, timeout=timeout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    return output.decode("utf-8", "replace")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso(epoch: float) -> str:
    stamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _pct(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    if not whole or part is None:
        return None
    return round(max(0.0, float(part)) * 100.0 / float(whole), 1)


def _rate(before: Optional[int], after: Optional[int], seconds: float) -> Optional[float]:
    """Bytes per second between two counters, or ``None`` if it makes no sense.

    Counters reset when an interface is reconfigured; a negative rate would be a
    lie, so it becomes ``None`` for that one sample.
    """
    if before is None or after is None or seconds <= 0 or after < before:
        return None
    return round((after - before) / seconds, 1)


def reset_cache() -> None:
    """Forget every cached reading. For tests and for a config reload."""
    global _host_info, _boot_epoch, _cpu_sample, _cpu_primed, _net_sample
    global _proc_cpu_sample, _throttled_cache
    with _lock:
        _host_info = None
        _boot_epoch = None
        _cpu_sample = None
        _cpu_primed = False
        _net_sample = None
        _proc_cpu_sample = None
        _throttled_cache = None
        _which_cache.clear()


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------
def os_release() -> Dict[str, str]:
    """``/etc/os-release`` as a lower-cased dict. Empty off Linux."""
    raw = _read("/etc/os-release") or _read("/usr/lib/os-release") or ""
    out = {}  # type: Dict[str, str]
    for line in raw.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip().lower()] = value.strip().strip('"').strip("'")
    return out


def os_name() -> str:
    """The prettiest name this OS will give us."""
    release = os_release()
    pretty = release.get("pretty_name") or release.get("name")
    if pretty:
        version = release.get("version")
        if version and version not in pretty:
            return "%s (%s)" % (pretty, version)
        return pretty
    system = platform.system()
    if system == "Darwin":
        return "macOS %s" % (platform.mac_ver()[0] or platform.release())
    return platform.platform()


def board_model() -> Optional[str]:
    """The board's own name for itself, from the device tree.

    Authoritative and needs no lookup table: a Pi model released after this file
    was written still names itself correctly.
    """
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        model = (_read(path) or "").strip()
        if model:
            return model
    cpuinfo = _read("/proc/cpuinfo") or ""
    match = re.search(r"^Model\s*:\s*(.+)$", cpuinfo, re.MULTILINE)
    return match.group(1).strip() if match else None


def is_raspberry_pi() -> bool:
    return "raspberry pi" in (board_model() or "").lower()


def cpu_model() -> Optional[str]:
    """A human name for the processor."""
    cpuinfo = _read("/proc/cpuinfo") or ""
    for field in ("model name", "Model name", "Hardware", "Processor", "cpu model"):
        match = re.search(r"^%s\s*:\s*(.+)$" % re.escape(field), cpuinfo, re.MULTILINE)
        if match:
            return match.group(1).strip()
    if platform.system() == "Darwin" and _which("sysctl"):
        brand = _run(["sysctl", "-n", "machdep.cpu.brand_string"], 2.0)
        if brand and brand.strip():
            return brand.strip()
    return platform.processor() or None


def cpu_count() -> Optional[int]:
    count = os.cpu_count()
    if count:
        return int(count)
    psutil = _psutil()
    if psutil is not None:  # pragma: no cover - os.cpu_count() practically always works
        try:
            return int(psutil.cpu_count(logical=True) or 0) or None
        except Exception:
            return None
    return None


def memory_total() -> Optional[int]:
    """Physical RAM the kernel can hand out, in bytes."""
    info = _meminfo()
    if info.get("MemTotal"):
        return info["MemTotal"]
    psutil = _psutil()
    if psutil is not None:
        try:
            return int(psutil.virtual_memory().total)
        except Exception:  # pragma: no cover
            pass
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):  # pragma: no cover - Windows
        return None


def _usable_ip(address: str) -> bool:
    if not address:
        return False
    lowered = address.lower()
    if lowered.startswith(("127.", "169.254.", "fe80:", "::1", "0.0.0.0")):
        return False
    return lowered not in ("::", "0:0:0:0:0:0:0:1")


def _primary_ip() -> Optional[str]:
    """The address the box would use to reach the LAN.

    Connecting a UDP socket sends no packet; it only asks the routing table
    which source address applies. 192.0.2.0/24 is the reserved documentation
    range, so this works with no network at all.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass


def local_ips() -> List[str]:
    """LAN addresses, IPv4 first. Loopback and link-local are dropped."""
    found = []  # type: List[str]
    psutil = _psutil()
    if psutil is not None:
        try:
            for addresses in (psutil.net_if_addrs() or {}).values():
                for entry in addresses:
                    if entry.family not in (socket.AF_INET, getattr(socket, "AF_INET6", -1)):
                        continue
                    address = str(entry.address or "").split("%")[0]
                    if _usable_ip(address):
                        found.append(address)
        except Exception:  # pragma: no cover
            pass
    if not found:
        primary = _primary_ip()
        if primary:
            found.append(primary)
    ordered = []  # type: List[str]
    for address in sorted(found, key=lambda a: (":" in a, a)):
        if address not in ordered:
            ordered.append(address)
    return ordered


def boot_time() -> Optional[float]:
    """Unix timestamp of the last boot. Measured once, then cached."""
    global _boot_epoch
    if _boot_epoch is not None:
        return _boot_epoch or None
    epoch = None  # type: Optional[float]
    uptime = _uptime_from_proc()
    if uptime is not None:
        epoch = time.time() - uptime
    if epoch is None:
        psutil = _psutil()
        if psutil is not None:
            try:
                epoch = float(psutil.boot_time())
            except Exception:  # pragma: no cover
                epoch = None
    if epoch is None and platform.system() == "Darwin" and _which("sysctl"):
        raw = _run(["sysctl", "-n", "kern.boottime"], 2.0) or ""
        match = re.search(r"sec\s*=\s*(\d+)", raw)
        if match:
            epoch = float(match.group(1))
    with _lock:
        _boot_epoch = epoch if epoch is not None else False
    return epoch


def _uptime_from_proc() -> Optional[float]:
    raw = _read("/proc/uptime")
    if not raw:
        return None
    try:
        return round(float(raw.split()[0]), 1)
    except (ValueError, IndexError):  # pragma: no cover - corrupt /proc
        return None


def uptime_seconds() -> Optional[float]:
    """How long the machine has been up."""
    from_proc = _uptime_from_proc()
    if from_proc is not None:
        return from_proc
    epoch = boot_time()
    if epoch is None:
        return None
    return round(max(0.0, time.time() - epoch), 1)


def _hardware_board() -> Optional[Dict[str, Any]]:
    """``core.hardware``'s view of the board, when that module is importable.

    Imported defensively: hardware detection is a nice-to-have on the system
    page, and a problem in there must not cost us the whole payload.
    """
    try:
        from projectos.core import hardware

        return hardware.detect().as_dict()
    except Exception:  # pragma: no cover - only when hardware.py is broken
        log.debug("hardware detection unavailable", exc_info=True)
        return None


def host_info() -> Dict[str, Any]:
    """Everything that does not change while the process runs. Cached.

    Rebuilt only by :func:`reset_cache`; the expensive parts (an interface walk,
    a ``sysctl`` on macOS) therefore happen once per boot of ProjectOS rather
    than once per page load.
    """
    global _host_info
    cached = _host_info
    if cached is None:
        cached = _build_host_info()
        with _lock:
            _host_info = cached
    payload = copy.deepcopy(cached)
    # The two live values on an otherwise static payload: the page shows uptime,
    # and uptime obviously must not be frozen at process start.
    payload["uptime_seconds"] = uptime_seconds()
    payload["process_uptime_seconds"] = round(time.time() - _process_start, 1)
    return payload


def _build_host_info() -> Dict[str, Any]:
    try:
        from projectos import __version__ as version
    except Exception:  # pragma: no cover - only if the package is broken
        version = "unknown"

    release = os_release()
    model = board_model()
    epoch = boot_time()
    total_ram = memory_total()
    return {
        "hostname": socket.gethostname(),
        "os": {
            "name": os_name(),
            "id": release.get("id", "") or platform.system().lower(),
            "version": release.get("version_id", "") or platform.release(),
            "system": platform.system(),
        },
        "kernel": platform.release(),
        "arch": platform.machine(),
        "model": model,
        "is_raspberry_pi": bool(model and "raspberry pi" in model.lower()),
        "board": _hardware_board(),
        "cpu": {
            "model": cpu_model(),
            "cores": cpu_count(),
            "arch": platform.machine(),
        },
        "memory_total": total_ram,
        "python": platform.python_version(),
        "version": version,
        "boot_time": _iso(epoch) if epoch is not None else None,
        "ips": local_ips(),
        "psutil": have_psutil(),
        "psutil_hint": None if have_psutil() else PSUTIL_HINT,
    }


#: Older callers (and the system view's first paint) know this name.
info = host_info


# ---------------------------------------------------------------------------
# cpu
# ---------------------------------------------------------------------------
def _proc_stat_sample() -> Optional[Dict[str, Any]]:
    """Aggregate and per-core jiffies from ``/proc/stat``."""
    raw = _read("/proc/stat")
    if not raw:
        return None
    total = None  # type: Optional[List[int]]
    cores = []  # type: List[List[int]]
    for line in raw.splitlines():
        if not line.startswith("cpu"):
            break  # the cpu lines come first; stop before intr/ctxt/…
        parts = line.split()
        try:
            values = [int(value) for value in parts[1:]]
        except ValueError:  # pragma: no cover - corrupt /proc
            continue
        if parts[0] == "cpu":
            total = values
        else:
            cores.append(values)
    if total is None:
        return None
    return {"total": total, "cores": cores}


def _busy_percent(before: List[int], after: List[int]) -> Optional[float]:
    """Busy share between two jiffy samples. Field 3 is idle, field 4 is iowait."""
    if not before or not after:
        return None
    span = sum(after) - sum(before)
    if span <= 0:
        return None
    idle = sum(after[3:5]) - sum(before[3:5])
    return round(max(0.0, min(100.0, (span - idle) * 100.0 / span)), 1)


def cpu_percent() -> Dict[str, Any]:
    """CPU load overall and per core, measured against the previous call.

    Never sleeps. ``psutil.cpu_percent(interval=0.1)`` would block a worker
    thread for a tenth of a second on every tick, which on a Pi 3B is a
    measurable slice of the thing being measured. The first call has nothing to
    compare against and honestly reports ``None``; from the second tick on (five
    seconds later) the numbers are real.
    """
    global _cpu_sample, _cpu_primed
    result = {
        "percent": None,
        "per_core": [],
        "count": cpu_count(),
        "freq_mhz": cpu_freq_mhz(),
    }  # type: Dict[str, Any]

    psutil = _psutil()
    if psutil is not None:
        try:
            overall = psutil.cpu_percent(interval=None)
            per_core = psutil.cpu_percent(interval=None, percpu=True) or []
            with _lock:
                primed = _cpu_primed
                _cpu_primed = True
            if primed:
                result["percent"] = round(float(overall), 1)
                result["per_core"] = [round(float(value), 1) for value in per_core]
            return result
        except Exception:  # pragma: no cover - psutil failing mid-flight
            log.debug("psutil cpu_percent failed, falling back to /proc", exc_info=True)

    sample = _proc_stat_sample()
    if sample is None:
        return result
    with _lock:
        previous = _cpu_sample
        _cpu_sample = sample
    if previous is None:
        return result
    result["percent"] = _busy_percent(previous["total"], sample["total"])
    per_core = []  # type: List[float]
    for index, core in enumerate(sample["cores"]):
        if index >= len(previous["cores"]):
            break
        value = _busy_percent(previous["cores"][index], core)
        per_core.append(value if value is not None else 0.0)
    result["per_core"] = per_core
    return result


def cpu_freq_mhz() -> Optional[int]:
    raw = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if raw:
        try:
            return int(int(raw.strip()) / 1000)
        except ValueError:  # pragma: no cover
            pass
    psutil = _psutil()
    if psutil is not None:
        try:
            freq = psutil.cpu_freq()
            return int(freq.current) if freq and freq.current else None
        except Exception:  # macOS on Apple silicon has no cpu_freq
            return None
    return None


def load_average() -> Optional[List[float]]:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except (OSError, AttributeError):  # pragma: no cover - Windows
        return None


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------
def _meminfo() -> Dict[str, int]:
    """``/proc/meminfo`` in bytes. Empty off Linux."""
    raw = _read("/proc/meminfo")
    if not raw:
        return {}
    out = {}  # type: Dict[str, int]
    for line in raw.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)(?:\s+kB)?", line)
        if match:
            out[match.group(1)] = int(match.group(2)) * 1024
    return out


def memory() -> Dict[str, Any]:
    """RAM and swap in bytes.

    ``available`` -- not ``free`` -- decides whether another app fits: Linux
    fills spare RAM with cache and hands it back on demand, so ``free`` on a
    healthy Pi looks alarming and means nothing.
    """
    empty = {
        "total": None, "used": None, "available": None, "free": None,
        "cached": None, "percent": None,
        "swap": {"total": None, "used": None, "free": None, "percent": None},
    }  # type: Dict[str, Any]

    psutil = _psutil()
    if psutil is not None:
        try:
            virtual = psutil.virtual_memory()
            swap = psutil.swap_memory()
            return {
                "total": int(virtual.total),
                "used": int(virtual.total - virtual.available),
                "available": int(virtual.available),
                "free": int(getattr(virtual, "free", 0)),
                "cached": int(getattr(virtual, "cached", 0) or 0),
                "percent": round(float(virtual.percent), 1),
                "swap": {
                    "total": int(swap.total),
                    "used": int(swap.used),
                    "free": int(swap.free),
                    "percent": round(float(swap.percent), 1),
                },
            }
        except Exception:  # pragma: no cover
            log.debug("psutil memory read failed, falling back to /proc", exc_info=True)

    info = _meminfo()
    if info:
        total = info.get("MemTotal")
        available = info.get("MemAvailable", info.get("MemFree"))
        used = max(0, total - available) if (total and available is not None) else None
        swap_total = info.get("SwapTotal")
        swap_free = info.get("SwapFree")
        swap_used = (
            max(0, swap_total - swap_free)
            if (swap_total is not None and swap_free is not None)
            else None
        )
        return {
            "total": total,
            "used": used,
            "available": available,
            "free": info.get("MemFree"),
            "cached": info.get("Cached"),
            "percent": _pct(used, total),
            "swap": {
                "total": swap_total,
                "used": swap_used,
                "free": swap_free,
                "percent": _pct(swap_used, swap_total),
            },
        }

    total = memory_total()
    if total:
        empty["total"] = total
    return empty


# ---------------------------------------------------------------------------
# disks
# ---------------------------------------------------------------------------
def _is_pseudo_mount(mountpoint: str) -> bool:
    for prefix in PSEUDO_MOUNT_PREFIXES:
        if mountpoint == prefix or mountpoint.startswith(prefix + "/"):
            return True
    return False


def _partitions() -> List[Dict[str, str]]:
    """Mounted filesystems worth showing: real storage, no snaps, no loops."""
    entries = []  # type: List[Dict[str, str]]
    psutil = _psutil()
    if psutil is not None:
        try:
            for part in psutil.disk_partitions(all=False):
                entries.append(
                    {
                        "device": part.device or "",
                        "mountpoint": part.mountpoint or "",
                        "fstype": part.fstype or "",
                    }
                )
        except Exception:  # pragma: no cover
            entries = []
    if not entries:
        raw = _read("/proc/mounts") or _read("/etc/mtab")
        if raw:
            for line in raw.splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                entries.append(
                    {
                        "device": parts[0],
                        "mountpoint": parts[1].replace("\\040", " "),
                        "fstype": parts[2],
                    }
                )
    if not entries:
        # Not Linux and no psutil: at least report the filesystem we live on.
        entries = [{"device": "", "mountpoint": os.path.abspath(os.sep), "fstype": ""}]

    seen = set()  # type: set
    out = []  # type: List[Dict[str, str]]
    for entry in entries:
        mountpoint = entry["mountpoint"]
        if not mountpoint or mountpoint in seen:
            continue
        if entry["fstype"] in PSEUDO_FILESYSTEMS or _is_pseudo_mount(mountpoint):
            continue
        if entry["device"].startswith("/dev/loop"):
            continue
        seen.add(mountpoint)
        out.append(entry)
        if len(out) >= MAX_DISKS:
            break
    return out


def disks() -> List[Dict[str, Any]]:
    """Usage per mounted partition. A device that vanished mid-scan is skipped."""
    out = []  # type: List[Dict[str, Any]]
    for entry in _partitions():
        try:
            usage = shutil.disk_usage(entry["mountpoint"])
        except (OSError, ValueError):
            # A disconnected network mount, or a directory we may not stat.
            continue
        if not usage.total:
            continue
        out.append(
            {
                "device": entry["device"],
                # Both spellings: the contract says `mount`, the frontend and
                # psutil both say `mountpoint`.
                "mount": entry["mountpoint"],
                "mountpoint": entry["mountpoint"],
                "fs": entry["fstype"],
                "fstype": entry["fstype"],
                "total": int(usage.total),
                "used": int(usage.used),
                "free": int(usage.free),
                "percent": _pct(usage.used, usage.total),
            }
        )
    return out


# ---------------------------------------------------------------------------
# temperature and Pi power state
# ---------------------------------------------------------------------------
def _sensor_temperature() -> Optional[float]:
    psutil = _psutil()
    if psutil is None or not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        sensors = psutil.sensors_temperatures() or {}
    except Exception:  # pragma: no cover - platform without the syscall
        return None
    best = None  # type: Optional[float]
    for name, entries in sensors.items():
        preferred = any(hint in name.lower() for hint in CPU_SENSOR_HINTS)
        for entry in entries:
            current = getattr(entry, "current", None)
            if not current:
                continue
            if preferred:
                return round(float(current), 1)
            if best is None:
                best = round(float(current), 1)
    return best


def temperature_c() -> Optional[float]:
    """SoC temperature in Celsius, or ``None`` where nothing measures it.

    Order matters: psutil reads the same sysfs files but knows about more
    sensors, the thermal zone is the universal Linux fallback, and ``vcgencmd``
    (a fork) is the last resort because it is the expensive one.
    """
    value = _sensor_temperature()
    if value is not None:
        return value
    for path in THERMAL_PATHS:
        raw = _read(path)
        if not raw:
            continue
        try:
            reading = float(raw.strip())
        except ValueError:  # pragma: no cover
            continue
        # Millidegrees on every Pi kernel; degrees on a few odd drivers.
        return round(reading / 1000.0 if reading > 200 else reading, 1)
    if _which("vcgencmd"):
        raw = _run(["vcgencmd", "measure_temp"], VCGENCMD_TIMEOUT) or ""
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw)
        if match:
            return round(float(match.group(1)), 1)
    return None


def throttled(max_age: float = THROTTLED_TTL_SECONDS) -> Dict[str, Any]:
    """Under-voltage and thermal throttling, straight from the VideoCore.

    Worth surfacing because a weak USB power supply is the most common cause of
    a Pi behaving strangely, and it looks exactly like a software bug until
    someone reads this flag. Cached: it costs a fork and it changes slowly.
    """
    global _throttled_cache
    now = time.time()
    cached = _throttled_cache
    if cached is not None and now - cached[0] < max_age:
        return copy.deepcopy(cached[1])

    result = {"available": False, "raw": None, "flags": []}  # type: Dict[str, Any]
    for _, key, _label in THROTTLE_BITS:
        result[key] = False

    binary = _which("vcgencmd")
    if binary:
        raw = _run([binary, "get_throttled"], VCGENCMD_TIMEOUT) or ""
        match = re.search(r"0x([0-9a-fA-F]+)", raw)
        if match:
            bits = int(match.group(1), 16)
            result["available"] = True
            result["raw"] = "0x%x" % bits
            for bit, key, label in THROTTLE_BITS:
                flagged = bool(bits & (1 << bit))
                result[key] = flagged
                if flagged:
                    result["flags"].append(label)
        else:
            result["reason"] = "vcgencmd returned nothing we could parse"
    else:
        result["reason"] = "vcgencmd is not installed (this is not a Raspberry Pi)"

    with _lock:
        _throttled_cache = (now, copy.deepcopy(result))
    return result


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------
def _net_counters() -> Dict[str, Dict[str, int]]:
    psutil = _psutil()
    if psutil is not None:
        try:
            counters = psutil.net_io_counters(pernic=True) or {}
            return dict(
                (
                    name,
                    {
                        "rx_bytes": int(value.bytes_recv),
                        "tx_bytes": int(value.bytes_sent),
                        "rx_packets": int(value.packets_recv),
                        "tx_packets": int(value.packets_sent),
                    },
                )
                for name, value in counters.items()
                if name != "lo" and not name.startswith("lo0")
            )
        except Exception:  # pragma: no cover
            log.debug("psutil net counters failed, falling back to /proc", exc_info=True)

    raw = _read("/proc/net/dev")
    out = {}  # type: Dict[str, Dict[str, int]]
    if not raw:
        return out
    for line in raw.splitlines()[2:]:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        if name == "lo":
            continue
        fields = rest.split()
        if len(fields) < 16:
            continue
        try:
            out[name] = {
                "rx_bytes": int(fields[0]),
                "rx_packets": int(fields[1]),
                "tx_bytes": int(fields[8]),
                "tx_packets": int(fields[9]),
            }
        except ValueError:  # pragma: no cover
            continue
    return out


def network() -> Dict[str, Any]:
    """Byte counters plus per-second rates derived from the previous sample."""
    global _net_sample
    now = time.time()
    counters = _net_counters()
    rx_total = sum(value["rx_bytes"] for value in counters.values()) if counters else None
    tx_total = sum(value["tx_bytes"] for value in counters.values()) if counters else None

    with _lock:
        previous = _net_sample
        _net_sample = {"ts": now, "counters": counters, "rx": rx_total, "tx": tx_total}

    seconds = None  # type: Optional[float]
    if previous is not None:
        seconds = now - previous["ts"]

    interfaces = {}  # type: Dict[str, Dict[str, Any]]
    for name, value in counters.items():
        entry = dict(value)
        entry["rx_per_sec"] = None
        entry["tx_per_sec"] = None
        if seconds:
            before = (previous.get("counters") or {}).get(name)
            if before:
                entry["rx_per_sec"] = _rate(before["rx_bytes"], value["rx_bytes"], seconds)
                entry["tx_per_sec"] = _rate(before["tx_bytes"], value["tx_bytes"], seconds)
        interfaces[name] = entry

    return {
        "interfaces": interfaces,
        "rx_bytes": rx_total,
        "tx_bytes": tx_total,
        "rx_per_sec": _rate(previous["rx"], rx_total, seconds) if seconds else None,
        "tx_per_sec": _rate(previous["tx"], tx_total, seconds) if seconds else None,
        "sample_seconds": round(seconds, 2) if seconds else None,
    }


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------
def process_count() -> Optional[int]:
    psutil = _psutil()
    if psutil is not None:
        try:
            return len(psutil.pids())
        except Exception:  # pragma: no cover
            pass
    if _HAS_PROC:
        try:
            return sum(1 for entry in os.listdir("/proc") if entry.isdigit())
        except OSError:  # pragma: no cover
            return None
    return None


def _clock_ticks() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK")) or 100.0
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        return 100.0


def _page_size() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):  # pragma: no cover
        return 4096


def _proc_process_sample() -> List[Dict[str, Any]]:
    """Every process, from ``/proc``. One file read each, nothing else."""
    if not _HAS_PROC:
        return []
    page = _page_size()
    ticks = _clock_ticks()
    out = []  # type: List[Dict[str, Any]]
    try:
        pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:  # pragma: no cover
        return []
    for pid in pids:
        raw = _read("/proc/%s/stat" % pid)
        if not raw:
            continue  # the process exited between listing and reading
        head, _, tail = raw.rpartition(")")
        name = head.partition("(")[2]
        fields = tail.split()
        if len(fields) < 22:
            continue
        try:
            # `fields` starts at the third stat field, so utime(14) is index 11,
            # stime(15) is 12 and rss(24), measured in pages, is index 21.
            cpu_seconds = (int(fields[11]) + int(fields[12])) / ticks
            rss = int(fields[21]) * page
        except (ValueError, IndexError):  # pragma: no cover
            continue
        out.append(
            {
                "pid": int(pid),
                "name": name,
                "status": fields[0],
                "memory_bytes": rss,
                "cpu_seconds": cpu_seconds,
            }
        )
    return out


def _psutil_process_sample(psutil: Any) -> List[Dict[str, Any]]:
    out = []  # type: List[Dict[str, Any]]
    attrs = ["pid", "name", "username", "status", "memory_info", "cpu_times"]
    try:
        iterator = psutil.process_iter(attrs=attrs, ad_value=None)
    except Exception:  # pragma: no cover
        return out
    for process in iterator:
        try:
            info = process.info
            memory = info.get("memory_info")
            times = info.get("cpu_times")
            out.append(
                {
                    "pid": int(info.get("pid") or 0),
                    "name": info.get("name") or "",
                    "user": info.get("username"),
                    "status": info.get("status"),
                    "memory_bytes": int(getattr(memory, "rss", 0) or 0),
                    "cpu_seconds": (
                        float(getattr(times, "user", 0.0) + getattr(times, "system", 0.0))
                        if times is not None
                        else None
                    ),
                }
            )
        except Exception:  # a process that exited mid-iteration
            continue
    return out


def _user_for_pid(pid: int) -> Optional[str]:
    raw = _read("/proc/%d/status" % pid) or ""
    match = re.search(r"^Uid:\s+(\d+)", raw, re.MULTILINE)
    if not match:
        return None
    uid = int(match.group(1))
    try:
        import pwd  # Unix only; stdlib

        return pwd.getpwuid(uid).pw_name
    except Exception:
        return str(uid)


def _cmdline_for_pid(pid: int) -> Optional[str]:
    raw = _read("/proc/%d/cmdline" % pid)
    if raw is None:
        return None
    parts = [part for part in raw.split("\x00") if part]
    return " ".join(parts) if parts else None


def top_processes(limit: int = 10) -> List[Dict[str, Any]]:
    """The heaviest processes, memory first then CPU.

    CPU is the share used since the previous call to this function, so the first
    call reports ``None`` rather than inventing a number -- exactly like
    :func:`cpu_percent`. Only the rows that survive the sort pay for the extra
    reads (command line, owner), which keeps a 200-process box cheap.
    """
    global _proc_cpu_sample
    limit = max(1, min(int(limit or 10), 100))

    psutil = _psutil()
    if psutil is not None:
        sample = _psutil_process_sample(psutil)
    else:
        sample = _proc_process_sample()
    if not sample:
        return []

    now = time.time()
    times = dict((row["pid"], row["cpu_seconds"]) for row in sample if row["cpu_seconds"] is not None)
    with _lock:
        previous = _proc_cpu_sample
        _proc_cpu_sample = {"ts": now, "times": times}

    elapsed = (now - previous["ts"]) if previous else 0.0
    cores = cpu_count() or 1
    total_memory = memory_total()

    for row in sample:
        percent = None
        if elapsed > 0 and row["cpu_seconds"] is not None:
            before = previous["times"].get(row["pid"])
            if before is not None and row["cpu_seconds"] >= before:
                percent = round((row["cpu_seconds"] - before) * 100.0 / elapsed, 1)
                # A process on 4 cores can legitimately exceed 100%; cap at the
                # number of cores so a clock hiccup cannot produce nonsense.
                percent = min(percent, 100.0 * cores)
        row["cpu_percent"] = percent
        row["memory_percent"] = _pct(row["memory_bytes"], total_memory)

    sample.sort(
        key=lambda row: (row.get("memory_bytes") or 0, row.get("cpu_percent") or 0.0),
        reverse=True,
    )
    top = sample[:limit]

    for row in top:
        row.pop("cpu_seconds", None)
        if row.get("user") is None and _HAS_PROC:
            row["user"] = _user_for_pid(row["pid"])
        row.setdefault("user", None)
        if _HAS_PROC:
            row["cmdline"] = _cmdline_for_pid(row["pid"]) or row.get("name")
        else:
            row["cmdline"] = _psutil_cmdline(psutil, row["pid"]) or row.get("name")
    return top


def _psutil_cmdline(psutil: Any, pid: int) -> Optional[str]:
    if psutil is None:
        return None
    try:
        parts = psutil.Process(pid).cmdline()
    except Exception:
        return None
    return " ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# the payloads
# ---------------------------------------------------------------------------
def stats() -> Dict[str, Any]:
    """The five-second payload: everything that changes.

    ``main._stats_loop`` finds this function by name and publishes the result on
    the ``system.stats`` topic; ``GET /api/system/stats`` returns it verbatim.
    A few values appear twice under different names (``cpu_percent`` and
    ``cpu.percent``, ``swap`` and ``memory.swap``) because both spellings are
    read: the flat ones by the dashboard pills, the nested ones by the system
    page. They are the same numbers, produced once.
    """
    cpu = cpu_percent()
    mem = memory()
    net = network()
    return {
        "ts": _utcnow_iso(),
        "available": True,
        "cpu_percent": cpu["percent"],
        "per_core": cpu["per_core"],
        "cpu": cpu,
        "load": load_average(),
        "memory": mem,
        "swap": mem.get("swap"),
        "disks": disks(),
        "temp_c": temperature_c(),
        "net": net,
        "uptime_seconds": uptime_seconds(),
        "uptime": uptime_seconds(),
        "process_uptime_seconds": round(time.time() - _process_start, 1),
        "processes": process_count(),
        "throttled": throttled(),
        "psutil": have_psutil(),
    }


def snapshot() -> Dict[str, Any]:
    """Stats plus the static host facts, for the system page's first paint."""
    payload = stats()
    payload["info"] = host_info()
    return payload


__all__ = [
    "PSUTIL_HINT",
    "boot_time",
    "board_model",
    "cpu_count",
    "cpu_freq_mhz",
    "cpu_model",
    "cpu_percent",
    "disks",
    "have_psutil",
    "host_info",
    "info",
    "is_raspberry_pi",
    "load_average",
    "local_ips",
    "memory",
    "memory_total",
    "network",
    "os_name",
    "os_release",
    "process_count",
    "reset_cache",
    "snapshot",
    "stats",
    "temperature_c",
    "throttled",
    "top_processes",
    "uptime_seconds",
]
