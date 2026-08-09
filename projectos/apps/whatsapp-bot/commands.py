"""The command registry: built-ins plus whatever other apps add at runtime.

A handler is any callable, sync or async, that takes a :class:`CommandContext`
and returns a string reply (or ``None`` for "no reply"). ``dispatch`` awaits it
either way, so a handler never has to care whether it is a coroutine.

Other ProjectOS apps register a command without importing this module -- they
publish on the event bus instead (see ``REGISTER_TOPIC`` in ``app.py``)::

    ctx.bus.publish_nowait("whatsapp.command.register", {
        "name": "lights",
        "help": "lights <on|off> -- the living room lights",
        "handler": my_handler,
    })

That keeps the two apps decoupled in both directions: whatsapp-bot can be
absent, disabled, or reloaded without the other app's import statement
breaking, and the other app's handler is plain Python, not a plugin class.
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

CommandResult = Union[str, None, Awaitable[Optional[str]]]
CommandHandler = Callable[["CommandContext"], CommandResult]


class UnknownCommand(LookupError):
    """No command by that name. Distinct from a command that raised."""


class CommandContext(object):
    """What a handler receives.

    ``request`` is the FastAPI request that triggered dispatch (a webhook
    delivery or the panel's test-send button) when there is one -- it lets a
    handler reach other ProjectOS state the same way an HTTP route would
    (``from projectos.main import get_plugins; get_plugins(request)``, for
    instance). It is ``None`` when a handler is invoked outside an HTTP
    request, e.g. from a test.
    """

    __slots__ = ("name", "args", "text", "contact", "request")

    def __init__(
        self,
        name: str,
        args: List[str],
        text: str,
        contact: str,
        request: Optional[Any] = None,
    ) -> None:
        self.name = name
        self.args = args
        self.text = text
        self.contact = contact
        self.request = request


class Command(object):
    __slots__ = ("name", "handler", "help")

    def __init__(self, name: str, handler: CommandHandler, help_text: str = "") -> None:
        self.name = name
        self.handler = handler
        self.help = help_text


class CommandRegistry(object):
    def __init__(self) -> None:
        self._commands = {}  # type: Dict[str, Command]

    def register(self, name: str, handler: CommandHandler, help_text: str = "") -> None:
        key = str(name).strip().lower()
        if not key:
            raise ValueError("a command needs a name")
        if not callable(handler):
            raise TypeError("the handler for %r must be callable" % key)
        self._commands[key] = Command(key, handler, help_text)

    def unregister(self, name: str) -> None:
        self._commands.pop(str(name).strip().lower(), None)

    def names(self) -> List[str]:
        return sorted(self._commands)

    def describe(self) -> List[Dict[str, str]]:
        return [{"name": c.name, "help": c.help} for c in (self._commands[n] for n in self.names())]

    async def dispatch(
        self, name: str, args: List[str], text: str, contact: str, request: Optional[Any] = None
    ) -> str:
        key = str(name).strip().lower()
        command = self._commands.get(key)
        if command is None:
            raise UnknownCommand(key)
        context = CommandContext(key, args, text, contact, request)
        result = command.handler(context)
        if inspect.isawaitable(result):
            result = await result
        return "" if result is None else str(result)


__all__ = ["Command", "CommandContext", "CommandRegistry", "CommandHandler", "UnknownCommand"]
