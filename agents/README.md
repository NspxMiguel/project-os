# Ajudantes

> "tem um pc ligado, sem nada processando, e o rp ta sofrendo... se ele tiver com
> o app baixado nele dale. suporte a windows mac e linux"
>
> "o esp32 tbm n precisa ser só um sistema, poderia ser tbm algo q ajuda o rp,
> algum acessorio"

An *ajudante* is any machine that joined a project-os to lend it something it
does not have. A PC lends CPU, a GPU, a fast disk. An ESP32 lends a temperature
reading in another room, a button by the door, a relay on a lamp.

Both halves are here, and both talk the same protocol.

| | |
|---|---|
| [`helper_agent.py`](helper_agent.py) | Windows, macOS, Linux. One file, stock Python, nothing to install |
| [`esp32/main.py`](esp32/main.py) | MicroPython on an ESP32 |

---

## What this is, precisely

A queue of whole tasks handed to another machine.

It is **not** process migration and it is not a cluster. Nothing running on the
Pi moves anywhere. The Pi says "somebody convert this video", a machine that can
takes it, does it, and reports back. That is the whole idea, and saying it
plainly beats a screen that promises magic.

Jobs are routed by capability. A helper only ever receives work it declared it
can do, and a helper only declares what the machine actually has — the PC agent
checks for `ffmpeg` before claiming it can transcode, because claiming it and
failing five times is slower than never having offered.

## How the connection works

The helper dials the Pi. Always outward, never inward.

So: no port forwarding, no firewall rule, no fixed address for your laptop. A
laptop whose lid closes simply stops talking, and the Pi requeues whatever it
was holding.

Pairing is a six-digit code from the Ajudantes screen, good for ten minutes and
usable once. It is traded for a token that is stored hashed on the Pi and in a
`0600` file on the helper. The token is shown exactly once.

## A PC

Python 3 is all it needs — no `pip install`, nothing to build. Copy the file to
the machine you want to lend, get a code from the Ajudantes screen, and:

```bash
python3 helper_agent.py --pair http://project-os.local:8099 123456
python3 helper_agent.py
```

To see what this machine is offering before pairing:

```bash
python3 helper_agent.py --show
```

| Capability | Claimed when |
|---|---|
| `cpu` | always |
| `transcode` | `ffmpeg` is on PATH |
| `download` | `yt-dlp` or `youtube-dl` is on PATH |
| `storage` | at least 20 GB free in the home directory |
| `gpu` | `nvidia-smi` exists, or it is an Apple Silicon Mac |

The job kinds it will run are a short, closed list: `ping`, `facts`,
`transcode`, `download`. That list is short on purpose. An agent that runs
whatever the server sends is a remote shell, and a remote shell authenticated by
a token in a plain file next to it is a bad trade for "the Pi can convert videos
now".

### Leaving it running

**Linux** — a user unit, so it stops when you log out and starts when you log in:

```ini
# ~/.config/systemd/user/project-os-helper.service
[Unit]
Description=project-os helper
[Service]
ExecStart=/usr/bin/python3 %h/project_os/helper_agent.py
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now project-os-helper
```

**macOS** — a LaunchAgent in `~/Library/LaunchAgents/` with `RunAtLoad` and
`KeepAlive`.

**Windows** — Task Scheduler, "at log on", running
`pythonw.exe helper_agent.py` so no console window appears.

## An ESP32

Flash MicroPython, copy [`esp32/main.py`](esp32/main.py) to the board as
`main.py`, and edit the settings block at the top: Wi-Fi, the project-os address,
a pairing code, and which pins are actually wired.

On the first boot it prints the token it received. Paste that into `TOKEN` so it
survives a reboot.

An ESP32 may claim `sensor`, `actuator`, `display`, `button`, `infrared` and
`audio`. The Pi refuses anything else from a board of that kind — a board that
declares a GPU would be handed a transcode job it will never finish.

It heartbeats over HTTP rather than holding a websocket open, because a board
that has to keep a socket alive cannot deep-sleep, and a sensor that cannot
sleep is a sensor with a cable running to it.

`read_sensor()` returns a raw ADC value. That is a deliberate placeholder: it
needs no driver and it is obviously not a temperature, which beats a number that
is quietly wrong because your board is not the one this was written for. Replace
it with your sensor's library.

## Writing your own

Three HTTP calls, all under `/api/helpers`:

| | |
|---|---|
| `POST /pair` | `{code, name, kind, platform, capabilities, facts}` → `{helper, token}` |
| `POST /agent/heartbeat` | `{token, capabilities, facts}` → `{poll_seconds, job}` — `job` is `null` when there is nothing to do |
| `POST /agent/jobs/{id}/done?token=` | `{result, error}` — exactly one of the two is meaningful |

There is also `WS /agent/link?token=` for anything that can hold a socket open
and wants a job the moment it exists rather than at the next poll.

Kinds are `pc`, `pi`, `esp32` and `other`. Capabilities are `cpu`, `gpu`,
`transcode`, `download`, `storage`, `sensor`, `actuator`, `display`, `button`,
`infrared`, `audio` — the authoritative list lives in
[`project_os/core/helpers.py`](../project_os/core/helpers.py), along with which
kinds may claim what.
