"""The WhatsApp bot: allowlist, commands, rate limiting, and the provider seam.

No real network call ever leaves this file: outbound sends go through a fake
httpx module injected at the same seam the app imports it from (see
``fake_httpx``, modelled on conftest's ``fake_ytdl``), and the higher-level
tests exercise the webhook routes with fabricated but correctly (or
deliberately incorrectly) signed bodies instead of talking to Meta or a real
bridge process.

The app's own directory is ``whatsapp-bot`` (a hyphen, to match the catalog
id) so its submodules cannot be reached with a literal ``import`` statement --
this file goes through :func:`importlib.import_module`, exactly like
``PluginManager`` does when it loads the app for real.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import sys
import types
from typing import Any, Dict, Iterator, List

import pytest


def _wa(module: str = "app"):
    return importlib.import_module("projectos.apps.whatsapp-bot.%s" % module)


# --------------------------------------------------------------------------- fake httpx


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = json.dumps(self._payload)

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    calls: List[Dict[str, Any]] = []
    #: set by a test to force the next response
    next_response = None  # type: Any

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: Any = None, headers: Any = None, **kwargs: Any) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"url": url, "json": json, "headers": headers or {}})
        if _FakeAsyncClient.next_response is not None:
            return _FakeAsyncClient.next_response
        return _FakeResponse(200, {"ok": True})


@pytest.fixture()
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> Iterator[type]:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.next_response = None
    module = types.ModuleType("httpx")
    module.AsyncClient = _FakeAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", module)
    yield _FakeAsyncClient


# --------------------------------------------------------------------------- pure helpers


def test_digits_strips_everything_but_the_number() -> None:
    app = _wa()
    assert app._digits("+55 (11) 91234-5678") == "5511912345678"
    assert app._digits(None) == ""


def test_split_command_requires_the_configured_prefix() -> None:
    app = _wa()
    assert app._split_command("status", "!") is None
    assert app._split_command("", "!") is None
    assert app._split_command("!status now please", "!") == ("status", ["now", "please"])
    assert app._split_command("!STATUS", "!") == ("status", [])


def test_allowlist_is_closed_by_default_and_opens_by_number() -> None:
    app = _wa()
    assert app._is_allowed("5511999998888", []) is False
    assert app._is_allowed("5511999998888", None) is False
    assert app._is_allowed("5511999998888", ["+55 11 99999-8888"]) is True
    assert app._is_allowed("5511999998888", ["5511000000000"]) is False


# --------------------------------------------------------------------------- commands.py


def test_command_dispatch_returns_the_handlers_reply() -> None:
    commands = _wa("commands")
    registry = commands.CommandRegistry()
    registry.register("ping", lambda ctx: "pong " + ctx.contact, "ping -- test")
    assert registry.names() == ["ping"]

    async def run():
        return await registry.dispatch("ping", [], "!ping", "5511999998888")

    import asyncio

    assert asyncio.run(run()) == "pong 5511999998888"


def test_command_dispatch_awaits_an_async_handler() -> None:
    commands = _wa("commands")
    registry = commands.CommandRegistry()

    async def handler(ctx):
        return "async reply"

    registry.register("slow", handler)

    import asyncio

    async def run():
        return await registry.dispatch("slow", [], "!slow", "contact")

    assert asyncio.run(run()) == "async reply"


def test_an_unknown_command_raises() -> None:
    commands = _wa("commands")
    registry = commands.CommandRegistry()

    import asyncio

    with pytest.raises(commands.UnknownCommand):
        asyncio.run(registry.dispatch("nope", [], "!nope", "contact"))


# --------------------------------------------------------------------------- ratelimit.py


def test_rate_limiter_allows_up_to_the_limit_then_refuses() -> None:
    ratelimit = _wa("ratelimit")
    clock = {"t": 0.0}
    limiter = ratelimit.RateLimiter(window_seconds=60.0, clock=lambda: clock["t"])

    assert limiter.allow("contact", 2) is True
    assert limiter.allow("contact", 2) is True
    assert limiter.allow("contact", 2) is False  # third hit within the window


def test_rate_limiter_forgets_hits_older_than_the_window() -> None:
    ratelimit = _wa("ratelimit")
    clock = {"t": 0.0}
    limiter = ratelimit.RateLimiter(window_seconds=60.0, clock=lambda: clock["t"])

    assert limiter.allow("contact", 1) is True
    assert limiter.allow("contact", 1) is False
    clock["t"] = 61.0  # past the window
    assert limiter.allow("contact", 1) is True


def test_rate_limiter_tracks_contacts_independently() -> None:
    ratelimit = _wa("ratelimit")
    limiter = ratelimit.RateLimiter(window_seconds=60.0, clock=lambda: 0.0)

    assert limiter.allow("a", 1) is True
    assert limiter.allow("b", 1) is True
    assert limiter.allow("a", 1) is False
    assert limiter.allow("b", 1) is False


# --------------------------------------------------------------------------- providers


def test_build_provider_falls_back_to_null_on_a_bad_name() -> None:
    providers = _wa("providers")
    provider = providers.build_provider("something-that-does-not-exist", {})
    assert isinstance(provider, providers.NullProvider)
    provider2 = providers.build_provider(None, {})
    assert isinstance(provider2, providers.NullProvider)


def test_null_provider_never_claims_to_be_connected() -> None:
    providers = _wa("providers")
    provider = providers.NullProvider({})
    assert provider.status()["connected"] is False


def test_cloud_api_verify_challenge_echoes_only_a_matching_token() -> None:
    providers = _wa("providers")
    provider = providers.CloudApiProvider({"verify_token": "s3cr3t"})
    assert provider.verify_challenge(
        {"hub.mode": "subscribe", "hub.verify_token": "s3cr3t", "hub.challenge": "abc123"}
    ) == "abc123"
    assert provider.verify_challenge(
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "abc123"}
    ) is None
    assert provider.verify_challenge({}) is None


def test_cloud_api_verify_signature_accepts_correct_and_rejects_forged() -> None:
    providers = _wa("providers")
    provider = providers.CloudApiProvider({"app_secret": "my-app-secret"})
    body = b'{"entry": []}'
    good_sig = "sha256=" + hmac.new(b"my-app-secret", body, hashlib.sha256).hexdigest()
    assert provider.verify_signature({"x-hub-signature-256": good_sig}, body) is True

    forged_sig = "sha256=" + hmac.new(b"not-the-secret", body, hashlib.sha256).hexdigest()
    assert provider.verify_signature({"x-hub-signature-256": forged_sig}, body) is False
    assert provider.verify_signature({}, body) is False


def test_cloud_api_verify_signature_refuses_when_no_app_secret_is_set() -> None:
    providers = _wa("providers")
    provider = providers.CloudApiProvider({})
    body = b"{}"
    sig = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
    assert provider.verify_signature({"x-hub-signature-256": sig}, body) is False


def test_bridge_verify_signature_is_a_plain_token_match() -> None:
    providers = _wa("providers")
    provider = providers.BridgeProvider({"token": "bridge-token"})
    assert provider.verify_signature({"x-bridge-token": "bridge-token"}, b"{}") is True
    assert provider.verify_signature({"x-bridge-token": "wrong"}, b"{}") is False
    assert provider.verify_signature({}, b"{}") is False


# --------------------------------------------------------------------------- HTTP-level (client)

PREFIX = "/api/apps/whatsapp-bot"


def _set_config(client: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
    response = client.put(PREFIX + "/config", json=patch)
    assert response.status_code == 200, response.text
    return response.json()


def test_provider_defaults_to_null_and_the_app_starts_with_nothing_configured(auth_client: Any) -> None:
    response = auth_client.get(PREFIX + "/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "null"
    assert body["connected"] is False


def test_secrets_never_come_back_from_the_config_api(auth_client: Any) -> None:
    _set_config(auth_client, {
        "provider": "cloud_api",
        "cloud_api.access_token": "super-secret-token",
        "cloud_api.app_secret": "super-secret-app-secret",
    })
    response = auth_client.get(PREFIX + "/config")
    assert response.status_code == 200, response.text
    cfg = response.json()["config"]
    assert cfg["cloud_api"]["access_token"] == "********"
    assert cfg["cloud_api"]["app_secret"] == "********"
    assert "super-secret-token" not in json.dumps(cfg)


def test_hub_challenge_handshake_echoes_the_challenge_for_the_right_token(auth_client: Any) -> None:
    _set_config(auth_client, {"provider": "cloud_api", "cloud_api.verify_token": "the-verify-token"})
    ok = auth_client.get(PREFIX + "/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "the-verify-token", "hub.challenge": "echo-me",
    })
    assert ok.status_code == 200
    assert ok.text == "echo-me"

    bad = auth_client.get(PREFIX + "/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong-token", "hub.challenge": "echo-me",
    })
    assert bad.status_code == 403


def test_webhook_rejects_a_forged_signature_and_accepts_a_correct_one(auth_client: Any) -> None:
    _set_config(auth_client, {"provider": "cloud_api", "cloud_api.app_secret": "the-app-secret"})
    body = json.dumps({"entry": []}).encode("utf-8")
    good_sig = "sha256=" + hmac.new(b"the-app-secret", body, hashlib.sha256).hexdigest()

    forged = auth_client.post(
        PREFIX + "/webhook", content=body,
        headers={"content-type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert forged.status_code == 401

    accepted = auth_client.post(
        PREFIX + "/webhook", content=body,
        headers={"content-type": "application/json", "X-Hub-Signature-256": good_sig},
    )
    assert accepted.status_code == 200
    assert accepted.json()["received"] == 0  # no messages in an empty entry list


def test_allowlist_refuses_a_stranger_and_the_message_is_recorded_as_blocked(auth_client: Any) -> None:
    _set_config(auth_client, {"provider": "cloud_api", "cloud_api.app_secret": "s3cret"})
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {"from": "5511900000001", "type": "text", "text": {"body": "!help"}, "id": "wamid.1"},
        ]}}]}]
    }
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    response = auth_client.post(
        PREFIX + "/webhook", content=body,
        headers={"content-type": "application/json", "X-Hub-Signature-256": sig},
    )
    assert response.status_code == 200
    assert response.json()["received"] == 1

    history = auth_client.get(PREFIX + "/messages").json()["messages"]
    inbound = [m for m in history if m["direction"] == "in"]
    assert inbound and inbound[0]["status"] == "blocked"
    # A stranger never gets a reply -- no "out" message should exist for them.
    assert not [m for m in history if m["direction"] == "out"]


def test_an_allowed_contact_gets_a_command_reply_via_the_bridge_provider(
    auth_client: Any, fake_httpx: type
) -> None:
    _set_config(auth_client, {
        "provider": "bridge",
        "bridge.base_url": "http://bridge.local:9000",
        "bridge.token": "shared-secret",
        "allowlist": ["5511900000002"],
        "commands.prefix": "!",
    })
    payload = {"from": "5511900000002", "text": "!help"}
    body = json.dumps(payload).encode("utf-8")

    response = auth_client.post(
        PREFIX + "/webhook", content=body,
        headers={"content-type": "application/json", "X-Bridge-Token": "shared-secret"},
    )
    assert response.status_code == 200
    assert response.json()["received"] == 1

    # The reply went out through the (fake) bridge, not a real network call.
    assert fake_httpx.calls, "expected the bridge provider to send a reply"
    assert fake_httpx.calls[0]["url"] == "http://bridge.local:9000/send"

    history = auth_client.get(PREFIX + "/messages").json()["messages"]
    outbound = [m for m in history if m["direction"] == "out"]
    assert outbound and outbound[0]["contact"] == "5511900000002"


def test_an_unknown_command_gets_a_reply_saying_so(auth_client: Any, fake_httpx: type) -> None:
    _set_config(auth_client, {
        "provider": "bridge",
        "bridge.base_url": "http://bridge.local:9000",
        "bridge.token": "tok",
        "allowlist": ["5511900000003"],
    })
    body = json.dumps({"from": "5511900000003", "text": "!not-a-real-command"}).encode("utf-8")
    response = auth_client.post(
        PREFIX + "/webhook", content=body,
        headers={"content-type": "application/json", "X-Bridge-Token": "tok"},
    )
    assert response.status_code == 200
    assert fake_httpx.calls
    assert "desconhecido" in fake_httpx.calls[-1]["json"]["text"].lower()


def test_send_endpoint_uses_the_configured_provider(auth_client: Any, fake_httpx: type) -> None:
    _set_config(auth_client, {"provider": "bridge", "bridge.base_url": "http://bridge.local:9000"})
    response = auth_client.post(PREFIX + "/send", json={"to": "5511900000009", "text": "hi there"})
    assert response.status_code == 200, response.text
    assert fake_httpx.calls[-1]["json"] == {"to": "5511900000009", "text": "hi there"}


def test_the_null_provider_logs_instead_of_failing(auth_client: Any) -> None:
    # The default provider never raises: it is what lets the app be usable
    # (and testable) with nothing configured at all.
    response = auth_client.post(PREFIX + "/send", json={"to": "5511900000009", "text": "hi"})
    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is False


def test_send_endpoint_reports_a_provider_error_as_a_bad_gateway(auth_client: Any) -> None:
    # An unconfigured bridge (no base_url) is the easy way to make a real
    # provider fail predictably, without touching the network.
    _set_config(auth_client, {"provider": "bridge"})
    response = auth_client.post(PREFIX + "/send", json={"to": "5511900000009", "text": "hi"})
    assert response.status_code == 502


def test_an_app_route_wins_over_the_generic_one(auth_client: Any) -> None:
    """The bot implements its own /config, and its version rebuilds the provider.

    A generic /apps/{id}/config in api/apps.py is registered first, so without
    the app's routes being moved ahead of it, saving a token here would write the
    value and never reconnect -- silently.
    """
    _set_config(auth_client, {"provider": "cloud_api", "cloud_api.verify_token": "tok"})
    echoed = auth_client.get(PREFIX + "/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "hi",
    })
    assert echoed.status_code == 200 and echoed.text == "hi"
