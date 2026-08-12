# project-os

**The system layer of a Raspberry Pi, driven entirely from a browser.**

Write the image to a card, put the card in a Raspberry Pi, and open its address
in a browser. No monitor, no keyboard, no terminal. Same shape as Home Assistant
OS — but general purpose. It hosts *your* things.

> Português: a documentação de conceito está em [docs/CONCEITO.md](docs/CONCEITO.md).

1. Download `project-os-<version>.img.xz` from
   [Releases](https://github.com/NspxMiguel/project-os/releases).
2. Write it to the card with [Balena Etcher](https://etcher.balena.io).
3. On Wi-Fi only: rename `project-os-wifi.txt.exemplo` to `project-os-wifi.txt` on
   the boot partition and put your network and password in it.
4. Boot the Pi and open **http://project-os.local**.

The first screen creates your account — and, with one checkbox, the machine's
SSH password too. The image ships with that Linux account *locked*: a public
image cannot carry a real password, because it would be printed in every copy of
this file. Until you finish that screen, the system answers nothing else — that
is deliberate.

The interface is in Portuguese.

Already have a Raspberry Pi OS you do not want to reflash? There is an installer
for that too: see [docs/INSTALL.md](docs/INSTALL.md).

---

## It arrives empty

project-os ships with no apps. None. What you get on first boot is a machine
panel, a store, and a login.

That is the whole design decision, and everything else follows from it:

> **If it can be removed, it is a plug-in. If it cannot, it is core.**

Core is small — login, the store, detection, the plug-in manager, the machine
panel, the terminal (present, off by default). Everything else — media servers,
smart home, bots, your own projects — arrives from the store and can leave the
same way.

## What it actually does

**Finds what you already own.** It scans the network with mDNS and a handful of
dependency-free probes, and tells you what it saw: phones, printers, a PC, an
Xbox, a PS5, a 3D printer, a TV, a Chromecast, an Apple TV. For each one it
knows about, it offers a recipe — the steps to make that device useful here.
Steps it can run, it runs with one click. Steps only a human can do, it writes
out honestly instead of pretending.

**Finds what is already installed.** Went to Advanced mode and installed Firefox,
a Flatpak store, a container? It shows up under Apps when you come back to
Simple mode. apt, flatpak, snap, `.desktop` entries, systemd units, containers.

**Two modes.** Simple is a dashboard. Advanced is the machine: services, files,
logs, a real terminal, fan curves, CPU governor, temperature.

**Borrows other computers.** Any machine that is on and idle can join as an
*ajudante* — a helper — and take work the Pi is too small for. A PC running
[`agents/helper_agent.py`](agents/helper_agent.py) (one file, stock Python, no
`pip install`, Windows/macOS/Linux), another Pi, or an ESP32 running
[`agents/esp32/main.py`](agents/esp32/main.py) as a sensor, a button or a relay
out in the house.

It is honest about what that is: a queue of whole tasks handed to another
machine, not process migration. Nothing here pretends to be magic.

**Hosts things.** Media servers, game servers, bots, whatever the store carries
and whatever you add to it. On a small Pi some of that will be slow. That is
your call to make, not the software's.

**Updates itself, and survives a bad one.** The card carries two systems, A and
B. An update writes the *other* one and reboots into it; the one that was
working is left untouched. A system that does not come back up — or comes up and
never answers — is given three tries and then the card boots the last one that
worked, on its own, from the initramfs. The card never has to come out of the
Pi. See [docs/RECOVERY.md](docs/RECOVERY.md).

## Requirements

- A Raspberry Pi with **1 GB of RAM or more** — 3B, 3B+, 4, 5, Zero 2 W, 400.
  512 MB boards are refused by the installer; see
  [docs/SIMPLE-PROJECT-OS.md](docs/SIMPLE-PROJECT-OS.md) for the ESP32 sibling.
- Raspberry Pi OS Lite (or any Debian/Ubuntu with systemd).
- Python 3.9 or newer.

It also runs fine on an ordinary Linux box, and on macOS for development.

## Extending it

Two of the most important files in the repo are not code:

- `project_os/data/catalog.yaml` — what the store offers.
- `project_os/data/recipes.yaml` — what to do with a device once it is found.

Both are plain text. Adding an app or teaching project-os about a device you own
is an edit and a pull request, not a plug-in you have to write.

For actual plug-ins, see [docs/APPS.md](docs/APPS.md).

## Development

```bash
git clone https://github.com/NspxMiguel/project-os.git
cd project-os
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,all]'
.venv/bin/python3 -m project_os --dev
```

```bash
.venv/bin/python3 -m pytest -q
```

The frontend has no build step. It is plain ES modules and one stylesheet —
edit `web/` and reload the page. This is on purpose: a Pi should not need a
toolchain to serve its own UI, and neither should you to change it.

Only three dependencies are hard: FastAPI, uvicorn and PyYAML (plus
`python-multipart` for uploads). Everything else is optional, imported lazily,
and when it is missing the screen that needed it says so and tells you the
install command.

## Documentation

| | |
|---|---|
| [docs/CONCEITO.md](docs/CONCEITO.md) | What project-os is, and why it is empty |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The build contract: modules, tables, events, routes |
| [docs/APPS.md](docs/APPS.md) | Writing a plug-in |
| [docs/HOME.md](docs/HOME.md) | Smart home: Home Assistant, Tuya, eWeLink and friends |
| [docs/BIRDTUNES.md](docs/BIRDTUNES.md) | The first app: music for birds, on an Apple TV or a Chromecast |
| [docs/SIMPLE-PROJECT-OS.md](docs/SIMPLE-PROJECT-OS.md) | The ESP32 build |
| [docs/RECOVERY.md](docs/RECOVERY.md) | Two systems on one card: updating, rolling back, and why the card stays in the Pi |
| [docs/ARGOS.md](docs/ARGOS.md) | Hooks for an external assistant |

## License

MIT. See [LICENSE](LICENSE).
