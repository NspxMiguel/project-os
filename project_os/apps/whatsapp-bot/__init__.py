"""whatsapp-bot -- project-os's own line into WhatsApp.

WhatsApp has no free, official, self-hostable API. Meta's Cloud API is real
and stable but wants a business account and, past a small free tier, money;
the community's unofficial web clients (whatsapp-web.js, Baileys, WPPConnect)
are free but automate a phone's identity, and Meta bans numbers it catches
doing that. Neither trade-off is this app's to make for the user, so the app
is a provider seam (see :mod:`providers`) with three real backends plus a
null provider that logs instead of failing -- the app installs, starts and is
fully testable with nothing configured at all.

Layout::

    manifest.json    id, entrypoint (app:setup), config defaults
    app.py           setup(ctx) -> AppInstance: HTTP routes, inbound dispatch
    commands.py      the command registry -- built-ins plus what other apps add
    ratelimit.py     per-contact fixed-window limiter
    providers/       base.py, null.py, cloud_api.py, bridge.py
    web/panel.js     the project-os-hosted control panel

Security decisions worth naming up front, because they are easy to weaken by
accident later:

* The allowlist is on and empty by default. A bot that answers strangers the
  moment it is installed is a liability that lives in someone's pocket; the
  owner has to name who it talks to before it talks to anyone.
* The Cloud API webhook checks ``X-Hub-Signature-256`` with
  ``hmac.compare_digest`` before the body is trusted. Skipping that turns the
  webhook into an open relay into whoever's WhatsApp is configured.
* Every credential-shaped config key (``access_token``, ``verify_token``,
  ``app_secret``, ``bridge.token``) is named so it matches
  ``project_os.config.is_secret_key`` and comes back redacted from
  ``GET .../config`` -- nothing here invents its own secret handling.
"""

from __future__ import annotations

APP_ID = "whatsapp-bot"

__all__ = ["APP_ID"]
