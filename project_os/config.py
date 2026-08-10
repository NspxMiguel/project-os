"""Layered configuration.

Four layers, lowest priority first::

    defaults  ->  config.yaml  ->  environment  ->  runtime writes

Environment keys look like ``PROJECT_OS__SERVER__PORT=8099``: the prefix is
stripped, ``__`` means nesting, names are lower-cased and values are parsed as
YAML (so ``true``, ``8099``, ``[a, b]`` and ``"0.0.0.0"`` all do the right
thing).

Merging is deep: writing ``server.port`` never drops ``server.host``, and a
partial ``config.yaml`` never clobbers a whole default subtree.
"""

from __future__ import annotations

import copy
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import yaml

from project_os import paths

log = logging.getLogger(__name__)

ENV_PREFIX = "PROJECT_OS__"

#: Value substituted for secrets by :meth:`Config.as_dict` when redacting.
REDACTED = "********"

#: A leaf whose key contains one of these (case-insensitive) is a secret.
SECRET_HINTS = ("token", "password", "secret", "credential")

#: Normative defaults (see ARCHITECTURE.md section 3).
DEFAULTS = {
    "server": {"host": "0.0.0.0", "port": 8099, "base_url": ""},
    "security": {
        "auth_enabled": True,
        "session_ttl_hours": 720,
        "allow_shell": False,
        "allow_service_control": False,
        "allow_file_write": True,
        # Fan, clock, LEDs, HDMI, Wi-Fi power save. Off by default because a
        # wrong clock setting is one of the few things here that can leave the
        # box needing a keyboard and a monitor to recover.
        "allow_hardware_control": False,
        # Installing software from the browser is the point of Advanced mode
        # ("instalo um firefox, um flatpack store"), so this one ships on. It is
        # still a switch: it runs apt as root, and a box that only ever runs
        # Simple mode has no reason to leave it reachable.
        "allow_package_management": True,
        "file_roots": [],
    },
    "ui": {"default_mode": "simple", "theme": "auto", "accent": "#5ac8a8"},
    # The image boots on UTC, because an image cannot know where it will be
    # plugged in. Everything that decides *when* something happens -- above all
    # BirdTunes' quiet hours and its windows -- has to use the timezone of the
    # house, not of the card. Empty means "whatever the operating system says".
    "system": {
        "timezone": "",
        # Asked over the network on first boot, because a headless box has
        # nobody to ask and the image cannot know where it will be plugged in.
        # Set timezone by hand and this never runs again.
        "timezone_auto": True,
        "timezone_url": "http://ip-api.com/json/?fields=status,timezone",
    },
    "discovery": {"enabled": True, "interval_seconds": 300, "probe_hosts": []},
    # Updating over the network instead of pulling the SD card. manifest_url is
    # a plain URL on purpose: pointing it at our own domain later is one setting,
    # not a code change.
    "updates": {
        "enabled": True,
        "manifest_url": "",
        "branch": "main",
        "check_on_boot": True,
    },
    "apps": {
        "autostart": True,
        # Empty on purpose. A fresh project-os has nothing installed; you open the
        # store and pick. Shipping even one app pre-enabled would make it a
        # system that comes with opinions instead of a floor to build on.
        "enabled": [],
        "settings": {},
        "repositories": [{"name": "builtin", "url": "builtin://core"}],
    },
    "integrations": {"home_assistant": {"enabled": False, "url": "", "token": ""}},
    "logging": {"level": "INFO", "retain_lines": 2000},
}  # type: Dict[str, Any]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursive merge; neither argument is mutated."""
    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        current = result.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            result[key] = deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _get_path(data: Any, path: str, default: Any = None) -> Any:
    if not path:
        return data
    node = data
    for part in path.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return default
    return node


def _set_path(data: Dict[str, Any], path: str, value: Any) -> Dict[str, Any]:
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise ValueError("empty config path")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = copy.deepcopy(value)
    return data


def _drop_path(data: Dict[str, Any], path: str) -> bool:
    """Remove ``path`` from ``data`` if it is there. Returns whether it was."""
    parts = [p for p in path.split(".") if p]
    if not parts:
        return False
    node = data
    for part in parts[:-1]:
        child = node.get(part) if isinstance(node, dict) else None
        if not isinstance(child, dict):
            return False
        node = child
    if isinstance(node, dict) and parts[-1] in node:
        del node[parts[-1]]
        return True
    return False


def _has_path(data: Any, path: str) -> bool:
    sentinel = object()
    return _get_path(data, path, sentinel) is not sentinel


def is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in SECRET_HINTS)


def is_redacted(value: Any) -> bool:
    """True when a value came back from :meth:`Config.as_dict` masked.

    ``PUT /api/settings`` must skip such values instead of writing the mask
    back into the config file.
    """
    return isinstance(value, str) and value == REDACTED


def redact_dict(value: Any, key: str = "") -> Any:
    """Mask secret leaves.

    Only *non-empty* values are masked, so the UI can still tell "not set"
    (``""``) from "set but hidden" (``"********"``) without a parallel flag.
    """
    if isinstance(value, Mapping):
        return dict((k, redact_dict(v, str(k))) for k, v in value.items())
    if isinstance(value, list):
        return [redact_dict(v, key) for v in value]
    if key and is_secret_key(key) and value not in (None, "", [], {}):
        return REDACTED
    return value


#: Convenience alias -- ``redact`` reads better at call sites.
redact = redact_dict


def env_overrides(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Build the nested override dict described by ``PROJECT_OS__*`` variables."""
    source = os.environ if environ is None else environ
    out = {}  # type: Dict[str, Any]
    for raw_key, raw_value in source.items():
        if not raw_key.upper().startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in raw_key[len(ENV_PREFIX):].split("__") if p]
        if not parts:
            continue
        try:
            value = yaml.safe_load(raw_value)
        except yaml.YAMLError:
            value = raw_value
        if isinstance(value, str):
            value = value.strip()
        _set_path(out, ".".join(parts), value)
    return out


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        log.error("config file %s is not valid YAML, ignoring it: %s", path, exc)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        log.error("config file %s must contain a mapping, ignoring it", path)
        return {}
    return data


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------
class AppConfig(object):
    """A view over ``apps.settings.<app_id>`` in the core config.

    Apps see only their own subtree, but writes land in the one
    ``config.yaml`` the user edits and back up.
    """

    def __init__(self, config: "Config", app_id: str) -> None:
        self._config = config
        self.app_id = app_id
        self.root = "apps.settings.%s" % app_id

    def _full(self, path: str = "") -> str:
        return "%s.%s" % (self.root, path) if path else self.root

    def get(self, path: str = "", default: Any = None) -> Any:
        return self._config.get(self._full(path), default)

    def set(self, path: str, value: Any) -> None:
        self._config.set(self._full(path), value)

    def replace(self, path: str, value: Any) -> None:
        """Set ``path`` to exactly ``value`` -- see :meth:`Config.replace`."""
        self._config.replace(self._full(path), value)

    def setdefault(self, path: str, value: Any) -> bool:
        """Set only when unset. Returns True when it wrote something."""
        return self._config.setdefault(self._full(path), value)

    def set_many(self, values: Mapping[str, Any]) -> None:
        for path, value in values.items():
            self.set(path, value)

    def update(self, values: Mapping[str, Any]) -> None:
        """Deep-merge a nested mapping into the app subtree."""
        merged = deep_merge(self.raw_dict(), values)
        self._config.set(self.root, merged)

    def save(self) -> None:
        self._config.save()

    def raw_dict(self) -> Dict[str, Any]:
        value = self._config.get(self.root, {})
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def as_dict(self, redact: bool = True) -> Dict[str, Any]:
        """The app subtree. Secrets are masked unless you ask for them."""
        data = self.raw_dict()
        return redact_dict(data) if redact else data

    def __repr__(self) -> str:
        return "AppConfig(%r)" % self.app_id


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config(object):
    """The merged configuration, plus the layers it was built from."""

    def __init__(
        self,
        path: Optional[Path] = None,
        defaults: Optional[Mapping[str, Any]] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._explicit_path = Path(path) if path is not None else None
        self._defaults = copy.deepcopy(dict(defaults if defaults is not None else DEFAULTS))
        self._environ = environ
        self._file = {}  # type: Dict[str, Any]
        self._env = {}  # type: Dict[str, Any]
        self._runtime = {}  # type: Dict[str, Any]
        self._merged = copy.deepcopy(self._defaults)
        self._lock = threading.RLock()

    # -- layers ----------------------------------------------------------
    @property
    def path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        return paths.config_file()

    def load(self) -> "Config":
        """(Re)read config.yaml and the environment. Runtime writes survive."""
        with self._lock:
            self._file = _read_yaml(self.path)
            self._env = env_overrides(self._environ)
            self._rebuild()
        return self

    reload = load

    def _rebuild(self) -> None:
        merged = deep_merge(self._defaults, self._file)
        merged = deep_merge(merged, self._env)
        merged = deep_merge(merged, self._runtime)
        self._merged = merged

    # -- reads -----------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        with self._lock:
            value = _get_path(self._merged, path, default)
            if isinstance(value, (dict, list)):
                return copy.deepcopy(value)
            return value

    def has(self, path: str) -> bool:
        with self._lock:
            return _has_path(self._merged, path)

    def raw_dict(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._merged)

    def as_dict(self, redact: bool = True) -> Dict[str, Any]:
        """The whole merged config.

        Redacts by default: this is what ``GET /api/settings`` returns, and a
        token must never leave the box by accident. Pass ``False`` for the
        real values (``raw_dict()`` does the same thing, more obviously).
        """
        data = self.raw_dict()
        return redact_dict(data) if redact else data

    def layers(self) -> Dict[str, Dict[str, Any]]:
        """Debug aid: what each layer contributed."""
        with self._lock:
            return {
                "defaults": copy.deepcopy(self._defaults),
                "file": copy.deepcopy(self._file),
                "env": copy.deepcopy(self._env),
                "runtime": copy.deepcopy(self._runtime),
            }

    # -- writes ----------------------------------------------------------
    def set(self, path: str, value: Any) -> None:
        """In-memory write. Call :meth:`save` to persist."""
        with self._lock:
            _set_path(self._runtime, path, value)
            self._rebuild()

    def replace(self, path: str, value: Any) -> None:
        """Set ``path`` to exactly ``value``, keys removed included.

        :meth:`set` writes to the runtime layer, and the merge that follows keeps
        anything the file layer still has under the same path -- so a key you
        deleted comes back on the next read and on the next save. That is right
        for a single leaf and wrong for a whole subtree an editor owns end to
        end: a window removed from a BirdTunes schedule has to stay removed.
        The defaults layer is left alone; it is the floor, not stored state.
        """
        with self._lock:
            _drop_path(self._file, path)
            _set_path(self._runtime, path, value)
            self._rebuild()

    def set_many(self, values: Mapping[str, Any]) -> None:
        """Apply ``{"server.port": 8099, ...}`` in one go."""
        with self._lock:
            for path, value in values.items():
                _set_path(self._runtime, path, value)
            self._rebuild()

    def setdefault(self, path: str, value: Any) -> bool:
        with self._lock:
            if _has_path(self._merged, path):
                return False
            _set_path(self._runtime, path, value)
            self._rebuild()
            return True

    def persisted_dict(self) -> Dict[str, Any]:
        """Exactly what :meth:`save` will write: file layer + runtime writes.

        Defaults are deliberately excluded so config.yaml stays a short file of
        real decisions, and so upgrading project-os can change a default.
        Environment overrides are excluded too -- they belong to the process,
        not to the file.
        """
        with self._lock:
            return deep_merge(self._file, self._runtime)

    def save(self) -> Path:
        """Atomically write config.yaml (temp file + ``os.replace``)."""
        with self._lock:
            data = deep_merge(self._file, self._runtime)
            target = self.path
            target.parent.mkdir(parents=True, exist_ok=True)
            text = yaml.safe_dump(
                data, default_flow_style=False, sort_keys=True, allow_unicode=True
            )
            fd, tmp_name = tempfile.mkstemp(
                prefix=".config-", suffix=".yaml.tmp", dir=str(target.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, str(target))
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            # The runtime layer is now also the file layer. It deliberately
            # stays on top: an env override outranks the file, so clearing it
            # here would make "change a setting, save it" silently revert for
            # any key someone also set through PROJECT_OS__*.
            self._file = data
            self._rebuild()
            return target

    # -- app views -------------------------------------------------------
    def app(self, app_id: str) -> AppConfig:
        return AppConfig(self, app_id)

    def app_ids(self) -> List[str]:
        settings = self.get("apps.settings", {}) or {}
        return sorted(settings.keys()) if isinstance(settings, dict) else []

    def __repr__(self) -> str:
        return "Config(path=%r)" % str(self.path)


# ---------------------------------------------------------------------------
# module-level accessor (FastAPI dependencies reach the config through this)
# ---------------------------------------------------------------------------
_current = None  # type: Optional[Config]
_current_lock = threading.RLock()


def load_config(path: Optional[Path] = None) -> Config:
    """Build a fresh Config from every layer. Does not touch the global."""
    return Config(path=path).load()


def get_config() -> Config:
    """The process-wide Config, loading one on first use."""
    global _current
    with _current_lock:
        if _current is None:
            _current = load_config()
        return _current


def set_config(config: Optional[Config]) -> Optional[Config]:
    """Install the process-wide Config (``None`` clears it, for tests)."""
    global _current
    with _current_lock:
        _current = config
        return _current


__all__ = [
    "AppConfig",
    "Config",
    "DEFAULTS",
    "ENV_PREFIX",
    "REDACTED",
    "SECRET_HINTS",
    "deep_merge",
    "env_overrides",
    "get_config",
    "is_redacted",
    "is_secret_key",
    "load_config",
    "redact",
    "redact_dict",
    "set_config",
]
