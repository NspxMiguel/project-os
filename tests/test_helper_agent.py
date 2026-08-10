"""The PC agent, tested where it can be: what it claims and what it will run.

The loop itself is network code and stays out of the suite. Its two decisions
are here, and they are the ones with consequences -- claiming a capability the
machine does not have wastes real jobs, and running a job kind that was never
meant to be runnable turns the agent into a remote shell.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[1] / "agents" / "helper_agent.py"


@pytest.fixture()
def agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PROJECTOS_HELPER_CONFIG", str(tmp_path / "conf.json"))
    spec = importlib.util.spec_from_file_location("project_os_helper_agent", AGENT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_it_only_claims_tools_that_are_installed(agent, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "has", lambda command: False)
    monkeypatch.setattr(agent, "free_gigabytes", lambda: 1.0)
    monkeypatch.setattr(agent, "gpu_present", lambda: False)
    assert agent.capabilities() == ["cpu"]

    monkeypatch.setattr(agent, "has", lambda command: command == "ffmpeg")
    assert agent.capabilities() == ["cpu", "transcode"]


def test_a_roomy_disk_is_offered_and_a_full_one_is_not(agent, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "has", lambda command: False)
    monkeypatch.setattr(agent, "gpu_present", lambda: False)
    monkeypatch.setattr(agent, "free_gigabytes", lambda: 500.0)
    assert "storage" in agent.capabilities()
    monkeypatch.setattr(agent, "free_gigabytes", lambda: 3.0)
    assert "storage" not in agent.capabilities()


def test_the_capabilities_it_claims_are_ones_the_server_knows(agent) -> None:
    # A typo here is invisible: the job simply never arrives.
    from project_os.core import helpers as core

    monkeyless = agent.capabilities()
    assert set(monkeyless).issubset(set(core.CAPABILITIES))


def test_facts_are_json_serialisable(agent) -> None:
    import json

    json.dumps(agent.facts())


def test_an_unknown_job_kind_is_refused_rather_than_run(agent) -> None:
    result, error = agent.run_job({"kind": "rm -rf /", "payload": {}})
    assert result is None
    assert "nao sabe fazer" in error


def test_ping_answers_without_touching_the_system(agent) -> None:
    result, error = agent.run_job({"kind": "ping"})
    assert error == "" and result["pong"] is True


def test_transcode_refuses_a_file_that_is_not_there(agent) -> None:
    result, error = agent.run_job(
        {"kind": "transcode", "payload": {"input": "/nope.mkv", "output": "/tmp/out.mp4"}}
    )
    assert result is None and "nao achei" in error


def test_transcode_needs_both_ends(agent) -> None:
    result, error = agent.run_job({"kind": "transcode", "payload": {"input": "/tmp/a.mkv"}})
    assert result is None and "input/output" in error


def test_the_token_file_is_not_world_readable(agent, tmp_path: Path) -> None:
    agent.save_config({"server": "http://pi:8099", "token": "secret"})
    path = Path(agent.config_path())
    assert agent.load_config()["token"] == "secret"
    if os.name != "nt":
        assert oct(path.stat().st_mode)[-3:] == "600"


def test_a_missing_config_is_not_a_crash(agent) -> None:
    assert agent.load_config() == {}


def test_running_unpaired_explains_how_to_pair(agent, capsys) -> None:
    assert agent.main([]) == 1
    assert "--pair" in capsys.readouterr().out


def test_it_imports_nothing_that_has_to_be_installed(agent) -> None:
    """One file, stock Python. That promise is easy to break by accident."""
    import ast

    tree = ast.parse(AGENT.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:  # Python 3.9 does not have it
        stdlib = {
            "argparse", "json", "os", "platform", "shutil", "subprocess",
            "sys", "time", "urllib", "__future__",
        }
    outside = sorted(imported - set(stdlib))
    assert outside == [], "the agent would need pip for: %s" % ", ".join(outside)
