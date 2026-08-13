"""The provider interface -- what every WhatsApp backend must do.

Small on purpose: connect/disconnect for lifecycle, ``status`` for the panel,
``send_text`` for outbound, and three webhook-shaped hooks (``verify_challenge``,
``verify_signature``, ``parse_inbound``) for inbound. A provider that has no
webhook at all (the null provider) just answers "not mine" / "refused" to all
three instead of the app needing to know which providers use webhooks.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

InboundHandler = Callable[[str, str, Dict[str, Any]], Union[None, Awaitable[None]]]


class ProviderError(RuntimeError):
    """A provider could not do what was asked.

    Covers missing credentials, a missing optional dependency, and the remote
    end refusing the request -- always with a message a human can act on,
    never a bare traceback surfaced to the panel.
    """


class Provider(object):
    """Base class. Subclasses override what they actually support."""

    #: Short machine-readable name, also the config subtree the app hands it
    #: (``apps.settings.whatsapp-bot.<name>``) and the value of the
    #: ``provider`` config key that selects it.
    name = "base"

    def __init__(self, config: Optional[Dict[str, Any]], logger: Optional[logging.Logger] = None) -> None:
        self.config = config or {}
        self.log = logger or logging.getLogger("project_os.app.whatsapp-bot.provider")

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        """Prepare to send/receive.

        Must never raise for a missing optional dependency or missing
        credentials -- that goes in :meth:`status` instead, so an
        unconfigured provider still lets the app start.
        """

    async def disconnect(self) -> None:
        """Release whatever :meth:`connect` acquired. Safe to call twice."""

    # -- reporting -----------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return {"configured": False, "connected": False}

    async def probe(self) -> Dict[str, Any]:
        """Pergunta ao outro lado se ele está mesmo lá.

        Sem isto, "Conectado" queria dizer só "tem uma URL escrita no config" --
        a tela dizia Conectado numa caixa que nunca tinha falado com nada. O
        padrão é ``None``: não sei, que é diferente de sim e de não.
        """
        return {"connected": None, "reason": "este provedor não sabe se checar"}

    # -- outbound ------------------------------------------------------------
    async def send_text(self, to: str, text: str) -> Dict[str, Any]:
        raise ProviderError("The %s provider cannot send messages." % self.name)

    # -- inbound (webhook-shaped; a provider without a webhook leaves these
    #    at their defaults, which refuse everything) ------------------------
    def verify_challenge(self, query: Dict[str, str]) -> Optional[str]:
        """The GET handshake some webhooks use to prove the endpoint is real.

        Returns the value to echo back, or ``None`` when this request is not
        a handshake this provider recognises.
        """
        return None

    def verify_signature(self, headers: Dict[str, str], raw_body: bytes) -> bool:
        """Whether an inbound POST can be trusted. False refuses it outright.

        ``headers`` keys are already lower-cased by the caller.
        """
        return False

    def parse_inbound(self, raw_body: bytes) -> List[Dict[str, Any]]:
        """A verified webhook body -> ``[{"from": ..., "text": ..., "id": ...}, ...]``."""
        return []

    def __repr__(self) -> str:
        return "%s()" % type(self).__name__


__all__ = ["Provider", "ProviderError", "InboundHandler"]
