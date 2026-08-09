"""config.txt is the one file on the card that can stop the board from booting.

These tests are all about that file: that we change only the line asked for,
that we never leave duplicates behind, and that reading a machine which is not a
Raspberry Pi is quiet rather than an exception.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from projectos.core import tuning

SAMPLE = """\
# For more options and information see rpiconfig
dtparam=audio=on
camera_auto_detect=1
display_auto_detect=1
arm_64bit=1

[cm4]
otg_mode=1

[all]
gpu_mem=128
"""


@pytest.fixture()
def config_txt(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "config.txt"
    path.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(tuning, "config_txt_path", lambda: str(path))
    return path


def test_setting_a_value_replaces_only_that_line(config_txt: Path) -> None:
    tuning._patch_config_txt({"gpu_mem": "16"})
    text = config_txt.read_text()
    assert "gpu_mem=16" in text
    assert "gpu_mem=128" not in text
    # everything else survives, comments and section headers included
    for line in ("dtparam=audio=on", "[cm4]", "otg_mode=1", "# For more options"):
        assert line in text


def test_a_new_value_is_appended_and_marked(config_txt: Path) -> None:
    tuning._patch_config_txt({"arm_freq": "1200"})
    text = config_txt.read_text()
    assert "arm_freq=1200" in text
    # marked, because this file is also edited by hand and by raspi-config
    assert "# projectos" in text.split("arm_freq=1200")[1].splitlines()[0]


def test_none_removes_the_line(config_txt: Path) -> None:
    tuning._patch_config_txt({"gpu_mem": None})
    assert "gpu_mem" not in config_txt.read_text()


def test_duplicates_collapse_to_one(config_txt: Path) -> None:
    config_txt.write_text(SAMPLE + "gpu_mem=64\ngpu_mem=32\n", encoding="utf-8")
    tuning._patch_config_txt({"gpu_mem": "16"})
    lines = [l for l in config_txt.read_text().splitlines() if l.startswith("gpu_mem=")]
    assert lines == ["gpu_mem=16  # projectos"]


def test_the_file_always_ends_with_one_newline(config_txt: Path) -> None:
    tuning._patch_config_txt({"arm_freq": "1000"})
    body = config_txt.read_text()
    assert body.endswith("\n") and not body.endswith("\n\n")


def test_writing_without_a_config_txt_is_a_lookup_error(monkeypatch) -> None:
    monkeypatch.setattr(tuning, "config_txt_path", lambda: None)
    with pytest.raises(LookupError):
        tuning._patch_config_txt({"gpu_mem": "16"})


def test_out_of_range_values_are_refused(config_txt: Path) -> None:
    """The bounds are where a Pi stops booting, and a box that will not boot
    cannot be fixed from a browser."""
    with pytest.raises(ValueError):
        tuning.set_gpu_memory(4)
    with pytest.raises(ValueError):
        tuning.set_max_clock(9000)
    with pytest.raises(ValueError):
        tuning.set_fan_thresholds([{"step": 0, "celsius": 200}])


def test_reading_a_machine_with_none_of_this_still_answers(monkeypatch) -> None:
    """A laptop is not a Pi, and the tuning screen has to say so instead of 500ing."""
    monkeypatch.setattr(tuning, "config_txt_path", lambda: None)
    monkeypatch.setattr(tuning, "_run", lambda argv: None)
    # Not "whatever this machine happens to have": a CI runner does expose
    # cooling devices under /sys/class/thermal, and the test then measured the
    # host instead of the code.
    monkeypatch.setattr(tuning, "_cooling_devices", lambda: [])
    payload = tuning.snapshot()
    assert payload["fan"]["available"] is False
    assert payload["fan"]["reason"]
    assert payload["config_txt"] is None


def test_fan_thresholds_are_written_in_millidegrees(config_txt: Path) -> None:
    """The firmware reads these in thousandths; writing 55 instead of 55000 would
    make the fan run flat out and look like a hardware fault."""
    tuning.set_fan_thresholds([{"step": 0, "celsius": 60, "speed": 120}])
    text = config_txt.read_text()
    assert "dtparam=fan_temp0=60000" in text
    assert "dtparam=fan_temp0_speed=120" in text
    assert tuning._fan_thresholds() == [{"step": 0, "celsius": 60, "speed": 120}]
