"""The root helper that the image installs, run for real against fake tools.

The other syspass tests stub the helper out to check what Python sends it. These
run the actual shell script, because the bug that made this file necessary lived
in the script and nowhere else: it set the password and left pi-gen's
"change it at next login" flag standing, so sshd demanded a change it could not
perform and hung up. On a headless box that is a permanent lockout, and every
Python-side test passed while it was happening.
"""

from __future__ import annotations

import os
import subprocess

import pytest

HELPER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "image",
    "stage-project-os",
    "00-project-os",
    "files",
    "usr",
    "local",
    "sbin",
    "project-os-set-password",
)

TOOLS = ("chpasswd", "passwd", "chage")


def run_helper(tmp_path, line):
    """Run the helper with chpasswd/passwd/chage replaced by argv recorders."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for tool in TOOLS:
        script = bin_dir / tool
        script.write_text(
            '#!/bin/sh\nprintf "%s %s\\n" "{name}" "$*" >> "{log}"\ncat >/dev/null\nexit 0\n'.format(
                name=tool, log=log
            )
        )
        script.chmod(0o755)
    env = dict(os.environ)
    # /usr/bin stays on the path for date, printf and friends; the shims come
    # first so they win.
    env["PATH"] = "%s:%s" % (bin_dir, env.get("PATH", "/usr/bin:/bin"))
    result = subprocess.run(
        ["/bin/sh", HELPER],
        input=line.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def called(calls, tool):
    for line in calls:
        if line.startswith(tool + " "):
            return line[len(tool) + 1 :]
    return None


def test_the_helper_exists_and_is_executable():
    assert os.path.isfile(HELPER)
    assert os.access(HELPER, os.X_OK)


def test_setting_a_password_clears_the_forced_change(tmp_path):
    """The regression: a password set from the browser must not arrive expired.

    Without the chage call, the very next SSH login answers "You are required to
    change your password immediately (administrator enforced)", fails with
    "Authentication token manipulation error", and closes the connection.
    """
    result, calls = run_helper(tmp_path, "project-os:um segredo qualquer\n")

    assert result.returncode == 0, result.stderr
    assert called(calls, "chpasswd") is not None
    args = called(calls, "chage")
    assert args is not None, "the helper did not clear the password expiry"
    assert "-M -1" in args, args  # no maximum age: it never expires by itself
    assert args.strip().endswith("project-os")
    assert "-d " in args  # last-changed is today, not the epoch


def test_the_account_is_unlocked_as_well(tmp_path):
    """The image ships the account locked; a password has to undo that."""
    _, calls = run_helper(tmp_path, "project-os:x\n")
    assert called(calls, "passwd") == "-u project-os"


def test_another_account_is_refused_before_anything_runs(tmp_path):
    """A compromised web process must not be able to rewrite root's password."""
    result, calls = run_helper(tmp_path, "root:x\n")
    assert result.returncode == 2
    assert calls == []


def test_an_empty_password_is_refused_before_anything_runs(tmp_path):
    result, calls = run_helper(tmp_path, "project-os:\n")
    assert result.returncode == 3
    assert calls == []


def test_only_the_first_line_is_read(tmp_path):
    """Extra lines on stdin are not a second account to change."""
    result, calls = run_helper(tmp_path, "project-os:first\nroot:second\n")
    assert result.returncode == 0
    assert len([line for line in calls if line.startswith("chpasswd")]) == 1


@pytest.mark.parametrize("password", ["a b c", "senha'com\"aspas", "pa$$word", "ç-á-1"])
def test_awkward_passwords_survive_the_trip(tmp_path, password):
    """Nothing here may re-split or expand the secret."""
    result, _ = run_helper(tmp_path, "project-os:%s\n" % password)
    assert result.returncode == 0, result.stderr
