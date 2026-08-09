"""The default provider: logs instead of sending, trusts no webhook.

Exists so ``whatsapp-bot`` installs, starts, and answers its own status and
history endpoints on a machine with zero WhatsApp credentials configured --
which is the honest state of "just installed". A brand new box should be
running, not crashed, before anyone has typed in a token.
"""

from __future__ import annotations

from typing import Any, Dict

from .base import Provider


class NullProvider(Provider):
    name = "null"

    def status(self) -> Dict[str, Any]:
        return {
            "connected": False,
            "reason": "no provider configured -- set provider to cloud_api or bridge",
        }

    async def send_text(self, to: str, text: str) -> Dict[str, Any]:
        self.log.info("[null provider] would send to %s: %s", to, text)
        return {
            "ok": True,
            "provider": self.name,
            "delivered": False,
            "note": "no provider configured; the message was only logged",
        }


__all__ = ["NullProvider"]
