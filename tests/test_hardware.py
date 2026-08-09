"""Board detection, against real revision codes from real machines.

The point of this file is the memory floor. Miguel asked for every Raspberry Pi
except the 512 MB ones, and the failure mode that matters is not "an exotic board
is misidentified" -- it is "the Pi 3B this was written for gets refused because
1 GB of RAM reports as 948 MB". So the boundary cases are tested from both sides.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from projectos.core import hardware


# Revision codes taken from shipping hardware.
# (code, board, physical RAM in MB, SoC)
REAL_CODES = [
    ("a01041", "2B", 1024, "BCM2836"),      # Pi 2 Model B v1.1, Sony UK
    ("a02082", "3B", 1024, "BCM2837"),      # Pi 3 Model B v1.2, Sony UK
    ("a22082", "3B", 1024, "BCM2837"),      # same board, Embest
    ("a020d3", "3B+", 1024, "BCM2837"),     # Pi 3 Model B+ v1.3
    ("9020e0", "3A+", 512, "BCM2837"),      # Pi 3 Model A+  -- below the floor
    ("9000c1", "Zero W", 512, "BCM2835"),   # Pi Zero W      -- below the floor
    ("902120", "Zero 2 W", 512, "BCM2837"), # Pi Zero 2 W    -- below the floor
    ("a03111", "4B", 1024, "BCM2711"),      # Pi 4B 1 GB
    ("b03111", "4B", 2048, "BCM2711"),      # Pi 4B 2 GB
    ("c03112", "4B", 4096, "BCM2711"),      # Pi 4B 4 GB
    ("d03114", "4B", 8192, "BCM2711"),      # Pi 4B 8 GB
    ("c03130", "400", 4096, "BCM2711"),     # Pi 400
    ("a03140", "CM4", 1024, "BCM2711"),     # Compute Module 4
    ("c04170", "5", 4096, "BCM2712"),       # Pi 5 4 GB
    ("d04170", "5", 8192, "BCM2712"),       # Pi 5 8 GB
]


@pytest.mark.parametrize("code,board,ram_mb,soc", REAL_CODES)
def test_revision_codes_decode_to_the_right_board(code, board, ram_mb, soc) -> None:
    decoded = hardware.parse_revision(code)
    assert decoded["new_style"] is True
    assert decoded["board"] == board
    assert decoded["ram_mb"] == ram_mb
    assert decoded["soc"] == soc


def test_old_style_codes_are_reported_as_undecoded_not_guessed() -> None:
    """Pi 1 and the original Zero. All below the floor, so no second table."""
    decoded = hardware.parse_revision("0002")
    assert decoded == {"new_style": False}


@pytest.mark.parametrize("garbage", [None, "", "zzzz", "not-a-code", "0x"])
def test_a_broken_revision_code_never_raises(garbage) -> None:
    assert hardware.parse_revision(garbage) == {}


# ------------------------------------------------------------------ the GPU split


@pytest.mark.parametrize(
    "reported,expected",
    [
        (948, 1024),   # Pi 3B, default 64 MB split -- THE case this must not fail
        (972, 1024),   # Pi 3B, 48 MB split
        (760, 1024),   # 1 GB board with a 256 MB split
        (430, 512),    # Pi Zero 2 W
        (490, 512),    # Pi 3A+
        (1970, 2048),  # Pi 4B 2 GB
        (3830, 4096),  # Pi 4B 4 GB
        (7810, 8192),  # Pi 4B 8 GB
    ],
)
def test_gpu_split_is_undone(reported: int, expected: int) -> None:
    assert hardware._round_up_to_board_size(reported) == expected


def test_no_512mb_reading_is_ever_rounded_over_the_floor() -> None:
    """The rounding must not manufacture a supported board out of a small one."""
    for reported in range(300, 512 + 1):
        assert hardware._round_up_to_board_size(reported) <= 512


# ------------------------------------------------------------------ the decision


def _fake_proc(tmp_path: Path, revision: str, mem_kb: int, model: str) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    (proc / "cpuinfo").write_text(
        "processor\t: 0\nHardware\t: BCM2835\nRevision\t: %s\nSerial\t\t: 00000000\nModel\t\t: %s\n"
        % (revision, model)
    )
    (proc / "meminfo").write_text("MemTotal:       %d kB\nMemFree:  100000 kB\n" % mem_kb)
    return proc


def test_a_pi_3b_is_supported(tmp_path: Path) -> None:
    proc = _fake_proc(tmp_path, "a02082", 948 * 1024, "Raspberry Pi 3 Model B Rev 1.2")
    board = hardware.detect(proc_root=str(proc), dt_root=str(tmp_path / "nope"))

    assert board.supported is True
    assert board.reason == ""
    assert board.ram_mb == 1024
    assert board.is_raspberry_pi is True
    assert board.soc == "BCM2837"
    assert board.tier == "minimal"


def test_a_zero_2_w_is_refused_and_says_why(tmp_path: Path) -> None:
    proc = _fake_proc(tmp_path, "902120", 430 * 1024, "Raspberry Pi Zero 2 W Rev 1.0")
    board = hardware.detect(proc_root=str(proc), dt_root=str(tmp_path / "nope"))

    assert board.supported is False
    assert "512" in board.reason
    assert "1 GB" in board.reason


def test_a_pi_5_gets_the_whole_catalogue(tmp_path: Path) -> None:
    proc = _fake_proc(tmp_path, "d04170", 8000 * 1024, "Raspberry Pi 5 Model B Rev 1.0")
    board = hardware.detect(proc_root=str(proc), dt_root=str(tmp_path / "nope"))

    assert board.supported is True
    assert board.tier == "roomy"
    assert board.ram_mb == 8192


def test_an_unknown_board_with_enough_ram_is_supported(tmp_path: Path) -> None:
    """A board released after this file was written must not be refused."""
    proc = _fake_proc(tmp_path, "e0f0f0", 4000 * 1024, "Raspberry Pi 9 Model Q")
    board = hardware.detect(proc_root=str(proc), dt_root=str(tmp_path / "nope"))

    assert board.supported is True
    assert board.model == "Raspberry Pi 9 Model Q"
    assert board.is_raspberry_pi is True


def test_a_plain_linux_box_is_supported(tmp_path: Path) -> None:
    """ProjectOS is not Pi-only; the floor is RAM, not the board."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "cpuinfo").write_text("processor\t: 0\nvendor_id\t: GenuineIntel\n")
    (proc / "meminfo").write_text("MemTotal:       8000000 kB\n")

    board = hardware.detect(proc_root=str(proc), dt_root=str(tmp_path / "nope"))
    assert board.supported is True
    assert board.is_raspberry_pi is False


def test_detect_survives_a_machine_with_no_proc(tmp_path: Path) -> None:
    """Developers work on macOS. This must degrade, not explode."""
    board = hardware.detect(proc_root=str(tmp_path / "nothing"), dt_root=str(tmp_path / "nope"))
    assert isinstance(board.as_dict(), dict)
    assert board.is_raspberry_pi is False


def test_device_tree_model_wins_over_cpuinfo(tmp_path: Path) -> None:
    proc = _fake_proc(tmp_path, "a02082", 948 * 1024, "from cpuinfo")
    dt = tmp_path / "dt"
    dt.mkdir()
    (dt / "model").write_bytes(b"Raspberry Pi 3 Model B Rev 1.2\x00")

    board = hardware.detect(proc_root=str(proc), dt_root=str(dt))
    assert board.model == "Raspberry Pi 3 Model B Rev 1.2"


def test_the_summary_is_json_safe() -> None:
    import json

    json.dumps(hardware.summary())
