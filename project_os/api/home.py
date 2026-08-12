"""A casa: por enquanto, o Home Assistant que já existe na rede.

``project_os/core/ha.py`` são 472 linhas de cliente REST escritas com cuidado --
com ``summary()`` que diz "safe-to-serialise state for the settings screen" e
``ping()`` que diz "the probe behind the Test button". A tela nunca foi feita e
o módulo nunca teve um chamador. Enquanto isso o produto oferecia conectar num
Home Assistant existente em três lugares: um cartão de sugestão no painel
("Já existe um Home Assistant em 192.168.x.x"), uma receita, e o docs/HOME.md.
Os três levavam a um lugar onde não havia campo nenhum.

Este arquivo é a porta que faltava. Nada de novo acontece aqui: ele só liga o
que já estava escrito.

Três decisões:

**Um token que não funciona não é salvo.** ``POST /home/connect`` testa antes de
gravar. Salvar primeiro e descobrir depois deixaria a caixa dizendo "conectado"
com um token revogado.

**O token nunca volta.** Nem no GET, nem no erro, nem no log. O que volta é
``has_token``.

**Sem httpx não é erro, é recado.** O cliente já trata isso; aqui a resposta
carrega o comando que instala.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from project_os import auth
from project_os.core import ha
from project_os.errors import ApiError
from project_os.main import get_config

log = logging.getLogger(__name__)

router = APIRouter(prefix="/home", tags=["home"])

#: Os únicos serviços que esta rota chama. Um campo livre aqui seria "mande
#: qualquer serviço para a casa dele" vindo de qualquer aba aberta na LAN.
SERVICOS = ("turn_on", "turn_off", "toggle")


class ConnectBody(BaseModel):
    url: str
    token: str

    model_config = {"extra": "forbid"}


class TestBody(BaseModel):
    url: Optional[str] = None
    #: Ausente = usa o token já salvo, para testar de novo sem redigitar.
    token: Optional[str] = None

    model_config = {"extra": "forbid"}


class CallBody(BaseModel):
    service: str

    model_config = {"extra": "forbid"}


def _cliente(config: Any) -> ha.HomeAssistantClient:
    return ha.client_from_config(config)


@router.get("")
async def state(
    probe: bool = Query(False, description="Também testa a conexão agora"),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    cliente = _cliente(config)
    corpo = cliente.summary()
    corpo["connected"] = None
    corpo["message"] = "" if cliente.configured else cliente.missing_config_message()
    if probe and cliente.configured:
        ok, mensagem = await cliente.ping()
        corpo["connected"] = bool(ok)
        corpo["message"] = mensagem
    return corpo


@router.post("/test")
async def test(
    body: TestBody,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Testa um endereço sem gravar nada."""
    cliente = ha.HomeAssistantClient(
        config=config,
        url=body.url if body.url is not None else None,
        token=body.token if body.token is not None else None,
    )
    if not cliente.configured:
        return {"ok": False, "message": cliente.missing_config_message()}
    ok, mensagem = await cliente.ping()
    return {"ok": bool(ok), "message": mensagem, "url": cliente.url}


@router.post("/connect")
async def connect(
    body: ConnectBody,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Testa e, se responder, grava. Nesta ordem."""
    url = ha.normalise_url(body.url)
    token = (body.token or "").strip()
    if not url or not token:
        raise ApiError(
            400,
            "incomplete",
            ha.HomeAssistantClient(url=url, token=token).missing_config_message(),
        )
    cliente = ha.HomeAssistantClient(config=config, url=url, token=token)
    ok, mensagem = await cliente.ping()
    if not ok:
        # 502: quem falhou foi a caixa do outro lado, não este pedido.
        raise ApiError(502, "ha_unreachable", mensagem, detail={"url": url})
    config.set_many(
        {
            "%s.url" % ha.CONFIG_ROOT: url,
            "%s.token" % ha.CONFIG_ROOT: token,
            "%s.enabled" % ha.CONFIG_ROOT: True,
        }
    )
    config.save()
    log.info("home assistant connected at %s (by %s)", url, user.get("username"))
    corpo = _cliente(config).summary()
    corpo["connected"] = True
    corpo["message"] = mensagem
    return corpo


@router.delete("")
async def disconnect(
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Esquece endereço e token. Não mexe em nada do lado de lá."""
    config.set_many(
        {
            "%s.url" % ha.CONFIG_ROOT: "",
            "%s.token" % ha.CONFIG_ROOT: "",
            "%s.enabled" % ha.CONFIG_ROOT: False,
        }
    )
    config.save()
    log.info("home assistant disconnected (by %s)", user.get("username"))
    corpo = _cliente(config).summary()
    corpo["connected"] = False
    corpo["message"] = ""
    return corpo


@router.get("/entities")
async def entities(
    domain: Optional[str] = Query(None),
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    cliente = _cliente(config)
    if not cliente.configured:
        raise ApiError(409, "not_configured", cliente.missing_config_message())
    resultado = await cliente.entities(domain)
    if not resultado.ok:
        raise ApiError(502, "ha_unreachable", resultado.message)
    itens = resultado.data or []
    return {"items": itens, "count": len(itens), "domain": domain or ""}


@router.post("/entities/{entity_id}/call")
async def call(
    entity_id: str,
    body: CallBody,
    config: Any = Depends(get_config),
    user: Dict[str, Any] = Depends(auth.require_auth),
) -> Dict[str, Any]:
    """Liga, desliga ou inverte uma entidade. É o botão da lista."""
    servico = (body.service or "").strip()
    if servico not in SERVICOS:
        raise ApiError(
            400,
            "unknown_service",
            "Daqui só dá para %s." % ", ".join(SERVICOS),
        )
    if "." not in entity_id:
        raise ApiError(400, "bad_entity", "Uma entidade é dominio.nome, como light.sala.")
    dominio = entity_id.split(".", 1)[0]
    cliente = _cliente(config)
    if not cliente.configured:
        raise ApiError(409, "not_configured", cliente.missing_config_message())
    resultado = await cliente.call_service(dominio, servico, {"entity_id": entity_id})
    if not resultado.ok:
        raise ApiError(502, "ha_call_failed", resultado.message)
    log.info("home assistant %s %s (by %s)", servico, entity_id, user.get("username"))
    return {"ok": True, "entity_id": entity_id, "service": servico}


__all__ = ["router"]
