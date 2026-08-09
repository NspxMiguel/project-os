"""Home Assistant, as an optional integration.

ProjectOS is not built on Home Assistant and does not need it. But a lot of
houses already have one, and when they do it is the cheapest way to reach a
hundred devices ProjectOS would otherwise have to speak to one protocol at a
time -- so a small REST client earns its place.

Two rules shape this module.

**httpx is optional.** It is imported inside the request, never at module level,
and its absence is reported as a normal failed result with the command that
fixes it. A Pi that never touches Home Assistant should not have to install an
HTTP client.

**Nothing here raises into a request.** A missing token, a wrong address, a box
that is switched off, a token that was revoked -- all of them come back as
``HAResult(ok=False, message="<something a human can act on>")``. The one
exception to the result shape is :meth:`HomeAssistantClient.ping`, which the
contract fixes as ``(ok, message)``.

Everything is driven by ``integrations.home_assistant.{url, token}``. There is
no address and no token anywhere in this file.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

log = logging.getLogger(__name__)

#: Config subtree this client is driven by.
CONFIG_ROOT = "integrations.home_assistant"

#: Short on purpose: this runs inside a web request on a Raspberry Pi, and a
#: Home Assistant that needs more than five seconds to answer ``/api/config``
#: is a problem to report, not to wait for.
DEFAULT_TIMEOUT = 5.0

#: Home Assistant's own default port, used only for addresses that cannot be
#: anything but a local box (see :func:`normalise_url`).
DEFAULT_PORT = 8123

INSTALL_HINT = (
    'The Home Assistant integration needs httpx. Install it with: '
    'pip install "projectos[ha]"'
)

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

#: Domains worth offering as a target elsewhere in the UI.
MEDIA_PLAYER_DOMAIN = "media_player"


def httpx_available() -> Tuple[bool, str]:
    """``(installed, hint)`` -- the same shape the players use."""
    try:
        import httpx  # noqa: F401
    except ImportError:
        return False, INSTALL_HINT
    return True, ""


def normalise_url(raw: Any) -> str:
    """Turn whatever the user typed into a base URL, or ``""``.

    ``homeassistant.local`` and ``192.168.1.10`` get the default port appended:
    an address with no port that is either an ``.local`` name or a bare IPv4
    literal is a Home Assistant box on the LAN, never a reverse proxy, so
    guessing 8123 is safe and saves the most common support question. Anything
    with a real hostname is left exactly as given -- appending a port there
    would break every proxied install.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc or not parts.hostname:
        return ""
    netloc = parts.netloc
    try:
        port = parts.port
    except ValueError:  # e.g. "http://host:notaport"
        return ""
    if port is None and parts.scheme == "http":
        host = parts.hostname
        if host.endswith(".local") or _IPV4_RE.match(host):
            netloc = "%s:%d" % (netloc, DEFAULT_PORT)
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def entity_summary(state: Any) -> Dict[str, Any]:
    """One ``/api/states`` entry, reduced to what a UI actually shows."""
    if not isinstance(state, dict):
        return {"id": "", "entity_id": "", "domain": "", "name": "", "state": None, "attributes": {}}
    entity_id = str(state.get("entity_id") or "")
    attributes = state.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    return {
        "id": entity_id,
        "entity_id": entity_id,
        "domain": domain,
        "name": str(attributes.get("friendly_name") or entity_id),
        "state": state.get("state"),
        "attributes": attributes,
    }


class HAResult(object):
    """The outcome of one call: never an exception, always a message.

    Truthy when ``ok``, so ``if result:`` reads correctly, and ``data`` carries
    whatever the endpoint returned (a list for ``/api/states``, a dict for
    ``/api/config``, ``None`` for an empty body).
    """

    __slots__ = ("ok", "message", "data", "status")

    def __init__(
        self,
        ok: bool,
        message: str = "",
        data: Any = None,
        status: Optional[int] = None,
    ) -> None:
        self.ok = bool(ok)
        self.message = message or ""
        self.data = data
        self.status = status

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "status": self.status,
            "data": self.data,
        }

    def __repr__(self) -> str:
        return "HAResult(ok=%r, status=%r, message=%r)" % (self.ok, self.status, self.message)


class HomeAssistantClient(object):
    """A minimal Home Assistant REST client.

    Built either from the ProjectOS config (the normal path, so changing the
    address in Settings takes effect on the next call with nothing to restart)
    or from an explicit url/token pair, which is what the tests use.
    """

    def __init__(
        self,
        config: Any = None,
        url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._config = config
        self._url = url
        self._token = token
        try:
            self.timeout = float(timeout) if timeout else DEFAULT_TIMEOUT
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT

    # -- configuration ---------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        if self._config is None:
            return default
        try:
            return self._config.get("%s.%s" % (CONFIG_ROOT, key), default)
        except Exception:  # pragma: no cover - a broken config is not fatal here
            log.debug("cannot read %s.%s", CONFIG_ROOT, key, exc_info=True)
            return default

    @property
    def url(self) -> str:
        raw = self._url if self._url is not None else self._cfg("url", "")
        return normalise_url(raw)

    @property
    def token(self) -> str:
        raw = self._token if self._token is not None else self._cfg("token", "")
        return str(raw or "").strip()

    @property
    def enabled(self) -> bool:
        """Whether the user switched the integration on.

        Deliberately *not* consulted before making a request: the settings
        screen has to be able to test an address before enabling it.
        """
        if self._url is not None or self._token is not None:
            return True
        return bool(self._cfg("enabled", False))

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def missing_config_message(self) -> str:
        if not self.url and not self.token:
            return (
                "No Home Assistant is configured yet. Add its address and a "
                "long-lived access token in Settings."
            )
        if not self.url:
            return (
                "Home Assistant needs an address, for example "
                "http://homeassistant.local:%d." % DEFAULT_PORT
            )
        return (
            "Home Assistant needs a long-lived access token. Create one in Home "
            "Assistant under your profile, Security, Long-lived access tokens."
        )

    def summary(self) -> Dict[str, Any]:
        """Safe-to-serialise state for the settings screen. Never the token."""
        available, hint = httpx_available()
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "url": self.url,
            "has_token": bool(self.token),
            "httpx": available,
            "hint": hint,
            "timeout": self.timeout,
        }

    # -- transport -------------------------------------------------------
    async def request(
        self,
        method: str,
        path: str,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> HAResult:
        """One HTTP call, with every failure turned into a result."""
        if not self.configured:
            return HAResult(False, self.missing_config_message())
        available, hint = httpx_available()
        if not available:
            return HAResult(False, hint)

        import httpx  # lazy: optional dependency

        base = self.url
        url = base + (path if path.startswith("/") else "/" + path)
        headers = {
            "Authorization": "Bearer %s" % self.token,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                response = await client.request(
                    method, url, headers=headers, json=json, params=params
                )
        except httpx.TimeoutException:
            return HAResult(
                False,
                "Home Assistant at %s did not answer within %.0f seconds."
                % (base, self.timeout),
            )
        except httpx.HTTPError as exc:
            return HAResult(
                False,
                "Cannot reach Home Assistant at %s. Check the address and that "
                "the box is switched on. (%s)" % (base, type(exc).__name__),
            )
        except Exception as exc:  # malformed URL, unsupported scheme, ...
            return HAResult(
                False, "That Home Assistant address is not usable: %s." % exc
            )

        return self._interpret(response, base, path)

    def _interpret(self, response: Any, base: str, path: str) -> HAResult:
        status = int(getattr(response, "status_code", 0) or 0)
        if status in (401, 403):
            return HAResult(
                False,
                "Home Assistant rejected the access token (HTTP %d). Create a "
                "new long-lived access token and paste it again." % status,
                status=status,
            )
        if status == 404:
            return HAResult(
                False,
                "Home Assistant answered at %s but has nothing at %s (HTTP 404). "
                "Is that really a Home Assistant address?" % (base, path),
                status=status,
            )
        if status >= 500:
            return HAResult(
                False,
                "Home Assistant returned an internal error (HTTP %d). Its own "
                "log will say why." % status,
                status=status,
            )
        if status >= 400:
            return HAResult(
                False, "Home Assistant refused the request (HTTP %d)." % status, status=status
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        except Exception:  # pragma: no cover - defensive
            data = None
        return HAResult(True, "", data, status)

    # -- endpoints -------------------------------------------------------
    async def ping(self) -> Tuple[bool, str]:
        """``(ok, message)`` -- the probe behind the "Test" button."""
        result = await self.request("GET", "/api/config")
        if result.status == 404:
            # Very old cores expose only /api/; a 404 there is a wrong address.
            fallback = await self.request("GET", "/api/")
            if fallback.ok:
                return True, "Connected to Home Assistant at %s." % self.url
            return False, fallback.message
        if not result.ok:
            return False, result.message

        data = result.data if isinstance(result.data, dict) else {}
        name = str(data.get("location_name") or "").strip()
        version = str(data.get("version") or "").strip()
        if name and version:
            return True, "Connected to %s, running Home Assistant %s." % (name, version)
        if version:
            return True, "Connected to Home Assistant %s." % version
        return True, "Connected to Home Assistant at %s." % self.url

    async def states(self) -> HAResult:
        """Every entity, exactly as Home Assistant reports it."""
        result = await self.request("GET", "/api/states")
        if not result.ok:
            return result
        data = result.data if isinstance(result.data, list) else []
        return HAResult(True, "", data, result.status)

    async def entities(self, domain: Optional[str] = None) -> HAResult:
        """Entities reduced to ``{id, name, state, domain, attributes}``."""
        result = await self.states()
        if not result.ok:
            return result
        prefix = "%s." % str(domain).strip() if domain else ""
        out = []  # type: List[Dict[str, Any]]
        for state in result.data or []:
            summary = entity_summary(state)
            if prefix and not summary["entity_id"].startswith(prefix):
                continue
            out.append(summary)
        out.sort(key=lambda item: (item["name"] or "").lower())
        return HAResult(True, "", out, result.status)

    async def call_service(
        self, domain: str, service: str, data: Optional[Dict[str, Any]] = None
    ) -> HAResult:
        """``POST /api/services/<domain>/<service>``."""
        domain = str(domain or "").strip()
        service = str(service or "").strip()
        if not domain or not service:
            return HAResult(False, "A service call needs both a domain and a service name.")
        payload = dict(data) if isinstance(data, dict) else {}
        return await self.request(
            "POST",
            "/api/services/%s/%s" % (quote(domain, safe=""), quote(service, safe="")),
            json=payload,
        )

    async def media_players(self) -> HAResult:
        """Every ``media_player``, in the shape BirdTunes needs to pick one."""
        result = await self.entities(MEDIA_PLAYER_DOMAIN)
        if not result.ok:
            return result
        players = []  # type: List[Dict[str, Any]]
        for entity in result.data or []:
            attributes = entity.get("attributes") or {}
            volume = attributes.get("volume_level")
            players.append(
                {
                    "id": entity["entity_id"],
                    "entity_id": entity["entity_id"],
                    "name": entity["name"],
                    "state": entity["state"],
                    "volume": float(volume) if isinstance(volume, (int, float)) else None,
                    "muted": bool(attributes.get("is_volume_muted"))
                    if "is_volume_muted" in attributes
                    else None,
                    "source": attributes.get("source"),
                    "supported_features": attributes.get("supported_features"),
                }
            )
        return HAResult(True, "", players, result.status)

    # -- convenience used by the BirdTunes Home Assistant player ---------
    async def play_media(
        self,
        entity_id: str,
        url: str,
        content_type: str = "music",
        title: Optional[str] = None,
    ) -> HAResult:
        if not entity_id or not url:
            return HAResult(False, "Playing media needs an entity and a media URL.")
        data = {
            "entity_id": entity_id,
            "media_content_id": url,
            "media_content_type": content_type or "music",
        }  # type: Dict[str, Any]
        if title:
            # HA passes extra keys through to the player integration; the ones
            # that understand it show the title, the ones that do not ignore it.
            data["extra"] = {"title": title}
        return await self.call_service(MEDIA_PLAYER_DOMAIN, "play_media", data)

    async def set_volume(self, entity_id: str, level: float) -> HAResult:
        if not entity_id:
            return HAResult(False, "Setting the volume needs an entity.")
        try:
            value = float(level)
        except (TypeError, ValueError):
            return HAResult(False, "Volume must be a number between 0 and 1.")
        value = max(0.0, min(1.0, value))
        return await self.call_service(
            MEDIA_PLAYER_DOMAIN, "volume_set", {"entity_id": entity_id, "volume_level": value}
        )

    async def media_stop(self, entity_id: str) -> HAResult:
        if not entity_id:
            return HAResult(False, "Stopping playback needs an entity.")
        return await self.call_service(
            MEDIA_PLAYER_DOMAIN, "media_stop", {"entity_id": entity_id}
        )

    def __repr__(self) -> str:
        return "HomeAssistantClient(url=%r, configured=%r)" % (self.url, self.configured)


def client_from_config(config: Any, timeout: float = DEFAULT_TIMEOUT) -> HomeAssistantClient:
    """The client the API and BirdTunes use: config-driven, nothing hardcoded."""
    return HomeAssistantClient(config=config, timeout=timeout)


__all__ = [
    "CONFIG_ROOT",
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT",
    "HAResult",
    "HomeAssistantClient",
    "INSTALL_HINT",
    "MEDIA_PLAYER_DOMAIN",
    "client_from_config",
    "entity_summary",
    "httpx_available",
    "normalise_url",
]
