"""Filesystem layout.

Everything the running system owns lives under one home directory:
``$PROJECTOS_HOME`` when set, ``~/.projectos`` otherwise. Directories are
created on demand, so a fresh clone boots without an installer step.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

HOME_ENV = "PROJECTOS_HOME"
WEB_ENV = "PROJECTOS_WEB_DIR"
DEFAULT_HOME = "~/.projectos"

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_DIR = _PACKAGE_DIR.parent
_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def package_dir() -> Path:
    """Directory of the installed ``projectos`` package."""
    return _PACKAGE_DIR


def home() -> Path:
    """Root of all runtime state. Created on demand."""
    raw = os.environ.get(HOME_ENV) or DEFAULT_HOME
    return _ensure_dir(Path(raw).expanduser().resolve())


def config_file() -> Path:
    return home() / "config.yaml"


def db_file() -> Path:
    return home() / "projectos.db"


def log_file() -> Path:
    return home() / "projectos.log"


def apps_dir() -> Path:
    """User-installed apps (override bundled apps of the same id)."""
    return _ensure_dir(home() / "apps")


def data_dir(app_id: str) -> Path:
    """Private per-app storage."""
    if not app_id or not _APP_ID_RE.match(app_id):
        raise ValueError("invalid app id: %r" % (app_id,))
    return _ensure_dir(home() / "data" / app_id)


def media_dir() -> Path:
    return _ensure_dir(home() / "media")


def builtin_apps_dir() -> Path:
    """Apps bundled with the package (``projectos/apps``)."""
    return _PACKAGE_DIR / "apps"


def web_dir() -> Path:
    """The core frontend directory.

    Checked in order: ``$PROJECTOS_WEB_DIR``, ``projectos/web`` (when the
    frontend was shipped inside the package), then ``<repo>/web`` (source
    checkout, which is how the installer deploys it).
    """
    raw = os.environ.get(WEB_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    packaged = _PACKAGE_DIR / "web"
    if packaged.is_dir():
        return packaged
    return _REPO_DIR / "web"
