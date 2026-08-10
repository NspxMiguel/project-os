"""``python -m project_os`` -- run the server.

Command-line options are translated into the environment layer of the config
(``PROJECT_OS__SERVER__PORT`` and friends), so a flag, a config file entry and an
environment variable all mean the same thing and follow the same precedence.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from project_os import __version__, paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project_os",
        description="project-os -- the system layer of a Raspberry Pi, in a browser.",
    )
    parser.add_argument("--host", help="bind address (default: server.host, 0.0.0.0)")
    parser.add_argument("--port", type=int, help="bind port (default: server.port, 8099)")
    parser.add_argument(
        "--home",
        help="state directory (default: $PROJECT_OS_HOME or ~/.project_os)",
    )
    parser.add_argument(
        "--dev",
        "--reload",
        dest="dev",
        action="store_true",
        help="reload on source changes (development only)",
    )
    parser.add_argument("--log-level", dest="log_level", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--version", action="version", version="project_os %s" % __version__)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Snapshotted before anything can touch sys.argv: an update that is not
    # under systemd restarts by re-executing this exact command line, and
    # getting it wrong means the box comes back up differently than it went
    # down (wrong port, wrong home) or not at all.
    from project_os.core import updates as _updates

    _updates.remember_argv(sys.argv)

    if args.home:
        os.environ[paths.HOME_ENV] = str(Path(args.home).expanduser())
    if args.host:
        os.environ["PROJECT_OS__SERVER__HOST"] = str(args.host)
    if args.port:
        os.environ["PROJECT_OS__SERVER__PORT"] = str(args.port)
    if args.log_level:
        os.environ["PROJECT_OS__LOGGING__LEVEL"] = str(args.log_level).upper()

    from project_os.config import load_config

    config = load_config()
    host = str(config.get("server.host", "0.0.0.0"))
    port = int(config.get("server.port", 8099))
    log_level = str(config.get("logging.level", "INFO")).lower()

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write(
            "uvicorn is not installed. Run: pip install 'uvicorn[standard]'\n"
        )
        return 1

    options = {
        "factory": True,
        "host": host,
        "port": port,
        "log_level": log_level,
        "workers": 1,  # one process: this runs on a 1 GB Pi
        "reload": bool(args.dev),
        "access_log": bool(args.dev),
    }
    if args.dev:
        options["reload_dirs"] = [str(paths.package_dir())]

    try:
        uvicorn.run("project_os.main:create_app", **options)
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
