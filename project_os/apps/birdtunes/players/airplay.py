"""AirPlay output through pyatv 0.18 -- Apple TV, HomePod, AirPort speakers.

The important quirk of this backend is that pyatv's two playback calls,
``stream.stream_file()`` and ``stream.play_url()``, **do not return until the
track has finished**: the Apple TV keeps the RTSP/HTTP request open for the
whole duration. So playback runs as an :mod:`asyncio` task whose completion
*is* the end-of-track signal, and stopping means cancelling that task and
closing the stream.

Credentials from pairing live in ``<data_dir>/pyatv.json`` via pyatv's own
``FileStorage``, which is handed to every ``scan``/``connect``/``pair`` call so
a device paired once stays paired across reboots.
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import (
    PlaybackState,
    Player,
    PlayerError,
    REASON_ERROR,
    REASON_FINISHED,
    REASON_STOPPED,
    Track,
    clamp_volume,
    device_field,
    device_label,
    device_properties,
)

INSTALL_HINT = "pip install pyatv (AirPlay support is an optional dependency)"

#: Device kinds from core discovery this backend can drive.
DEVICE_KINDS = ["apple_tv", "airplay", "airplay_speaker", "homepod", "raop"]

#: Pairing protocols we let the UI ask for, lowest friction first.
PAIRING_PROTOCOLS = ("airplay", "raop", "companion")

SCAN_TIMEOUT = 5
CONNECT_TIMEOUT = 20.0


def _pyatv() -> Any:
    """Import pyatv (and the submodules we touch) or explain how to get it."""
    try:
        import pyatv
        import pyatv.const  # noqa: F401
        import pyatv.interface  # noqa: F401
        import pyatv.storage.file_storage  # noqa: F401
        import pyatv.storage.memory_storage  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the box
        raise PlayerError(
            "pyatv is not installed, so AirPlay output is unavailable.",
            code="pyatv_missing",
            hint=INSTALL_HINT,
            retryable=False,
        ) from exc
    return pyatv


class AirPlayPlayer(Player):
    """Streams a file to an AirPlay receiver and reports when it ends."""

    name = "AirPlay"
    kind = "airplay"
    device_kinds = DEVICE_KINDS
    #: RAOP streaming has no pause; we fall back to the remote control when
    #: the device exposes one, and say so honestly when it does not.
    can_pause = True
    needs_media_url = False

    def __init__(self, ctx: Any = None) -> None:
        super().__init__(ctx)
        self._atv = None  # type: Any
        self._config = None  # type: Any
        self._storage = None  # type: Any
        self._stream_task = None  # type: Optional[asyncio.Future]
        self._stopping = False
        self._mode = ""
        self._pairing = None  # type: Optional[Dict[str, Any]]

    # -- availability ----------------------------------------------------
    @classmethod
    def available(cls) -> Tuple[bool, str]:
        import importlib.util

        try:
            spec = importlib.util.find_spec("pyatv")
        except (ImportError, ValueError):  # pragma: no cover - broken install
            spec = None
        if spec is None:
            return False, INSTALL_HINT
        return True, ""

    # -- pyatv plumbing --------------------------------------------------
    async def _get_storage(self) -> Any:
        """The credential store, created and loaded once per player."""
        if self._storage is not None:
            return self._storage
        pyatv = _pyatv()
        loop = asyncio.get_event_loop()
        data_dir = getattr(self.ctx, "data_dir", None)
        if data_dir is None:
            self._storage = pyatv.storage.memory_storage.MemoryStorage()
            return self._storage
        path = os.path.join(str(data_dir), "pyatv.json")
        storage = pyatv.storage.file_storage.FileStorage(path, loop)
        try:
            await storage.load()
        except Exception as exc:
            # A corrupt credential file must not make the app unstartable;
            # the user can re-pair, which rewrites it.
            self.log.warning("could not load %s (%s); starting empty", path, exc)
        self._storage = storage
        return self._storage

    async def _save_storage(self) -> None:
        storage = self._storage
        if storage is None:
            return
        try:
            await storage.save()
        except Exception as exc:  # pragma: no cover - disk full, read-only fs
            self.log.warning("could not persist AirPlay credentials: %s", exc)

    async def _scan(
        self,
        timeout: int = SCAN_TIMEOUT,
        identifier: Optional[str] = None,
        hosts: Optional[List[str]] = None,
    ) -> List[Any]:
        pyatv = _pyatv()
        loop = asyncio.get_event_loop()
        storage = await self._get_storage()
        protocols = {pyatv.const.Protocol.RAOP, pyatv.const.Protocol.AirPlay}  # type: Set[Any]
        kwargs = {
            "timeout": int(max(1, timeout)),
            "protocol": protocols,
            "storage": storage,
        }  # type: Dict[str, Any]
        if identifier:
            kwargs["identifier"] = identifier
        if hosts:
            kwargs["hosts"] = list(hosts)
        try:
            return await pyatv.scan(loop, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PlayerError(
                "AirPlay scan failed",
                code="airplay_scan_failed",
                hint="Veja se o Pi está na mesma rede (e VLAN) da caixa de som.",
            ) from exc

    @staticmethod
    def _identifier_candidates(device: Any) -> List[str]:
        """Every string that might be the pyatv identifier for this device."""
        out = []  # type: List[str]
        props = device_properties(device)
        for key in ("deviceid", "id", "uuid", "psi", "pk", "serialnumber"):
            value = props.get(key)
            if value:
                out.append(str(value))
        raw_id = device_field(device, "id")
        if raw_id:
            # Registry ids look like "<kind>:<unique>"; pyatv wants the unique.
            out.append(raw_id.split(":", 1)[-1] if ":" in raw_id else raw_id)
        for key in ("identifier", "uuid", "mac"):
            value = device_field(device, key)
            if value:
                out.append(value)
        seen = set()  # type: Set[str]
        unique = []  # type: List[str]
        for item in out:
            lowered = item.lower()
            if lowered and lowered not in seen:
                seen.add(lowered)
                unique.append(item)
        return unique

    async def _resolve(self, device: Any, timeout: int = SCAN_TIMEOUT) -> Any:
        """Find the pyatv config for ``device``, or say why we cannot."""
        if isinstance(device, str):
            device = self._lookup_device(device)
        address = device_field(device, "address", "host", "ip")
        identifiers = self._identifier_candidates(device)
        wanted = set(item.lower() for item in identifiers)
        name = device_label(device)

        found = []  # type: List[Any]
        if address:
            found = await self._scan(timeout=timeout, hosts=[address])
        if not found:
            found = await self._scan(timeout=timeout)
        if not found:
            raise PlayerError(
                "No AirPlay receiver answered at %s." % (address or name),
                code="airplay_not_found",
                hint="Veja se a caixa de som está ligada e nesta rede.",
            )

        if wanted:
            for config in found:
                known = set(str(i).lower() for i in (config.all_identifiers or []))
                if known & wanted:
                    return config
        if address:
            for config in found:
                if str(config.address) == address:
                    return config
        lowered_name = name.lower()
        for config in found:
            if str(config.name or "").lower() == lowered_name:
                return config
        if len(found) == 1:
            return found[0]
        raise PlayerError(
            "Could not match %s to any AirPlay receiver on the network." % name,
            code="airplay_not_found",
            hint="Rode a busca de aparelhos de novo e escolha a saída outra vez.",
        )

    def _lookup_device(self, device_id: str) -> Any:
        registry = getattr(self.ctx, "devices", None)
        if registry is not None:
            try:
                found = registry.get(device_id)
            except Exception:  # pragma: no cover - registry is best effort
                found = None
            if found:
                return found
        return {"id": device_id, "name": device_id}

    # -- connection ------------------------------------------------------
    async def connect(self, device: Any) -> None:
        pyatv = _pyatv()
        await self.disconnect()
        self._set_state(PlaybackState.CONNECTING)
        self.device = device
        loop = asyncio.get_event_loop()
        try:
            config = await self._resolve(device)
            storage = await self._get_storage()
            self._atv = await asyncio.wait_for(
                pyatv.connect(config, loop, storage=storage), CONNECT_TIMEOUT
            )
            self._config = config
        except PlayerError as exc:
            self._fail(exc.message)
            raise
        except asyncio.TimeoutError as exc:
            message = self._fail("Timed out connecting to %s" % device_label(device))
            raise PlayerError(
                message,
                code="airplay_timeout",
                hint="O aparelho pode estar dormindo; tente de novo ou acorde ele pelo controle.",
            ) from exc
        except asyncio.CancelledError:
            self._set_state(PlaybackState.IDLE)
            raise
        except Exception as exc:
            message = self._fail("Could not connect to %s" % device_label(device), exc)
            hint = ""
            name = type(exc).__name__
            if "Auth" in name or "Credentials" in name:
                hint = "Pair this speaker first: Outputs -> Pair, then enter the PIN."
            raise PlayerError(message, code="airplay_connect_failed", hint=hint) from exc
        self._set_state(PlaybackState.IDLE)
        self.log.info("AirPlay connected to %s", device_label(device))

    @property
    def connected(self) -> bool:
        return self._atv is not None

    def _feature_state(self, feature_name: str) -> str:
        """``Available`` / ``Unsupported`` / ``Unknown`` for a pyatv feature."""
        if self._atv is None:
            return "Unknown"
        try:
            pyatv = _pyatv()
            name = getattr(pyatv.const.FeatureName, feature_name)
            return self._atv.features.get_feature(name).state.name
        except Exception:  # pragma: no cover - older/odd devices
            return "Unknown"

    # -- playback --------------------------------------------------------
    async def play(self, track: Any, url: str = "") -> None:
        if self._atv is None:
            raise PlayerError(
                "Not connected to an AirPlay speaker.",
                code="not_connected",
                hint="Pick an output device in BirdTunes settings.",
            )
        pyatv = _pyatv()
        item = Track.coerce(track)
        loop = asyncio.get_event_loop()

        has_file = False
        if item.path:
            has_file = await loop.run_in_executor(None, os.path.isfile, item.path)
        can_stream_file = self._feature_state("StreamFile") != "Unsupported"

        target = ""
        mode = ""
        if has_file and can_stream_file:
            target, mode = item.path, "stream_file"
        else:
            target = url or item.url
            mode = "play_url"
            if not target:
                raise PlayerError(
                    "Track %s has no playable file or URL." % (item.label,),
                    code="track_unplayable",
                    hint="Releia a biblioteca; o arquivo pode ter mudado de lugar.",
                )

        metadata = None
        try:
            metadata = pyatv.interface.MediaMetadata(
                title=item.title or item.label,
                artist=item.artist or None,
                album=item.album or None,
                duration=item.duration or None,
            )
        except Exception:  # pragma: no cover - metadata is a nicety
            metadata = None

        await self._cancel_stream(REASON_STOPPED, notify=False)
        self._mode = mode
        self._stopping = False
        self._begin_playback(item)
        self._stream_task = asyncio.ensure_future(self._run_stream(target, mode, metadata))
        self.log.info("AirPlay %s: %s", mode, item.label)

    async def _run_stream(self, target: str, mode: str, metadata: Any) -> None:
        """Own the whole track: pyatv only returns when it has finished."""
        try:
            if mode == "stream_file":
                await self._atv.stream.stream_file(target, metadata=metadata)
            else:
                await self._atv.stream.play_url(target)
        except asyncio.CancelledError:
            self._finish_playback(REASON_STOPPED)
            raise
        except Exception as exc:
            message = "AirPlay playback failed"
            detail = str(exc).strip()
            if detail:
                message = "%s: %s" % (message, detail)
            self.log.warning("%s", message)
            self.log.debug("airplay stream traceback", exc_info=True)
            self._finish_playback(REASON_ERROR, message)
            return
        if self._stopping:
            self._finish_playback(REASON_STOPPED)
        else:
            self._finish_playback(REASON_FINISHED)

    async def _cancel_stream(self, reason: str, notify: bool = True) -> None:
        task, self._stream_task = self._stream_task, None
        if task is None:
            return
        self._stopping = True
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - already reported by _run_stream
                pass
        if notify:
            self._finish_playback(reason)

    async def stop(self) -> None:
        self._stopping = True
        await self._cancel_stream(REASON_STOPPED)
        atv = self._atv
        if atv is not None:
            try:
                atv.stream.close()
            except Exception as exc:  # pragma: no cover - device already gone
                self.log.debug("stream.close() failed: %s", exc)
        self._set_state(PlaybackState.STOPPED)

    async def pause(self) -> None:
        """RAOP has no pause; use the remote control when the device has one."""
        if self._atv is None:
            raise PlayerError("Not connected to an AirPlay speaker.", code="not_connected")
        if self._feature_state("Pause") == "Available":
            try:
                await self._atv.remote_control.pause()
            except Exception as exc:
                raise PlayerError(
                    "Could not pause %s" % device_label(self.device),
                    code="airplay_pause_failed",
                ) from exc
            self._mark_paused()
            return
        raise PlayerError(
            "This AirPlay speaker cannot pause a stream; stopping instead is the only option.",
            code="pause_unsupported",
            hint="Use Parar e depois Tocar para começar a faixa de novo.",
            retryable=False,
        )

    async def resume(self) -> None:
        if self._atv is None:
            raise PlayerError("Not connected to an AirPlay speaker.", code="not_connected")
        if self._feature_state("Play") == "Available":
            try:
                await self._atv.remote_control.play()
            except Exception as exc:
                raise PlayerError(
                    "Could not resume %s" % device_label(self.device),
                    code="airplay_resume_failed",
                ) from exc
            self._mark_resumed()
            return
        raise PlayerError(
            "This AirPlay speaker cannot resume a stream.",
            code="resume_unsupported",
            hint="Aperte Tocar para começar a faixa do início.",
            retryable=False,
        )

    async def set_volume(self, level: float) -> None:
        value = clamp_volume(level, self.volume)
        self.volume = value
        if self._atv is None:
            return
        try:
            # pyatv speaks 0-100, BirdTunes speaks 0.0-1.0.
            await self._atv.audio.set_volume(value * 100.0)
        except Exception as exc:
            raise PlayerError(
                "Could not set the volume on %s" % device_label(self.device),
                code="airplay_volume_failed",
            ) from exc

    async def disconnect(self) -> None:
        await self._cancel_stream(REASON_STOPPED, notify=False)
        await self._close_pairing()
        atv, self._atv = self._atv, None
        self._config = None
        if atv is None:
            self._set_state(PlaybackState.IDLE)
            return
        try:
            result = atv.close()
        except Exception as exc:  # pragma: no cover - already unreachable
            self.log.debug("atv.close() failed: %s", exc)
            result = None
        # close() is synchronous in 0.18 and hands back the tasks it cancelled,
        # but tolerate a coroutine in case that changes.
        try:
            if asyncio.iscoroutine(result):
                await result
            elif result:
                await asyncio.gather(*list(result), return_exceptions=True)
        except Exception:  # pragma: no cover
            pass
        self._set_state(PlaybackState.IDLE)

    # -- discovery -------------------------------------------------------
    async def discover(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """AirPlay receivers visible right now, for ``GET /outputs``."""
        try:
            found = await self._scan(timeout=int(max(1, timeout)))
        except PlayerError:
            return []
        return [self._describe_config(config) for config in found]

    @staticmethod
    def _describe_config(config: Any) -> Dict[str, Any]:
        info = getattr(config, "device_info", None)
        model = ""
        if info is not None:
            raw = getattr(info, "raw_model", "") or ""
            model = str(raw or getattr(info, "model", "") or "")
        services = []  # type: List[str]
        paired = False
        for service in getattr(config, "services", []) or []:
            protocol = getattr(getattr(service, "protocol", None), "name", "")
            if protocol:
                services.append(protocol)
            if getattr(service, "credentials", None):
                paired = True
        return {
            "id": str(config.identifier or ""),
            "name": str(config.name or ""),
            "address": str(config.address or ""),
            "model": model,
            "protocols": services,
            "paired": paired,
            "identifiers": [str(i) for i in (config.all_identifiers or [])],
            "kind": "apple_tv" if "AudioAccessory" not in model else "homepod",
        }

    # -- pairing ---------------------------------------------------------
    async def begin_pairing(self, device: Any, protocol: str = "airplay") -> Dict[str, Any]:
        """Start pairing and tell the UI what to ask the user for.

        Two shapes exist. Either the speaker shows a PIN (``needs_pin``: the
        user types it and we call :meth:`finish_pairing`), or *we* pick the
        PIN and the user types it into the speaker -- in which case the code
        to display comes back in ``pin``.
        """
        pyatv = _pyatv()
        name = str(protocol or "airplay").lower()
        if name not in PAIRING_PROTOCOLS:
            raise PlayerError(
                "Unknown pairing protocol %r." % protocol,
                code="bad_protocol",
                hint="Use one of: %s." % ", ".join(PAIRING_PROTOCOLS),
                retryable=False,
            )
        await self._close_pairing()
        loop = asyncio.get_event_loop()
        config = await self._resolve(device)
        storage = await self._get_storage()
        proto = {
            "airplay": pyatv.const.Protocol.AirPlay,
            "raop": pyatv.const.Protocol.RAOP,
            "companion": pyatv.const.Protocol.Companion,
        }[name]
        try:
            handler = await pyatv.pair(config, proto, loop, storage=storage)
            await handler.begin()
        except Exception as exc:
            raise PlayerError(
                "Could not start pairing with %s" % device_label(device),
                code="pairing_failed",
                hint="Veja se a caixa de som está acordada e tente de novo.",
            ) from exc

        provides_pin = bool(getattr(handler, "device_provides_pin", True))
        shown = ""
        if not provides_pin:
            # The device expects us to name the code and the human to type it in.
            shown = "%04d" % random.SystemRandom().randrange(0, 10000)
            try:
                handler.pin(shown)
            except Exception as exc:  # pragma: no cover - protocol specific
                await self._close_pairing()
                raise PlayerError(
                    "Could not set the pairing code",
                    code="pairing_failed",
                ) from exc
        self._pairing = {
            "handler": handler,
            "protocol": name,
            "device": device,
            "config": config,
        }
        return {
            "started": True,
            "protocol": name,
            "device": device_label(device),
            "needs_pin": provides_pin,
            "pin": shown,
            "message": (
                "Enter the PIN shown on the speaker."
                if provides_pin
                else "Enter %s on the speaker to finish pairing." % shown
            ),
        }

    async def finish_pairing(self, pin: str = "") -> Dict[str, Any]:
        """Hand over the PIN (when the device asked for one) and complete."""
        session = self._pairing
        if session is None:
            raise PlayerError(
                "No pairing in progress.",
                code="no_pairing",
                hint="Comece o pareamento de novo pela lista de saídas.",
                retryable=False,
            )
        handler = session["handler"]
        if getattr(handler, "device_provides_pin", True):
            code = str(pin or "").strip()
            if not code:
                raise PlayerError(
                    "This speaker needs the PIN it is showing on screen.",
                    code="pin_required",
                    retryable=False,
                )
            try:
                handler.pin(code)
            except Exception as exc:
                raise PlayerError("The PIN was rejected.", code="bad_pin") from exc
        try:
            await handler.finish()
            paired = bool(getattr(handler, "has_paired", False))
        except Exception as exc:
            await self._close_pairing()
            raise PlayerError(
                "Pairing failed",
                code="pairing_failed",
                hint="Confira o PIN e tente de novo.",
            ) from exc
        await self._close_pairing()
        if paired:
            await self._save_storage()
            self.log.info(
                "paired with %s over %s", device_label(session["device"]), session["protocol"]
            )
        return {
            "paired": paired,
            "protocol": session["protocol"],
            "device": device_label(session["device"]),
            "message": "Paired." if paired else "The speaker did not accept the pairing.",
        }

    async def cancel_pairing(self) -> Dict[str, Any]:
        had = self._pairing is not None
        await self._close_pairing()
        return {"cancelled": had}

    async def _close_pairing(self) -> None:
        session, self._pairing = self._pairing, None
        if session is None:
            return
        handler = session.get("handler")
        if handler is None:
            return
        try:
            await handler.close()
        except Exception as exc:  # pragma: no cover - best effort teardown
            self.log.debug("closing pairing handler failed: %s", exc)

    @property
    def pairing_in_progress(self) -> bool:
        return self._pairing is not None

    # -- status ----------------------------------------------------------
    async def status(self) -> Dict[str, Any]:
        data = self.snapshot()
        data["mode"] = self._mode
        data["pairing"] = self.pairing_in_progress
        atv = self._atv
        if atv is not None:
            try:
                # Cached by pyatv's listener; no network round trip here.
                level = atv.audio.volume
                if level is not None:
                    data["device_volume"] = round(float(level) / 100.0, 3)
            except Exception:  # pragma: no cover - device without volume support
                pass
            data["supports_stream_file"] = self._feature_state("StreamFile") == "Available"
        config = self._config
        if config is not None:
            data["device_id"] = str(getattr(config, "identifier", "") or "")
            data["device"] = str(getattr(config, "name", "") or data.get("device", ""))
        return data


__all__ = ["AirPlayPlayer", "DEVICE_KINDS", "INSTALL_HINT", "PAIRING_PROTOCOLS"]
