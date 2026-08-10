"""A local HTTP bridge -- someone's own container running whatsapp-web.js,
Baileys or WPPConnect next to project-os.

This is an unofficial client wearing a phone's identity; Meta can and does
ban numbers it catches doing that. It is also free, needs no business
account, and is the only option for a person who cannot or will not go
through Cloud API approval. project-os does not run the bridge itself -- see
``docs/WHATSAPP.md`` for how to stand one up -- it only talks to it over
``base_url`` using a shared token, the same way it would talk to any other
local service.

The wire contract is deliberately the smallest thing that could work, because
every community bridge shapes its HTTP a little differently:

* outbound: ``POST {base_url}/send`` with JSON ``{"to": ..., "text": ...}``
  and ``Authorization: Bearer <token>``.
* inbound: the bridge POSTs to *this app's* webhook route with header
  ``X-Bridge-Token: <token>`` and JSON ``{"from": ..., "text": ...}`` (or
  ``{"messages": [...]}`` for a batch). A small adapter script in front of an
  off-the-shelf bridge is usually enough to produce this.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Dict, List, Optional

from .base import Provider, ProviderError


class BridgeProvider(Provider):
    name = "bridge"

    def __init__(self, config: Optional[Dict[str, Any]], logger: Optional[logging.Logger] = None) -> None:
        super().__init__(config, logger)
        self.base_url = str(self.config.get("base_url") or "").rstrip("/")
        self.token = str(self.config.get("token") or "")

    def status(self) -> Dict[str, Any]:
        configured = bool(self.base_url)
        return {
            "connected": configured,
            "mode": self.name,
            "base_url": self.base_url or None,
            "reason": None if configured else "missing bridge.base_url",
            # The panel shows pairing as static instructions rather than a live
            # QR image: how a bridge exposes its QR (an HTTP endpoint, a log
            # line, a file) is different per project, and project-os does not
            # run the bridge process to know which.
            "pairing_hint": (
                "Open the bridge container's own logs or QR endpoint and scan "
                "with WhatsApp > Linked devices."
            ),
        }

    async def send_text(self, to: str, text: str) -> Dict[str, Any]:
        if not self.base_url:
            raise ProviderError("The bridge provider is not configured: set bridge.base_url.")
        try:
            import httpx  # optional dependency; only needed for outbound calls
        except ImportError as exc:
            raise ProviderError(
                "The bridge provider needs httpx. Install it with: pip install httpx"
            ) from exc
        headers = {"Authorization": "Bearer %s" % self.token} if self.token else {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                self.base_url + "/send", json={"to": to, "text": text}, headers=headers
            )
        if response.status_code >= 400:
            raise ProviderError(
                "The bridge refused the message (HTTP %s): %s"
                % (response.status_code, response.text[:300])
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        return {"ok": True, "provider": self.name, "delivered": True, "response": body}

    def verify_signature(self, headers: Dict[str, str], raw_body: bytes) -> bool:
        if not self.token:
            self.log.warning(
                "bridge.token is not set; refusing an inbound webhook I cannot authenticate"
            )
            return False
        supplied = headers.get("x-bridge-token", "")
        return hmac.compare_digest(supplied, self.token)

    def parse_inbound(self, raw_body: bytes) -> List[Dict[str, Any]]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
            items = payload["messages"]
        else:
            items = [payload]
        out = []  # type: List[Dict[str, Any]]
        for item in items:
            if not isinstance(item, dict):
                continue
            frm = item.get("from") or ""
            text = item.get("text") or ""
            if frm and text:
                out.append({"from": frm, "text": text, "id": item.get("id", "")})
        return out


__all__ = ["BridgeProvider"]
