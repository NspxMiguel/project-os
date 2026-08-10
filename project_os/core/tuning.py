"""The knobs on the board itself: fan, clock, LEDs, HDMI, Wi-Fi power save.

    "ai opção de diminuir ventoinha do rp, e etc. varias coisas legais adiciona."

Everything here is split into two kinds of change, and the difference is the
whole design of this module:

**Now** -- a write to ``/sys`` or one ``vcgencmd``. Takes effect immediately and
is gone after a reboot. Fan level, LED brightness, HDMI output, Wi-Fi power save,
CPU governor.

**On boot** -- a line in ``config.txt``. Needs a reboot and *survives* one. Fan
temperature thresholds, clock ceiling, GPU memory split.

They are reported separately and applied separately, because a knob that quietly
does one when you meant the other is how a machine ends up in a state nobody can
explain a month later.

Reading is always allowed. Writing needs ``security.allow_hardware_control`` and,
for most knobs, root -- when it is missing, every knob says so in ``writable``
and ``reason`` rather than failing at the moment you drag the slider.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: Raspberry Pi OS moved the boot partition in Bookworm; older images and other
#: distributions still use the old path. First one that exists wins.
CONFIG_TXT_CANDIDATES = (
    "/boot/firmware/config.txt",
    "/boot/config.txt",
)

CPUFREQ = "/sys/devices/system/cpu/cpu0/cpufreq"
COOLING_GLOB = "/sys/class/thermal/cooling_device*"
LED_GLOB = "/sys/class/leds/*"
COMMAND_TIMEOUT = 5.0

#: Bounds we refuse to write past. Not paternalism -- these are the values where
#: a Pi stops booting, and a box that will not boot cannot be fixed from a
#: browser.
GPU_MEM_RANGE = (16, 512)
ARM_FREQ_RANGE = (600, 3000)


# ---------------------------------------------------------------------------
# small readers
# ---------------------------------------------------------------------------
def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def _write(path: str, value: str) -> None:
    """Write to a sysfs attribute. Raises OSError, which the caller turns into a
    message naming the file -- "permission denied on /sys/class/leds/ACT/brightness"
    tells you to check the service user; "could not set LED" tells you nothing."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(value)


def _run(argv: List[str]) -> Optional[str]:
    if not shutil.which(argv[0]):
        return None
    try:
        out = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=COMMAND_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace").strip()


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def config_txt_path() -> Optional[str]:
    for candidate in CONFIG_TXT_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# fan
# ---------------------------------------------------------------------------
def _cooling_devices() -> List[Dict[str, Any]]:
    """Cooling devices the kernel exposes. On a Pi 5 the official fan is one of
    these; on a Pi 4 with a HAT fan it usually is not, which is why this can
    legitimately come back empty."""
    devices = []  # type: List[Dict[str, Any]]
    for path in sorted(glob.glob(COOLING_GLOB)):
        kind = _read(os.path.join(path, "type")) or "?"
        cur = _read_int(os.path.join(path, "cur_state"))
        maximum = _read_int(os.path.join(path, "max_state"))
        if cur is None or maximum is None:
            continue
        devices.append(
            {
                "path": path,
                "name": os.path.basename(path),
                "type": kind,
                "level": cur,
                "max_level": maximum,
                "percent": int(round(100.0 * cur / maximum)) if maximum else None,
                "is_fan": "fan" in kind.lower(),
            }
        )
    return devices


def fan() -> Dict[str, Any]:
    devices = _cooling_devices()
    fans = [device for device in devices if device["is_fan"]] or devices
    thresholds = _fan_thresholds()
    return {
        "available": bool(fans),
        "devices": fans,
        "thresholds": thresholds,
        "reason": None if fans else (
            "O kernel não expõe nenhuma ventoinha. Nas placas anteriores à Pi 5 a "
            "ventoinha costuma estar ligada direto no 5V e girar sempre: nesse caso "
            "não há o que controlar por software."
        ),
    }


def _fan_thresholds() -> List[Dict[str, int]]:
    """The ``dtparam=fan_temp0=...`` lines from config.txt, in degrees C.

    These are what actually decide when the fan spins on a Pi 5; the runtime
    level is overridden by the firmware the moment the temperature moves.
    """
    path = config_txt_path()
    if not path:
        return []
    text = _read(path) or ""
    out = []  # type: List[Dict[str, int]]
    for index in range(4):
        temp = re.search(r"^\s*dtparam=fan_temp%d=(\d+)" % index, text, re.M)
        speed = re.search(r"^\s*dtparam=fan_temp%d_speed=(\d+)" % index, text, re.M)
        if temp:
            out.append(
                {
                    "step": index,
                    # firmware writes these in milli-degrees
                    "celsius": int(temp.group(1)) // 1000,
                    "speed": int(speed.group(1)) if speed else 0,
                }
            )
    return out


def set_fan_level(level: int, device: Optional[str] = None) -> Dict[str, Any]:
    """Set the current fan level. Immediate, and *temporary*.

    The firmware's own curve will move it again as the temperature changes. To
    make a fan genuinely quieter, raise the thresholds -- see
    :func:`set_fan_thresholds`.
    """
    devices = [item for item in _cooling_devices() if item["is_fan"]] or _cooling_devices()
    if device:
        devices = [item for item in devices if item["name"] == device]
    if not devices:
        raise LookupError("Nenhuma ventoinha controlável encontrada.")
    target = devices[0]
    level = max(0, min(int(level), target["max_level"]))
    _write(os.path.join(target["path"], "cur_state"), str(level))
    log.info("fan %s set to level %d/%d", target["name"], level, target["max_level"])
    return {
        "device": target["name"],
        "level": level,
        "max_level": target["max_level"],
        "temporary": True,
        "note": "O firmware volta a mandar na ventoinha quando a temperatura mudar. "
                "Para deixar quieta de verdade, suba as temperaturas de acionamento.",
    }


def set_fan_thresholds(steps: List[Dict[str, int]]) -> Dict[str, Any]:
    """Rewrite the fan curve in config.txt. Takes effect on the next boot.

    Higher temperatures mean a quieter box and a hotter chip. We do not stop you
    from doing that -- the Pi throttles itself long before anything is damaged --
    but the numbers go back to the caller so the screen can show them.
    """
    lines = {}  # type: Dict[str, str]
    for step in steps:
        index = int(step["step"])
        if index not in (0, 1, 2, 3):
            raise ValueError("fan_temp%s não existe (use 0 a 3)." % index)
        celsius = int(step["celsius"])
        if not 30 <= celsius <= 85:
            raise ValueError("%d C está fora da faixa aceita (30 a 85)." % celsius)
        lines["dtparam=fan_temp%d" % index] = str(celsius * 1000)
        if "speed" in step:
            speed = max(0, min(255, int(step["speed"])))
            lines["dtparam=fan_temp%d_speed" % index] = str(speed)
    changed = _patch_config_txt(lines)
    return {"applied_on_reboot": True, "changed": changed, "steps": steps}


# ---------------------------------------------------------------------------
# clock and governor
# ---------------------------------------------------------------------------
def clock() -> Dict[str, Any]:
    governors = (_read(os.path.join(CPUFREQ, "scaling_available_governors")) or "").split()
    khz = lambda name: _read_int(os.path.join(CPUFREQ, name))  # noqa: E731
    current = khz("scaling_cur_freq")
    return {
        "available": os.path.isdir(CPUFREQ),
        "governor": _read(os.path.join(CPUFREQ, "scaling_governor")),
        "governors": governors,
        "current_mhz": int(current / 1000) if current else None,
        "min_mhz": int((khz("cpuinfo_min_freq") or 0) / 1000) or None,
        "max_mhz": int((khz("cpuinfo_max_freq") or 0) / 1000) or None,
        "limit_mhz": int((khz("scaling_max_freq") or 0) / 1000) or None,
        "boot_arm_freq": _config_value("arm_freq"),
        "boot_over_voltage": _config_value("over_voltage"),
    }


def set_governor(name: str) -> Dict[str, Any]:
    """``powersave`` on a Pi is a real thing: it pins the clock at the minimum,
    which is a good trade for a box that mostly waits for MQTT messages."""
    available = (_read(os.path.join(CPUFREQ, "scaling_available_governors")) or "").split()
    if name not in available:
        raise ValueError(
            "Governor %r não existe aqui. Disponíveis: %s" % (name, ", ".join(available) or "-")
        )
    written = 0
    for path in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")):
        try:
            _write(path, name)
            written += 1
        except OSError:
            continue
    if not written:
        raise OSError("Não foi possível escrever em nenhum núcleo (precisa de root).")
    log.info("cpu governor set to %s on %d cores", name, written)
    return {"governor": name, "cores": written, "temporary": True}


def set_max_clock(mhz: Optional[int]) -> Dict[str, Any]:
    """Cap the clock in config.txt (``arm_freq``). Applies on the next boot.

    ``None`` removes the line and gives the board its stock speed back. There is
    no overclock helper here on purpose: raising ``arm_freq`` past stock also
    wants ``over_voltage`` and a real heatsink, and a browser slider is the wrong
    place to make that decision by accident.
    """
    if mhz is None:
        changed = _patch_config_txt({"arm_freq": None})
        return {"applied_on_reboot": True, "changed": changed, "arm_freq": None}
    mhz = int(mhz)
    if not ARM_FREQ_RANGE[0] <= mhz <= ARM_FREQ_RANGE[1]:
        raise ValueError("arm_freq precisa ficar entre %d e %d MHz." % ARM_FREQ_RANGE)
    changed = _patch_config_txt({"arm_freq": str(mhz)})
    return {"applied_on_reboot": True, "changed": changed, "arm_freq": mhz}


# ---------------------------------------------------------------------------
# LEDs, HDMI, Wi-Fi, GPU memory
# ---------------------------------------------------------------------------
def leds() -> List[Dict[str, Any]]:
    """The board's own LEDs. Turning them off is the usual reason to come here:
    a Pi in a bedroom blinking green all night is a real complaint."""
    out = []  # type: List[Dict[str, Any]]
    for path in sorted(glob.glob(LED_GLOB)):
        brightness = _read_int(os.path.join(path, "brightness"))
        if brightness is None:
            continue
        trigger_raw = _read(os.path.join(path, "trigger")) or ""
        active = re.search(r"\[([^\]]+)\]", trigger_raw)
        out.append(
            {
                "name": os.path.basename(path),
                "path": path,
                "on": brightness > 0,
                "brightness": brightness,
                "max_brightness": _read_int(os.path.join(path, "max_brightness")) or 1,
                "trigger": active.group(1) if active else None,
                "triggers": [t.strip("[]") for t in trigger_raw.split()],
            }
        )
    return out


def set_led(name: str, on: bool, trigger: Optional[str] = None) -> Dict[str, Any]:
    path = os.path.join("/sys/class/leds", name)
    if not os.path.isdir(path):
        raise LookupError("Não existe LED chamado %r." % name)
    if trigger:
        _write(os.path.join(path, "trigger"), trigger)
    elif not on:
        # Without this the kernel keeps driving the LED from its trigger and the
        # brightness write is undone within a second.
        try:
            _write(os.path.join(path, "trigger"), "none")
        except OSError:
            pass
    maximum = _read_int(os.path.join(path, "max_brightness")) or 1
    _write(os.path.join(path, "brightness"), str(maximum if on else 0))
    return {"name": name, "on": on, "trigger": trigger}


def hdmi() -> Dict[str, Any]:
    state = _run(["vcgencmd", "display_power"])
    match = re.search(r"=(\d)", state or "")
    return {
        "available": bool(state),
        "on": match.group(1) == "1" if match else None,
        "reason": None if state else "vcgencmd não está disponível nesta máquina.",
    }


def set_hdmi(on: bool) -> Dict[str, Any]:
    """Cuts the HDMI output. Worth maybe 25 mA, and worth more than that on a
    headless box plugged into a TV that keeps waking up."""
    result = _run(["vcgencmd", "display_power", "1" if on else "0"])
    if result is None:
        raise OSError("vcgencmd não está disponível nesta máquina.")
    return {"on": on, "output": result}


def _wifi_interfaces() -> List[str]:
    return [
        os.path.basename(path)
        for path in sorted(glob.glob("/sys/class/net/*"))
        if os.path.isdir(os.path.join(path, "wireless"))
    ]


def wifi_power_save() -> Dict[str, Any]:
    """Wi-Fi power save is the usual reason a Pi answers pings in 800 ms.

    Anything that expects to be *reached* -- a media server, a shell, this very
    interface -- wants it off. A sensor that only ever sends wants it on.
    """
    interfaces = []  # type: List[Dict[str, Any]]
    for name in _wifi_interfaces():
        info = _run(["iw", "dev", name, "get", "power_save"]) or ""
        match = re.search(r"power save:\s*(\w+)", info)
        interfaces.append(
            {
                "interface": name,
                "power_save": match.group(1) == "on" if match else None,
            }
        )
    return {
        "available": bool(interfaces) and shutil.which("iw") is not None,
        "interfaces": interfaces,
        "reason": None if shutil.which("iw") else "O comando iw não está instalado.",
    }


def set_wifi_power_save(on: bool, interface: Optional[str] = None) -> Dict[str, Any]:
    names = [interface] if interface else _wifi_interfaces()
    if not names:
        raise LookupError("Nenhuma interface Wi-Fi encontrada.")
    changed = []
    for name in names:
        if _run(["iw", "dev", name, "set", "power_save", "on" if on else "off"]) is None:
            raise OSError("O comando iw não está disponível.")
        changed.append(name)
    return {"power_save": on, "interfaces": changed, "temporary": True}


def gpu_memory() -> Dict[str, Any]:
    """How much RAM the GPU took. It matters here more than it looks: on a
    headless box that split is memory no app will ever see, and it is why a 1 GB
    Pi reports around 948 MB."""
    reported = _run(["vcgencmd", "get_mem", "gpu"]) or ""
    match = re.search(r"(\d+)", reported)
    return {
        "current_mb": int(match.group(1)) if match else None,
        "boot_value": _config_value("gpu_mem"),
        "range_mb": list(GPU_MEM_RANGE),
        "note": "Numa placa sem monitor, 16 MB basta e devolve o resto pros apps.",
    }


def set_gpu_memory(mb: int) -> Dict[str, Any]:
    mb = int(mb)
    if not GPU_MEM_RANGE[0] <= mb <= GPU_MEM_RANGE[1]:
        raise ValueError("gpu_mem precisa ficar entre %d e %d MB." % GPU_MEM_RANGE)
    changed = _patch_config_txt({"gpu_mem": str(mb)})
    return {"applied_on_reboot": True, "changed": changed, "gpu_mem": mb}


# ---------------------------------------------------------------------------
# config.txt
# ---------------------------------------------------------------------------
def _config_value(key: str) -> Optional[str]:
    path = config_txt_path()
    if not path:
        return None
    match = re.search(r"^\s*%s=(.+)$" % re.escape(key), _read(path) or "", re.M)
    return match.group(1).strip() if match else None


def _patch_config_txt(values: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    """Set or remove ``key=value`` lines, keeping the rest of the file byte for byte.

    Written to a sibling temp file and moved into place, so a power cut during
    the write cannot leave a half-written config.txt -- which is the one file on
    the card that will stop the board from booting at all.

    Every managed line is marked, because this file is also edited by hand and by
    ``raspi-config``, and a line that appeared from nowhere is worse than no line.
    """
    path = config_txt_path()
    if not path:
        raise LookupError(
            "Não achei config.txt (%s). Esta máquina provavelmente não é um Raspberry Pi."
            % " nem ".join(CONFIG_TXT_CANDIDATES)
        )
    original = _read(path)
    if original is None:
        raise OSError("Não consegui ler %s." % path)

    marker = "  # project_os"
    lines = original.splitlines()
    changed = {}  # type: Dict[str, Optional[str]]

    for key, value in values.items():
        pattern = re.compile(r"^\s*%s=" % re.escape(key))
        new_line = None if value is None else "%s=%s%s" % (key, value, marker)
        kept = []  # type: List[str]
        replaced = False
        for line in lines:
            if not pattern.match(line):
                kept.append(line)
                continue
            # First match becomes the new value; later duplicates are dropped, so
            # a file edited three times does not end up with three arm_freq lines
            # where only the last one counts.
            if new_line is not None and not replaced:
                kept.append(new_line)
                replaced = True
        if new_line is not None and not replaced:
            kept.append(new_line)
        lines = kept
        changed[key] = value

    body = "\n".join(lines).rstrip("\n") + "\n"
    temp = path + ".project_os-tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    log.info("config.txt updated: %s", ", ".join(sorted(values)))
    return changed


# ---------------------------------------------------------------------------
# the whole picture
# ---------------------------------------------------------------------------
def snapshot() -> Dict[str, Any]:
    """Everything the tuning screen shows, in one call."""
    path = config_txt_path()
    root = is_root()
    return {
        "writable": root,
        "reason": None if root else (
            "O project-os não está rodando como root, então só dá pra ler. O "
            "serviço systemd roda como root; um venv aberto no terminal, não."
        ),
        "config_txt": path,
        "fan": fan(),
        "clock": clock(),
        "leds": leds(),
        "hdmi": hdmi(),
        "wifi": wifi_power_save(),
        "gpu": gpu_memory(),
    }


__all__ = [
    "clock",
    "config_txt_path",
    "fan",
    "gpu_memory",
    "hdmi",
    "is_root",
    "leds",
    "set_fan_level",
    "set_fan_thresholds",
    "set_governor",
    "set_gpu_memory",
    "set_hdmi",
    "set_led",
    "set_max_clock",
    "set_wifi_power_save",
    "snapshot",
    "wifi_power_save",
]
