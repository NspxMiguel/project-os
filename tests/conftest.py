"""Shared fixtures.

Every test runs against a throwaway PROJECTOS_HOME and a fresh SQLite file, with no
network, no mDNS and no real speakers. The optional device libraries (zeroconf, pyatv,
pychromecast, yt_dlp) are faked at the import seam so the suite behaves identically on a
developer laptop, in CI, and on a Pi that has none of them installed.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------- home / config


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated PROJECTOS_HOME for one test."""
    h = tmp_path / "projectos-home"
    h.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROJECTOS_HOME", str(h))
    # Any PROJECTOS__* left over from the developer's shell would leak into the config.
    for key in list(os.environ):
        if key.startswith("PROJECTOS__"):
            monkeypatch.delenv(key, raising=False)
    # The dependency-free scanners (SSDP, ARP, Kasa, Tuya) talk to the actual
    # network, so they are off for the whole suite: a test whose result depends
    # on what happens to be plugged in at home is not a test. They have their own
    # coverage in tests/test_lan.py, against fed-in bytes.
    monkeypatch.setenv("PROJECTOS__DISCOVERY__EXTRA_SCANNERS", "false")
    _reset_modules()
    return h


def _reset_modules() -> None:
    """Drop cached projectos modules so paths/config re-read the new environment."""
    for name in [n for n in sys.modules if n == "projectos" or n.startswith("projectos.")]:
        del sys.modules[name]


@pytest.fixture()
def config(home: Path):
    from projectos.config import load_config

    return load_config()


@pytest.fixture()
def db(home: Path):
    from projectos.db import Database
    from projectos import paths

    database = Database(paths.db_file())
    database.migrate()
    yield database
    try:
        database.close()
    except Exception:  # pragma: no cover - close() is best effort
        pass


# --------------------------------------------------------------------------- fake devices


class FakeServiceInfo:
    """Stands in for zeroconf.ServiceInfo."""

    def __init__(
        self,
        type_: str,
        name: str,
        addresses: Optional[List[bytes]] = None,
        port: int = 7000,
        properties: Optional[Dict[bytes, bytes]] = None,
        server: str = "device.local.",
    ) -> None:
        self.type = type_
        self.name = name
        self.port = port
        self.server = server
        self.properties = properties or {}
        self._addresses = addresses or [bytes([192, 168, 1, 50])]

    def parsed_addresses(self) -> List[str]:
        return [".".join(str(b) for b in addr) for addr in self._addresses]

    def addresses_by_version(self, _version: Any) -> List[bytes]:
        return self._addresses


@pytest.fixture()
def apple_tv_service() -> FakeServiceInfo:
    """An Apple TV 4K as it actually announces itself over _airplay._tcp."""
    return FakeServiceInfo(
        "_airplay._tcp.local.",
        "Living Room._airplay._tcp.local.",
        properties={
            b"deviceid": b"AA:BB:CC:DD:EE:FF",
            b"model": b"AppleTV14,1",
            b"am": b"AppleTV14,1",
            b"srcvers": b"770.8.1",
            b"features": b"0x4A7FDFD5,0xBC157FDE",
        },
    )


@pytest.fixture()
def homepod_service() -> FakeServiceInfo:
    """A HomePod mini announces AudioAccessory5,1 over _raop._tcp."""
    return FakeServiceInfo(
        "_raop._tcp.local.",
        "AABBCCDDEEFF@Kitchen._raop._tcp.local.",
        port=7000,
        properties={
            b"am": b"AudioAccessory5,1",
            b"deviceid": b"AA:BB:CC:DD:EE:01",
            b"ch": b"2",
            b"sr": b"44100",
        },
    )


@pytest.fixture()
def chromecast_service() -> FakeServiceInfo:
    return FakeServiceInfo(
        "_googlecast._tcp.local.",
        "Chromecast-abc123._googlecast._tcp.local.",
        port=8009,
        properties={
            b"md": b"Chromecast Audio",
            b"fn": b"Bedroom speaker",
            b"id": b"abc123def456",
            b"ca": b"4101",
        },
    )


@pytest.fixture()
def home_assistant_service() -> FakeServiceInfo:
    return FakeServiceInfo(
        "_home-assistant._tcp.local.",
        "Home._home-assistant._tcp.local.",
        port=8123,
        properties={b"base_url": b"http://homeassistant.local:8123", b"version": b"2026.7.1"},
    )


# --------------------------------------------------------------------------- fake yt-dlp


class FakeYoutubeDL:
    """Records what it was asked to do and produces plausible metadata. No network."""

    instances: List["FakeYoutubeDL"] = []
    # Tests set this to control what extract_info returns.
    result: Dict[str, Any] = {}
    downloaded: List[str] = []
    fail_ids: List[str] = []

    def __init__(self, params: Optional[Dict[str, Any]] = None) -> None:
        self.params = params or {}
        FakeYoutubeDL.instances.append(self)

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> Dict[str, Any]:
        info = dict(FakeYoutubeDL.result or _default_video_info(url))
        if download:
            entries = info.get("entries") or [info]
            for entry in entries:
                if not entry:
                    continue
                if entry.get("id") in FakeYoutubeDL.fail_ids:
                    continue
                FakeYoutubeDL.downloaded.append(entry.get("id", ""))
        return info

    def prepare_filename(self, info: Dict[str, Any]) -> str:
        return "%s [%s].%s" % (info.get("title", "track"), info.get("id", "x"), info.get("ext", "m4a"))

    def close(self) -> None:
        return None


def _default_video_info(url: str) -> Dict[str, Any]:
    return {
        "id": "dQw4w9WgXcQ",
        "title": "A Calm Melody",
        "uploader": "Some Channel",
        "duration": 212.0,
        "ext": "m4a",
        "acodec": "mp4a.40.2",
        "webpage_url": url,
        "thumbnail": "",
    }


@pytest.fixture()
def fake_ytdl(monkeypatch: pytest.MonkeyPatch) -> Iterator[type]:
    FakeYoutubeDL.instances = []
    FakeYoutubeDL.downloaded = []
    FakeYoutubeDL.fail_ids = []
    FakeYoutubeDL.result = {}

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    module.version = types.SimpleNamespace(__version__="0.0.0-test")  # type: ignore[attr-defined]

    class DownloadError(Exception):
        pass

    utils = types.ModuleType("yt_dlp.utils")
    utils.DownloadError = DownloadError  # type: ignore[attr-defined]
    module.utils = utils  # type: ignore[attr-defined]
    module.DownloadError = DownloadError  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", utils)
    yield FakeYoutubeDL


# --------------------------------------------------------------------------- app / client


@pytest.fixture()
def app(home: Path):
    from projectos.main import create_app

    return create_app()


@pytest.fixture()
def client(app) -> Iterator[Any]:
    """TestClient with the lifespan actually run, so app.state is populated."""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


ADMIN = {"username": "miguel", "password": "correct horse battery staple"}


@pytest.fixture()
def auth_client(client) -> Any:
    """A client that has completed first-run setup and holds a session cookie."""
    response = client.post("/api/setup", json=ADMIN)
    assert response.status_code in (200, 201), response.text
    login = client.post("/api/auth/login", json=ADMIN)
    assert login.status_code == 200, login.text
    return client


@pytest.fixture()
def media_dir(home: Path) -> Path:
    d = home / "media" / "birdtunes"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def sample_tracks(media_dir: Path) -> List[Path]:
    """Real, tiny, valid WAV files - enough for a library scan to find something."""
    import struct
    import wave

    made: List[Path] = []
    for index, name in enumerate(["lullaby.wav", "morning.wav", "forest-calm.wav"]):
        path = media_dir / name
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            frames = b"".join(
                struct.pack("<h", int(3000 * ((i * (index + 1)) % 64 - 32) / 32))
                for i in range(800)
            )
            handle.writeframes(frames)
        made.append(path)
    return made
