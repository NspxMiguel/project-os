# ProjectOS

**The system layer of a Raspberry Pi, driven entirely from a browser.**

Flash Raspberry Pi OS Lite, run one command, and the Pi becomes a headless
appliance you operate from any machine on your network. Same shape as Home
Assistant OS — but general purpose. It hosts *your* things.

> Português: a documentação de conceito está em [docs/CONCEITO.md](docs/CONCEITO.md).

```bash
curl -fsSL https://raw.githubusercontent.com/NspxMiguel/ProjectOS/main/install.sh | sudo bash
```

Then open `http://raspberrypi.local:8099` and create the first account. Until
you do, the system answers nothing else — that is deliberate.

---

## It arrives empty

ProjectOS ships with no apps. None. What you get on first boot is a machine
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

## Requirements

- A Raspberry Pi with **1 GB of RAM or more** — 3B, 3B+, 4, 5, Zero 2 W, 400.
  512 MB boards are refused by the installer; see
  [docs/SIMPLEPROJECTOS.md](docs/SIMPLEPROJECTOS.md) for the ESP32 sibling.
- Raspberry Pi OS Lite (or any Debian/Ubuntu with systemd).
- Python 3.9 or newer.

It also runs fine on an ordinary Linux box, and on macOS for development.

## Extending it

Two of the most important files in the repo are not code:

- `projectos/data/catalog.yaml` — what the store offers.
- `projectos/data/recipes.yaml` — what to do with a device once it is found.

Both are plain text. Adding an app or teaching ProjectOS about a device you own
is an edit and a pull request, not a plug-in you have to write.

For actual plug-ins, see [docs/APPS.md](docs/APPS.md).

## Development

```bash
git clone https://github.com/NspxMiguel/ProjectOS.git
cd ProjectOS
python3 -m venv .venv
.venv/bin/pip install -e '.[dev,all]'
.venv/bin/python3 -m projectos --dev
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
| [docs/CONCEITO.md](docs/CONCEITO.md) | What ProjectOS is, and why it is empty |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The build contract: modules, tables, events, routes |
| [docs/APPS.md](docs/APPS.md) | Writing a plug-in |
| [docs/HOME.md](docs/HOME.md) | Smart home: Home Assistant, Tuya, eWeLink and friends |
| [docs/BIRDTUNES.md](docs/BIRDTUNES.md) | The first app: music for birds, on an Apple TV or a Chromecast |
| [docs/SIMPLEPROJECTOS.md](docs/SIMPLEPROJECTOS.md) | The ESP32 build |
| [docs/ARGOS.md](docs/ARGOS.md) | Hooks for an external assistant |

## License

MIT. See [LICENSE](LICENSE).
