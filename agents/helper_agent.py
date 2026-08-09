#!/usr/bin/env python3
"""ProjectOS helper agent -- lends this computer to the Pi.

*"tem um pc ligado, sem nada processando, e o rp ta sofrendo... se ele tiver com
o app baixado nele dale. suporte a windows mac e linux"*

Run it on the machine you want to lend:

    python3 helper_agent.py --pair http://projectos.local:8099 123456
    python3 helper_agent.py

The first form trades a pairing code for a token and writes it next to this
file; the second runs forever, asking the Pi for work.

Deliberately one file and standard library only. Windows, macOS and Linux
support is not three code paths -- it is the discipline of never reaching for
anything that is not in a stock Python install. There is nothing to `pip
install`, nothing to build, and no reason for the agent to break when something
else on the machine updates.

It talks HTTP, not the websocket, for the same reason: ``urllib`` is in the
standard library and a websocket client is not. Polling every fifteen seconds
costs nothing and survives a laptop lid closing, which a long-lived socket does
not.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONFIG_NAME = "projectos-helper.json"
DEFAULT_POLL = 15.0
#: Backoff when the Pi is unreachable. It is a Raspberry Pi -- it reboots, it
#: gets unplugged, someone re-flashes it. That is not a reason to give up or to
#: hammer the network.
RETRY_SECONDS = 30.0
HTTP_TIMEOUT = 30.0


# --------------------------------------------------------------------------- config


def config_path() -> str:
    override = os.environ.get("PROJECTOS_HELPER_CONFIG")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)


def load_config() -> dict:
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        print("could not read %s: %s" % (path, exc))
        return {}


def save_config(data: dict) -> None:
    path = config_path()
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2)
    # The token is a password in all but name.
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows does not do POSIX modes
        pass
    print("saved %s" % path)


# --------------------------------------------------------------------------- what this machine offers


def has(command: str) -> bool:
    return shutil.which(command) is not None


def capabilities() -> list:
    """Only claim what is actually installed.

    Claiming ``transcode`` without ffmpeg means the Pi hands over a job this
    machine will fail five times before giving up -- slower than never having
    offered.
    """
    found = ["cpu"]
    if has("ffmpeg"):
        found.append("transcode")
    if has("yt-dlp") or has("youtube-dl"):
        found.append("download")
    if free_gigabytes() >= 20:
        found.append("storage")
    if gpu_present():
        found.append("gpu")
    return found


def free_gigabytes() -> float:
    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
    except OSError:
        return 0.0
    return usage.free / (1024.0 ** 3)


def gpu_present() -> bool:
    """A conservative yes.

    Only reports a GPU when something can be asked about it -- a wrong yes here
    routes work to a machine that cannot do it.
    """
    if has("nvidia-smi"):
        return True
    if sys.platform == "darwin":
        # Apple Silicon always has one worth using.
        return platform.machine() in ("arm64", "aarch64")
    return False


def facts() -> dict:
    return {
        "hostname": platform.node(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cores": os.cpu_count() or 1,
        "free_gb": round(free_gigabytes(), 1),
        "ffmpeg": has("ffmpeg"),
    }


def default_name() -> str:
    return platform.node() or "ajudante"


# --------------------------------------------------------------------------- http


def request(url: str, payload=None, method: str = "POST", timeout: float = HTTP_TIMEOUT):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body else {}


def explain(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            return "%s: %s" % (detail.get("error", exc.code), detail.get("message", ""))
        except Exception:
            return "HTTP %s" % exc.code
    return str(exc)


# --------------------------------------------------------------------------- pairing


def pair(server: str, code: str, name: str = "", kind: str = "pc") -> int:
    server = server.rstrip("/")
    body = {
        "code": code,
        "name": name or default_name(),
        "kind": kind,
        "platform": "%s %s" % (platform.system(), platform.release()),
        "capabilities": capabilities(),
        "facts": facts(),
    }
    try:
        result = request(server + "/api/helpers/pair", body)
    except Exception as exc:
        print("pairing failed -- %s" % explain(exc))
        return 1
    save_config({"server": server, "token": result["token"], "helper": result["helper"]["id"]})
    print("paired as %r, offering: %s" % (
        result["helper"]["name"], ", ".join(result["helper"]["capabilities"]) or "nothing"
    ))
    return 0


# --------------------------------------------------------------------------- running jobs


def run_job(job: dict) -> tuple:
    """Do the work. Returns ``(result, error)`` -- exactly one is meaningful.

    Deliberately a short list. An agent that runs whatever the server sends is a
    remote shell, and a remote shell that authenticates with a token stored in
    plain text next to it is a bad trade for "the Pi can now convert videos".
    """
    kind = job.get("kind")
    payload = job.get("payload") or {}

    if kind == "ping":
        return {"pong": True, "host": platform.node()}, ""

    if kind == "facts":
        return facts(), ""

    if kind == "transcode":
        return transcode(payload)

    if kind == "download":
        return download(payload)

    return None, "este ajudante nao sabe fazer %r" % kind


def _run(command: list, timeout: float) -> tuple:
    try:
        finished = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout
        )
    except FileNotFoundError:
        return None, "%s nao esta instalado aqui" % command[0]
    except subprocess.TimeoutExpired:
        return None, "%s passou do tempo" % command[0]
    output = finished.stdout.decode("utf-8", "replace")[-4000:]
    if finished.returncode != 0:
        return None, output.strip() or "%s falhou" % command[0]
    return output, ""


def transcode(payload: dict) -> tuple:
    source = payload.get("input")
    target = payload.get("output")
    if not source or not target:
        return None, "faltou input/output"
    if not os.path.exists(source):
        return None, "nao achei %s -- o caminho precisa existir nesta maquina" % source
    command = ["ffmpeg", "-y", "-i", source]
    command += [str(part) for part in (payload.get("args") or [])]
    command.append(target)
    output, error = _run(command, float(payload.get("timeout", 3600)))
    if error:
        return None, error
    return {"output": target, "log": output[-1000:]}, ""


def download(payload: dict) -> tuple:
    url = payload.get("url")
    into = payload.get("into") or os.path.expanduser("~")
    if not url:
        return None, "faltou url"
    tool = "yt-dlp" if has("yt-dlp") else "youtube-dl"
    command = [tool, "--paths", into, url]
    output, error = _run(command, float(payload.get("timeout", 3600)))
    if error:
        return None, error
    return {"into": into, "log": output[-1000:]}, ""


# --------------------------------------------------------------------------- the loop


def serve(server: str, token: str) -> int:
    server = server.rstrip("/")
    poll = DEFAULT_POLL
    said_offline = False
    print("lending this machine to %s -- ctrl-c to stop" % server)

    while True:
        try:
            answer = request(
                server + "/api/helpers/agent/heartbeat",
                {"token": token, "facts": facts(), "capabilities": capabilities()},
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                print("this token is no longer valid -- pair again")
                return 1
            print("the Pi answered %s, trying again in %ds" % (exc.code, RETRY_SECONDS))
            time.sleep(RETRY_SECONDS)
            continue
        except Exception as exc:
            if not said_offline:
                # Said once, not every thirty seconds: an unreachable Pi should
                # not fill a terminal someone left open for a week.
                print("cannot reach the Pi (%s), will keep trying" % explain(exc))
                said_offline = True
            time.sleep(RETRY_SECONDS)
            continue

        said_offline = False
        poll = float(answer.get("poll_seconds") or DEFAULT_POLL)
        job = answer.get("job")
        if job is None:
            time.sleep(poll)
            continue

        print("job %s: %s" % (job["id"][:8], job["kind"]))
        started = time.time()
        try:
            result, error = run_job(job)
        except Exception as exc:  # a broken job must not take the agent down
            result, error = None, "%s: %s" % (type(exc).__name__, exc)
        took = time.time() - started

        try:
            request(
                "%s/api/helpers/agent/jobs/%s/done?token=%s" % (server, job["id"], token),
                {"result": result, "error": error},
            )
        except Exception as exc:
            print("could not report job %s -- %s" % (job["id"][:8], explain(exc)))
            continue
        print("  %s in %.1fs" % ("failed: %s" % error if error else "done", took))


# --------------------------------------------------------------------------- cli


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ProjectOS helper agent")
    parser.add_argument(
        "--pair", nargs=2, metavar=("SERVER", "CODE"),
        help="pair with a ProjectOS using a six-digit code from its Ajudantes screen",
    )
    parser.add_argument("--name", default="", help="what to call this machine")
    parser.add_argument("--kind", default="pc", choices=["pc", "pi", "other"])
    parser.add_argument("--show", action="store_true", help="print what this machine offers")
    args = parser.parse_args(argv)

    if args.show:
        print(json.dumps({"capabilities": capabilities(), "facts": facts()}, indent=2))
        return 0

    if args.pair:
        return pair(args.pair[0], args.pair[1], name=args.name, kind=args.kind)

    settings = load_config()
    if not settings.get("token"):
        print("not paired yet. run:\n  %s --pair http://projectos.local:8099 CODIGO"
              % os.path.basename(sys.argv[0]))
        return 1
    try:
        return serve(settings["server"], settings["token"])
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
