"""The provider registry: config value ``provider`` -> a :class:`Provider`.

Everything the app does with WhatsApp goes through this seam. Adding a fourth
backend later means one new module and one line here, never a change to
``app.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type

from .base import Provider, ProviderError
from .bridge import BridgeProvider
from .cloud_api import CloudApiProvider
from .null import NullProvider

_PROVIDERS = {
    NullProvider.name: NullProvider,
    CloudApiProvider.name: CloudApiProvider,
    BridgeProvider.name: BridgeProvider,
}  # type: Dict[str, Type[Provider]]


def build_provider(
    name: Optional[str], config: Optional[Dict[str, Any]], logger: Optional[logging.Logger] = None
) -> Provider:
    """Build the provider named by config. An unknown name degrades to null.

    A typo in ``config.yaml`` should leave the bot running and honest about
    why it is not connected, not crash the app on the next boot.
    """
    key = str(name or "null").strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        if logger is not None:
            logger.warning("unknown whatsapp provider %r, falling back to null", name)
        cls = NullProvider
    return cls(config, logger)


__all__ = [
    "Provider",
    "ProviderError",
    "NullProvider",
    "CloudApiProvider",
    "BridgeProvider",
    "build_provider",
]
