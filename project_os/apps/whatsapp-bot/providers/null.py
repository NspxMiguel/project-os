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
            "configured": False,
            "connected": False,
            "reason": "nenhum provedor configurado — ponha provider como cloud_api ou bridge",
        }

    async def probe(self) -> Dict[str, Any]:
        """Aqui "não sei" não cabe: não há ninguém do outro lado para perguntar."""
        return {
            "connected": False,
            "reason": "nenhum provedor configurado — ponha provider como cloud_api ou bridge",
        }

    async def send_text(self, to: str, text: str) -> Dict[str, Any]:
        self.log.info("[provedor nulo] mandaria para %s: %s", to, text)
        return {
            "ok": True,
            "provider": self.name,
            "delivered": False,
            "note": "sem provedor configurado: a mensagem só foi para o registro",
        }


__all__ = ["NullProvider"]
