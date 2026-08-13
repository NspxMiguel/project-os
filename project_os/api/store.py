"""The store: everything you can add to an empty project-os.

A clean install has no apps at all, so this is the screen the system really
starts on. Two decisions shape it:

**Nothing is hidden for being too big.**

> "hospedar servidores (dependendo da rasp fica ruim, mas fds, usuario q escolhe
> ele q se fode)"

An entry that does not fit this board is listed, marked, with the numbers, and
installs anyway once the caller says ``accept_oversize``. The check is advice,
not a gate. Hiding it would only teach people the store is incomplete, and they
would install it by hand.

**Install is honest about what it can do today.** Lightweight built-ins install
for real, here and now. Larger third-party apps are containers or native
recipes that the installer module still has to grow; for those this endpoint
returns the plan it *would* run instead of pretending to succeed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from project_os import auth
from project_os.core import catalog, containers, hardware
from project_os.errors import ApiError
from project_os.main import get_config, get_plugins

log = logging.getLogger(__name__)

router = APIRouter(prefix="/store", tags=["store"])


class InstallBody(BaseModel):
    #: Required when the entry does not fit in the board's memory budget. The
    #: client has to have shown the numbers to get here.
    accept_oversize: bool = False


def _installed_ids(plugins: Any) -> List[str]:
    """The apps that are actually *on*, not the ones that happen to be on disk.

    A bundled app is always on disk -- it ships inside project-os. Counting that
    as installed made the store say "Installed" for BirdTunes on a machine where
    it had never been turned on, and the Install button then answered
    "already_installed" and did nothing.
    """
    return [item["id"] for item in plugins.list_apps() if item.get("state") != "disabled"]


def _decorate(entry: Dict[str, Any], installed: List[str], plugins: Any = None,
              runtime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    item = dict(entry)
    item["installed"] = entry["id"] in installed
    if item["kind"] == "builtin":
        # A few builtins in the catalog are planned, not written. Saying so on
        # the card beats an Install button that answers "No app called 'kasa'".
        item["installable"] = plugins is None or bool(plugins.has(entry["id"]))
        if not item["installable"]:
            item["install_reason"] = (
                "%s ainda não foi feito — está na lista." % entry["name"]
            )
    if item["kind"] == "service":
        # 23 dos 34 itens do catálogo são serviço, e nenhum instalador de
        # serviço foi escrito: o botão Instalar deles terminava sempre em
        # "installer_pending", depois do clique. Os que têm pacote no Debian
        # instalam de verdade pelo mesmo apt da tela de Programas; o resto diz
        # no cartão, antes do clique, que ainda não instala daqui.
        item["installable"] = bool(entry.get("apt"))
        if item["installable"]:
            item["install_method"] = "apt"
            item["install_package"] = entry["apt"]
        else:
            item["install_reason"] = (
                "%s ainda não instala pela loja: falta o instalador de serviço. "
                "Dá para instalar na mão pelo Terminal ou pela tela de Programas."
                % entry["name"]
            )
    if item["kind"] == "recipe":
        item["installable"] = False
        item["install_reason"] = (
            "%s é uma receita: os passos ficam em Aparelhos, não num botão de "
            "instalar." % entry["name"]
        )
    if item["kind"] == "container":
        # Said up front, on the card, before the install button: a machine
        # with neither docker nor podman should not find that out mid-spinner.
        # Perguntado uma vez por página e passado adiante: desde que a resposta
        # deixou de ser "o binário existe" e passou a ser "o motor responde",
        # cada pergunta é um "docker info". Um catálogo com trinta contêineres
        # faria trinta -- e cada um pode demorar até o timeout.
        item["container_runtime"] = runtime if runtime is not None else containers.runtime_status()
        item["installable"] = bool(item.get("container"))
        if not item["installable"]:
            # Immich, Frigate e Paperless não são um contêiner: são pilhas
            # (banco, fila, modelos). Um bloco `container:` de um só serviço
            # aqui seria uma mentira diferente, não um conserto.
            item["install_reason"] = (
                "%s precisa de mais de um contêiner (banco de dados e afins), e "
                "este instalador roda um só. Está listado para você saber que "
                "existe e quanto custa." % entry["name"]
            )
    return item


@router.get("")
async def browse(
    category: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Search name, summary and tags"),
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    board = hardware.detect()
    installed = _installed_ids(plugins)
    entradas = catalog.entries(board)
    # Só pergunta ao motor de contêiner se a página tem algum contêiner.
    runtime = (containers.runtime_status()
               if any(e.get("kind") == "container" for e in entradas) else None)
    items = [_decorate(entry, installed, plugins, runtime) for entry in entradas]

    if category:
        items = [item for item in items if item["category"] == category]
    if q:
        needle = q.strip().lower()
        items = [
            item
            for item in items
            # O id entra na busca porque é ele que as outras telas mandam: o
            # selo "Está na loja" e os cartões de sugestão linkam por id, e
            # procurar "home-assistant" não achava o "Home Assistant".
            if needle in item["id"].lower()
            or needle in item["name"].lower()
            or needle in item["summary"].lower()
            or any(needle in tag.lower() for tag in item.get("tags", []))
        ]

    return {
        "items": items,
        "count": len(items),
        "categories": catalog.categories(),
        "board": {
            "model": board.model,
            "ram_total_mb": board.ram_total_mb,
            "tier": board.tier,
            "budget_mb": catalog.budget_mb(board),
            "reserved_mb": catalog.RESERVED_MB,
        },
    }


@router.get("/{app_id}")
async def detail(
    app_id: str,
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    entry = catalog.get(app_id)
    if entry is None:
        raise ApiError(404, "not_in_catalog", "A loja não tem nenhum item chamado %r." % app_id)
    return _decorate(entry, _installed_ids(plugins), plugins)


@router.post("/{app_id}/install")
async def install(
    app_id: str,
    body: InstallBody,
    plugins: Any = Depends(get_plugins),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    entry = catalog.get(app_id)
    if entry is None:
        raise ApiError(404, "not_in_catalog", "A loja não tem nenhum item chamado %r." % app_id)
    if app_id in _installed_ids(plugins):
        raise ApiError(409, "already_installed", "%s já está instalado." % entry["name"])

    if not entry["fits"] and not body.accept_oversize:
        # Not a refusal -- a speed bump with the numbers on it. Send
        # accept_oversize and it proceeds.
        raise ApiError(
            409,
            "does_not_fit",
            entry["fit_reason"],
            detail={
                "ram_mb": entry["ram_mb"],
                "budget_mb": catalog.budget_mb(),
                "override_with": "accept_oversize",
            },
        )

    if entry["kind"] == "builtin":
        if not plugins.has(app_id):
            raise ApiError(
                501,
                "installer_pending",
                "%s está no catálogo mas ainda não foi feito — está na lista."
                % entry["name"],
                detail={"id": app_id, "kind": entry["kind"]},
            )
        result = await plugins.enable(app_id)
        log.info("installed builtin app %s (by %s)", app_id, user.get("username"))
        return {"ok": True, "installed": True, "app": result}

    if entry["kind"] == "service" and entry.get("apt"):
        # O instalador que já existe, o mesmo da tela de Programas: apt-get com
        # log ao vivo e job id. Nada de novo aqui além de chamar.
        from project_os.api import packages as packages_api
        from project_os.core import packages

        packages_api._require_writes(config)
        try:
            job = packages_api.runner().start("install", "apt", entry["apt"])
        except packages.PackageError as exc:
            raise packages_api._translate(exc)
        log.info("installing service app %s via apt (by %s)", app_id, user.get("username"))
        # Não é "instalado": é "começou". O apt-get num Pi 3 leva minutos, e a
        # tela acompanha pelo mesmo job da tela de Programas.
        return {
            "ok": True,
            "installed": False,
            "started": True,
            "job": job.as_dict(tail=0),
            "watch": "/api/packages/jobs/%s" % job.id,
        }

    if entry["kind"] == "container" and entry.get("container"):
        engine = containers.detect_runtime()
        if engine is None:
            raise ApiError(
                503,
                "no_container_runtime",
                "%s needs a container runtime (Docker or Podman), and neither is "
                "installed on this machine yet." % entry["name"],
                detail={"id": app_id},
            )
        try:
            spec = containers.parse_spec(app_id, entry["container"])
        except containers.ContainerError as exc:
            # A broken block in the catalog, not the user's fault -- but still
            # not something to hand to a command line.
            raise ApiError(
                500,
                "invalid_container_spec",
                "%s tem uma configuração de contêiner que não serve: %s" % (entry["name"], exc),
            )
        result = await plugins.install_container(app_id, entry, spec, engine)
        log.info("installed container app %s via %s (by %s)", app_id, engine, user.get("username"))
        return {"ok": True, "installed": True, "app": result}

    # Everything else needs the installer, which is still being built. Saying so
    # beats a spinner that ends in a lie.
    raise ApiError(
        501,
        "installer_pending",
        "%s instala como %s, e esse instalador ainda não está pronto."
        % (entry["name"], entry["kind"]),
        detail={"id": app_id, "kind": entry["kind"], "plan": entry.get("install")},
    )


@router.delete("/{app_id}")
async def uninstall(
    app_id: str,
    plugins: Any = Depends(get_plugins),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Remove an app. Its settings and data are left alone.

    Reinstalling something and finding your playlists still there is the right
    surprise; the wrong one is a click that silently deletes a year of them.
    Clearing data is a separate, explicit action.
    """
    if app_id not in _installed_ids(plugins):
        raise ApiError(404, "not_installed", "%s não está instalado." % app_id)
    info = plugins.info(app_id) or {}
    if info.get("source") == "container":
        result = await plugins.uninstall_container(app_id)
    else:
        result = await plugins.disable(app_id)
    log.info("uninstalled %s (by %s)", app_id, user.get("username"))
    return {"ok": True, "installed": False, "app": result, "data_kept": True}


__all__ = ["router"]
