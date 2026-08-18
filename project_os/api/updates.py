"""Updating the system over HTTP.

    "n precisa fica tirando o pendrive, puxa pela rede"

``GET /api/updates`` says what version this is and whether there is a newer one.
``POST /api/updates/install`` starts a job -- download, verify, swap, reinstall
dependencies, restart -- and the browser follows its log, exactly like the
package installer, because it is the same shape of operation.

The restart is the interesting part for a client: the answer to the last poll
before it happens is the last answer this process will ever give. The job's log
says ``restarting`` and then the connection drops; the screen is expected to
poll ``/api/health`` until the box answers again, and compare the version.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

import anyio
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from project_os import __version__, auth
from project_os.core import slots, sysupdate, updates
from project_os.errors import ApiError
from project_os.main import get_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/updates", tags=["updates"])

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_RESTARTING = "restarting"

LOG_LINES = 300


class InstallRequest(BaseModel):
    #: Belt and braces: the client says which version it believes it is
    #: installing, and a mismatch means the manifest changed underneath it.
    version: Optional[str] = None
    restart: bool = True


class _Job(object):
    """The one update in flight, if any. Deliberately a singleton."""

    def __init__(self) -> None:
        self.state = STATE_IDLE
        self.message = ""
        self.target = ""
        self.previous = ""
        self.started_at = 0.0
        self._log = []  # type: List[str]
        self._lock = threading.Lock()

    def reset(self, target: str) -> None:
        with self._lock:
            self.state = STATE_RUNNING
            self.message = ""
            self.target = target
            self.previous = ""
            self._log = []

    def say(self, line: str) -> None:
        log.info("update: %s", line)
        with self._lock:
            self._log.append(str(line).rstrip())
            if len(self._log) > LOG_LINES:
                del self._log[: len(self._log) - LOG_LINES]

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "message": self.message,
                "target": self.target,
                "previous": self.previous,
                "log": list(self._log),
            }


_job = _Job()
_last_check = None  # type: Optional[Dict[str, Any]]


def _settings(config: Any) -> Dict[str, Any]:
    return {
        "manifest_url": str(
            config.get("updates.manifest_url", updates.DEFAULT_MANIFEST_URL)
            or updates.DEFAULT_MANIFEST_URL
        ),
        "branch": str(config.get("updates.branch", "main") or "main"),
        "enabled": bool(config.get("updates.enabled", True)),
    }


def _translate(exc: updates.UpdateError) -> ApiError:
    status = {
        "offline": 503,
        "manifest_http_error": 502,
        "bad_manifest": 502,
        "unverifiable": 409,
        "checksum_mismatch": 409,
        "unsafe_archive": 409,
        "bad_archive": 409,
        "no_previous": 404,
    }.get(exc.code, 500)
    return ApiError(status, exc.code, exc.message, exc.hint or None)


def _previous() -> Optional[Dict[str, Any]]:
    """A versão anterior guardada no disco, se houver.

    Lida a cada pedido, e não lembrada da instalação: a instalação reinicia o
    serviço, então tudo que estivesse só na memória sumiria antes de alguém
    querer voltar.
    """
    for item in updates.previous_versions():
        return {"version": item["version"], "path": item["path"], "at": item["at"]}
    return None


@router.get("")
async def status(
    config: Any = Depends(get_config), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    settings = _settings(config)
    return {
        "version": __version__,
        "method": updates.method(),
        # Por onde a troca de versão vai passar nesta caixa -- por fora da pasta
        # do código, por dentro dela, ou por git. Responde "por que atualizou
        # aqui e não ali?" sem precisar abrir o Pi.
        "strategy": updates.swap_strategy()[0],
        "root": updates.root_dir(),
        "under_systemd": updates.under_systemd(),
        "manifest_url": settings["manifest_url"],
        "branch": settings["branch"],
        "enabled": settings["enabled"],
        "last_check": _last_check,
        "job": _job.as_dict(),
        "previous": _previous(),
    }


@router.post("/check")
async def check(
    config: Any = Depends(get_config), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    global _last_check
    settings = _settings(config)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: updates.check(settings["manifest_url"], settings["branch"])
        )
    except updates.UpdateError as exc:
        raise _translate(exc)
    _last_check = result
    return result


def _run_install(info: Dict[str, Any], restart: bool) -> None:
    try:
        if info["method"] == updates.METHOD_GIT:
            result = updates.apply_git(info, on_line=_job.say)
        else:
            result = updates.apply_tarball(info, on_line=_job.say)
        _job.previous = str(result.get("previous", ""))
        code = updates.install_requirements(on_line=_job.say)
        if code != 0:
            # Not fatal: the new code is in, and every optional dependency in
            # project-os degrades honestly when missing.
            _job.say("pip exited %d -- some optional dependencies may be missing" % code)
        # O yt-dlp entra pelo script da imagem e não pelo requirements.txt, então
        # a linha acima nunca o alcançava: ele ficava parado na versão do dia em
        # que a imagem foi construída. Um baixador de YouTube parado é um
        # baixador que vai falhar, porque o YouTube não fica parado.
        extras = updates.refresh_extras(on_line=_job.say)
        if extras != 0:
            _job.say("pip exited %d refreshing the image extras" % extras)
        _job.state = STATE_DONE
        _job.message = "Updated to %s." % info.get("latest", "the new version")
        if restart:
            _job.state = STATE_RESTARTING
            _job.say("restarting")
            updates.restart(on_line=_job.say)
    except updates.UpdateError as exc:
        _job.state = STATE_ERROR
        _job.message = exc.message
        _job.say("failed: %s" % exc.message)
    except Exception as exc:  # pragma: no cover - defensive
        _job.state = STATE_ERROR
        _job.message = str(exc)
        _job.say("failed: %s" % exc)
        log.exception("update failed")


@router.post("/install")
async def install(
    body: InstallRequest,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    settings = _settings(config)
    if not settings["enabled"]:
        raise ApiError(
            403, "updates_disabled",
            "Ligue updates.enabled em Configurações para atualizar por aqui.",
        )
    if _job.state in (STATE_RUNNING, STATE_RESTARTING):
        raise ApiError(409, "busy", "Já tem uma atualização rodando.")

    try:
        info = await anyio.to_thread.run_sync(
            lambda: updates.check(settings["manifest_url"], settings["branch"])
        )
    except updates.UpdateError as exc:
        raise _translate(exc)

    if not info.get("update_available"):
        raise ApiError(
            409, "already_current", "Esta caixa já está na %s." % __version__,
        )
    if body.version and body.version != info.get("latest"):
        raise ApiError(
            409, "version_moved",
            "A versão mais nova agora é %s, não %s. Verifique de novo."
            % (info.get("latest"), body.version),
        )

    _job.reset(str(info.get("latest", "")))
    _job.say("updating %s -> %s" % (__version__, info.get("latest")))
    thread = threading.Thread(target=_run_install, args=(info, body.restart), daemon=True)
    thread.start()
    return {"job": _job.as_dict(), "target": info.get("latest")}


# ---------------------------------------------------------------------------
# o sistema inteiro, e não só o app
# ---------------------------------------------------------------------------
@router.get("/system")
async def system_status(
    config: Any = Depends(get_config), user: Dict[str, Any] = Depends(auth.require_auth)
) -> Dict[str, Any]:
    """Os dois sistemas do cartão e o que há para instalar neles.

    A tela precisa saber três coisas antes de oferecer um botão: se este cartão
    tem dois sistemas, se o ajudante root está instalado, e qual slot está no ar.
    Num cartão antigo isso responde "não dá, e por quê" em vez de oferecer uma
    atualização que não teria para onde escrever.
    """
    settings = _settings(config)
    return await anyio.to_thread.run_sync(
        lambda: sysupdate.status(settings["manifest_url"])
    )


def _run_system_install(manifest_url: str, restart: bool) -> None:
    """A instalação de sistema, na mesma forma de job que a do app.

    Baixar 600 MB e desempacotar num Pi 3 leva minutos; a tela acompanha pelo
    log do job, e a última linha antes de a conexão cair é "reiniciando".
    """
    try:
        resultado = sysupdate.install(
            manifest_url=manifest_url, on_line=_job.say, reboot=restart,
        )
        _job.target = resultado.get("installed", "")
        if restart:
            _job.state = STATE_RESTARTING
            _job.message = "reiniciando no slot %s" % resultado.get("slot", "")
        else:
            _job.state = STATE_DONE
            _job.message = "slot %s gravado" % resultado.get("slot", "")
    except sysupdate.SystemUpdateError as exc:
        _job.state = STATE_ERROR
        _job.message = exc.message
        _job.say("erro: %s" % exc.message)
        if exc.hint:
            _job.say(exc.hint)
    except Exception as exc:  # pragma: no cover - rede e disco surpreendem
        _job.state = STATE_ERROR
        _job.message = str(exc)
        _job.say("erro: %s" % exc)
        log.exception("atualização de sistema falhou")


@router.post("/system/install")
async def system_install(
    body: InstallRequest,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Grava o sistema novo no slot que não está rodando e reinicia nele."""
    settings = _settings(config)
    if not settings["enabled"]:
        raise ApiError(
            403, "updates_disabled",
            "Ligue updates.enabled em Configurações para atualizar por aqui.",
        )
    if _job.state in (STATE_RUNNING, STATE_RESTARTING):
        raise ApiError(409, "busy", "Já tem uma atualização rodando.")

    pronto = await anyio.to_thread.run_sync(sysupdate.available)
    if not pronto["ok"]:
        raise ApiError(409, pronto["code"], pronto["reason"])

    try:
        info = await anyio.to_thread.run_sync(
            lambda: sysupdate.check(settings["manifest_url"])
        )
    except sysupdate.SystemUpdateError as exc:
        raise ApiError(502, exc.code, exc.message, exc.hint)

    if not info.get("update_available"):
        raise ApiError(409, "already_current", "Esta caixa já está no sistema %s." % __version__)

    _job.reset(str(info.get("latest", "")))
    _job.say("sistema %s -> %s (slot %s)" % (__version__, info.get("latest"), slots.other_slot()))
    thread = threading.Thread(
        target=_run_system_install, args=(settings["manifest_url"], body.restart), daemon=True,
    )
    thread.start()
    return {"job": _job.as_dict(), "target": info.get("latest"), "slot": slots.other_slot()}


@router.post("/system/rollback")
async def system_rollback(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Volta na mão para o outro sistema.

    O initramfs já faz isso sozinho quando o slot novo não sobe. Este botão é
    para o outro caso: o slot novo *sobe*, mas está pior -- aí quem percebe é
    uma pessoa, não um contador de tentativas.
    """
    if not slots.available():
        raise ApiError(409, "no_slots", "Este cartão tem um sistema só.")
    alvo = slots.other_slot()
    if alvo is None:
        raise ApiError(409, "no_slots", "Não sei qual é o outro slot.")
    slots.boot_into(alvo)
    log.warning("voltando para o slot %s por pedido de %s", alvo, user.get("username"))
    import subprocess

    subprocess.Popen(updates.systemctl_argv() + ["reboot"])
    return {"ok": True, "slot": alvo}


@router.post("/rollback")
async def rollback(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    guardada = _previous()
    # _job.previous só existe até o serviço reiniciar -- e a atualização
    # reinicia. O disco é quem sabe.
    previous = _job.previous or (guardada or {}).get("path") or ""
    if not previous:
        raise ApiError(
            404, "no_previous",
            "Não existe versão anterior guardada nesta caixa para voltar.",
        )
    try:
        await anyio.to_thread.run_sync(lambda: updates.rollback(previous))
    except updates.UpdateError as exc:
        raise _translate(exc)
    _job.say("rolled back to %s" % previous)
    _job.previous = ""
    return {
        "ok": True,
        "restored": previous,
        "version": (guardada or {}).get("version", ""),
        "restart_required": True,
    }


@router.post("/restart")
async def restart_service(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
    """Reinicia o serviço para o código que está no disco passar a valer.

    Voltar uma versão troca os arquivos e não a memória: sem isto a caixa fica
    com 0.4.8 no disco e 0.4.9 na tela até alguém reiniciar na mão -- e reiniciar
    na mão é o SSH que este projeto existe para não precisar.

    Numa thread porque as duas saídas de ``updates.restart`` matam este
    processo: sob systemd o unit reinicia, e fora dele o processo se re-executa.
    """
    log.warning("reinício pedido por %s", user.get("username"))
    _job.say("restarting on request")
    threading.Thread(target=lambda: updates.restart(on_line=_job.say), daemon=True).start()
    return {"ok": True}


__all__ = ["router"]
