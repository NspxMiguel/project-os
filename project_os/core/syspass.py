"""The password of the Linux account, set from the browser.

    "n da pra criar a passwd na porra do setup??? no site igual ha. se inspira
     nele"

Yes -- and it is better than what shipped. A public image cannot carry a real
password: whatever it is, it is printed in the README of every copy. The first
attempt at fixing that was to expire the password on first login, which locked
the box out entirely, because an expired password makes sshd refuse the
non-interactive commands that are the only way to administer a machine with no
screen.

So the image ships the account *locked*: there is no password, and no password
means no login. The first-run screen -- the one that already asks you to invent
an administrator password -- can set the system one too, and from then on SSH
works with a secret that exists nowhere but this card.

Setting a Unix password needs root, and the service deliberately is not root.
The image installs one small root-owned helper that does exactly this and
nothing else, and grants the service the right to run that one command. If the
helper is absent (a hand-made install, a container, a dev machine), every
function here says so plainly instead of pretending.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

#: Installed by the image. Reads ``user:password`` on stdin, applies it to the
#: one account it is allowed to touch, and exits.
HELPER = "/usr/local/sbin/project-os-set-password"

#: The account the helper is allowed to change.
SERVICE_USER = os.environ.get("PROJECT_OS_SYSTEM_USER", "project-os")

TIMEOUT = 15.0


def available(helper: str = HELPER) -> Dict[str, Any]:
    """Whether this box can have its system password set from the browser."""
    if not os.path.exists(helper):
        return {
            "available": False,
            "user": SERVICE_USER,
            "reason": (
                "This installation has no password helper, so the system "
                "password cannot be changed from here."
            ),
            "hint": "Ele vem na imagem do project-os; numa instalação manual, a senha se define com passwd.",
        }
    if not os.access(helper, os.X_OK):
        return {
            "available": False,
            "user": SERVICE_USER,
            "reason": "O ajudante de senha não está executável.",
            "hint": None,
        }
    return {"available": True, "user": SERVICE_USER, "reason": None, "hint": None}


def _privileged_argv(helper: str) -> Optional[list]:
    """How to run the helper as root, or None when there is no way to."""
    if os.geteuid() == 0:
        return [helper]
    if shutil.which("sudo"):
        # -n: never prompt. A web request must not block on a terminal that
        # nobody is sitting at.
        return ["sudo", "-n", helper]
    return None


def set_password(password: str, helper: str = HELPER, user: str = "") -> Dict[str, Any]:
    """Set the Linux account's password. Returns a result, never raises.

    The password reaches the helper on stdin, never as an argument: arguments
    are readable in ``ps`` by every user on the machine.
    """
    account = user or SERVICE_USER
    state = available(helper)
    if not state["available"]:
        return {"ok": False, "code": "unavailable", "message": state["reason"], "hint": state["hint"]}

    if not isinstance(password, str) or not password:
        # Empty is the one refusal left: an account with an empty password is
        # not a short password, it is an open door. Everything else is his call.
        return {
            "ok": False,
            "code": "empty",
            "message": "A senha do sistema não pode ficar vazia.",
            "hint": None,
        }
    if "\n" in password or ":" in password:
        # chpasswd's format is user:password, one per line.
        return {
            "ok": False,
            "code": "bad_password",
            "message": "A senha do sistema não pode ter dois-pontos nem quebra de linha.",
            "hint": None,
        }

    argv = _privileged_argv(helper)
    if argv is None:
        return {
            "ok": False,
            "code": "needs_root",
            "message": "Este serviço não consegue virar root para definir a senha.",
            "hint": None,
        }

    try:
        completed = subprocess.run(
            argv,
            input=("%s:%s\n" % (account, password)).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "code": "failed", "message": "Não consegui rodar o ajudante de senha: %s" % exc, "hint": None}

    if completed.returncode != 0:
        detail = (completed.stdout or b"").decode("utf-8", "replace").strip()
        log.warning("password helper failed: %s", detail)
        return {
            "ok": False,
            "code": "failed",
            "message": "O ajudante de senha recusou: %s" % (detail or "no reason given"),
            "hint": None,
        }

    log.info("system password for %s set from the web interface", account)
    return {"ok": True, "code": "", "message": "", "hint": None, "user": account}


__all__ = ["HELPER", "SERVICE_USER", "available", "set_password"]
