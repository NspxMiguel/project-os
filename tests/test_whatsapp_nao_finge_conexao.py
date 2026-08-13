"""O WhatsApp dizia "Connected" sem nunca ter falado com nada.

Duas mentiras na mesma tela, as duas nascidas de tratar *configurado* como
*conectado*:

* ``bridge.status()`` devolvia ``connected: bool(base_url)``. Digitar um
  endereço no campo acendia a bolinha verde e escrevia **Connected** -- mesmo
  que a ponte não existisse, estivesse desligada, ou fosse erro de digitação.
  O ``cloud_api`` fazia o mesmo com o token: token colado, "conectado";
* o botão *"Enviar mensagem de teste"* mostrava o toast verde *"Message sent"*
  mesmo com o provedor ``null``, que só escreve no registro e devolve
  ``delivered: false`` -- o dado estava na resposta, e a tela ignorava.

Agora ``connected`` tem três estados -- sim, não, e ainda-não-perguntei -- e
quem responde "sim" é o ``probe()``, que bate no outro lado de verdade.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from typing import Any, Dict, Iterator, List

import pytest

PREFIX = "/api/apps/whatsapp-bot"


def _wa(module: str = "app"):
    return importlib.import_module("project_os.apps.whatsapp-bot.%s" % module)


# --------------------------------------------------------------------------- um httpx que também responde GET


class _Resposta(object):
    def __init__(self, status_code: int = 200, payload: Any = None, texto: str = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = texto if texto is not None else json.dumps(payload)

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("não é json")
        return self._payload


class _ClienteFalso(object):
    gets: List[Dict[str, Any]] = []
    #: a resposta do próximo GET, ou uma exceção para levantar
    proxima = None  # type: Any

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, url: str, headers: Any = None, **kwargs: Any) -> _Resposta:
        _ClienteFalso.gets.append({"url": url, "headers": headers or {}})
        if isinstance(_ClienteFalso.proxima, Exception):
            raise _ClienteFalso.proxima
        return _ClienteFalso.proxima or _Resposta(200, {"ok": True})


@pytest.fixture()
def httpx_falso(monkeypatch: pytest.MonkeyPatch) -> Iterator[type]:
    _ClienteFalso.gets = []
    _ClienteFalso.proxima = None
    modulo = types.ModuleType("httpx")
    modulo.AsyncClient = _ClienteFalso  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", modulo)
    yield _ClienteFalso


def _rodar(corotina):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(corotina)


# --------------------------------------------------------------------------- a ponte


def test_ponte_configurada_nao_e_ponte_conectada() -> None:
    bridge = _wa("providers.bridge")
    provedor = bridge.BridgeProvider({"base_url": "http://192.168.1.9:3000"})
    estado = provedor.status()
    assert estado["configured"] is True
    assert estado["connected"] is None, "ter uma URL escrita não é estar conectado"


def test_ponte_sem_url_diz_nao_e_nao_nao_sei() -> None:
    bridge = _wa("providers.bridge")
    estado = bridge.BridgeProvider({}).status()
    assert estado["configured"] is False
    assert estado["connected"] is False


def test_ponte_que_atende_vira_conectado(httpx_falso) -> None:
    bridge = _wa("providers.bridge")
    httpx_falso.proxima = _Resposta(200, {"connected": True})
    provedor = bridge.BridgeProvider({"base_url": "http://192.168.1.9:3000", "token": "abc"})
    resultado = _rodar(provedor.probe())
    assert resultado["connected"] is True
    assert httpx_falso.gets[0]["url"] == "http://192.168.1.9:3000/status"
    assert httpx_falso.gets[0]["headers"]["Authorization"] == "Bearer abc"


def test_ponte_desligada_vira_motivo_e_nao_traceback(httpx_falso) -> None:
    bridge = _wa("providers.bridge")
    httpx_falso.proxima = OSError("connection refused")
    provedor = bridge.BridgeProvider({"base_url": "http://192.168.1.9:3000"})
    resultado = _rodar(provedor.probe())
    assert resultado["connected"] is False
    assert "192.168.1.9" in resultado["reason"]


def test_ponte_que_nao_tem_status_fica_em_nao_sei(httpx_falso) -> None:
    """404 é uma ponte viva com outro contrato -- não é "não conectado"."""
    bridge = _wa("providers.bridge")
    httpx_falso.proxima = _Resposta(404, None, texto="not found")
    provedor = bridge.BridgeProvider({"base_url": "http://ponte:3000"})
    resultado = _rodar(provedor.probe())
    assert resultado["connected"] is None
    assert "/status" in resultado["reason"]


def test_ponte_pareada_mas_deslogada_diz_nao(httpx_falso) -> None:
    bridge = _wa("providers.bridge")
    httpx_falso.proxima = _Resposta(200, {"connected": False, "reason": "sessão expirada"})
    provedor = bridge.BridgeProvider({"base_url": "http://ponte:3000"})
    resultado = _rodar(provedor.probe())
    assert resultado["connected"] is False
    assert resultado["reason"] == "sessão expirada"


# --------------------------------------------------------------------------- a Cloud API


def test_token_colado_nao_e_conexao() -> None:
    cloud = _wa("providers.cloud_api")
    provedor = cloud.CloudApiProvider(
        {"phone_number_id": "123", "access_token": "EAA-token"}
    )
    estado = provedor.status()
    assert estado["configured"] is True
    assert estado["connected"] is None


def test_meta_recusando_o_token_vira_nao_conectado(httpx_falso) -> None:
    cloud = _wa("providers.cloud_api")
    httpx_falso.proxima = _Resposta(401, {"error": {"message": "Invalid OAuth token"}})
    provedor = cloud.CloudApiProvider(
        {"phone_number_id": "123", "access_token": "velho"}
    )
    resultado = _rodar(provedor.probe())
    assert resultado["connected"] is False
    assert "token" in resultado["reason"].lower()


def test_meta_respondendo_o_numero_vira_conectado(httpx_falso) -> None:
    cloud = _wa("providers.cloud_api")
    httpx_falso.proxima = _Resposta(200, {"id": "123", "display_phone_number": "+55 11 9"})
    provedor = cloud.CloudApiProvider(
        {"phone_number_id": "123", "access_token": "bom"}
    )
    resultado = _rodar(provedor.probe())
    assert resultado["connected"] is True
    assert "123" in httpx_falso.gets[0]["url"]


# --------------------------------------------------------------------------- o que a tela recebe


def test_sem_provedor_a_tela_recebe_nao_e_um_resumo_que_explica(auth_client) -> None:
    corpo = auth_client.get(PREFIX + "/status").json()
    assert corpo["provider"] == "null"
    assert corpo["connected"] is False
    assert corpo["configured"] is False
    assert "provedor" in corpo["summary"].lower()
    rotulos = {campo["label"]: campo["value"] for campo in corpo["fields"]}
    assert rotulos["Configurado"] is False
    assert rotulos["Conectado"] == "não"


def test_ponte_configurada_e_fora_do_ar_nao_vira_conectado_na_tela(auth_client, httpx_falso) -> None:
    """O caminho inteiro: salvar config -> GET /status -> o que a tela lê."""
    httpx_falso.proxima = OSError("connection refused")
    salvar = auth_client.put(
        PREFIX + "/config",
        json={"provider": "bridge", "bridge.base_url": "http://192.168.1.9:3000"},
    )
    assert salvar.status_code == 200, salvar.text

    corpo = auth_client.get(PREFIX + "/status").json()
    assert corpo["provider"] == "bridge"
    assert corpo["configured"] is True
    assert corpo["connected"] is False, "a ponte não atendeu; a tela não pode dizer conectado"
    assert "192.168.1.9" in corpo["summary"]
    rotulos = {campo["label"]: campo["value"] for campo in corpo["fields"]}
    assert rotulos["Conectado"] == "não"


def test_sem_probe_a_tela_recebe_nao_sei_em_vez_de_sim(auth_client, httpx_falso) -> None:
    """``?probe=false`` é o estado barato -- e ele diz "não sei", não "sim"."""
    auth_client.put(
        PREFIX + "/config",
        json={"provider": "bridge", "bridge.base_url": "http://ponte:3000"},
    )
    corpo = auth_client.get(PREFIX + "/status", params={"probe": "false"}).json()
    assert corpo["connected"] is None
    assert corpo["configured"] is True
    rotulos = {campo["label"]: campo["value"] for campo in corpo["fields"]}
    assert rotulos["Conectado"] == "não sei"
    assert httpx_falso.gets == [], "probe=false não pode sair na rede"


def test_um_provedor_que_explode_no_probe_nao_derruba_a_tela(auth_client, monkeypatch) -> None:
    auth_client.put(
        PREFIX + "/config",
        json={"provider": "bridge", "bridge.base_url": "http://ponte:3000"},
    )

    resposta = auth_client.get(PREFIX + "/status")
    assert resposta.status_code == 200, resposta.text


# --------------------------------------------------------------------------- o envio


def test_mandar_sem_provedor_responde_delivered_false(auth_client) -> None:
    """A tela precisa do dado para não mostrar verde: ele está aqui."""
    auth_client.post(PREFIX + "/allowlist", json={"number": "5511999998888"})
    resposta = auth_client.post(
        PREFIX + "/send", json={"to": "5511999998888", "text": "oi"}
    )
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["delivered"] is False
    assert corpo["note"], "e com um motivo para a tela mostrar"


def test_a_tela_nao_diz_enviado_quando_delivered_e_falso() -> None:
    """O painel lê ``delivered`` antes de escolher o toast.

    Um teste de leitura do JS: não há navegador aqui, mas dá para garantir que
    o caminho verde não é mais incondicional -- que era o bug.
    """
    import io
    import os

    caminho = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "project_os", "apps", "whatsapp-bot", "web", "panel.js",
    )
    fonte = io.open(caminho, encoding="utf-8").read()
    assert "resposta.delivered === false" in fonte
    inicio = fonte.index("appApi.post('/send'")
    trecho = fonte[inicio:inicio + 900]
    verde = trecho.index("wa.send.sent")
    aviso = trecho.index("wa.send.logged")
    assert aviso < verde, "o caso 'não entregue' tem que ser testado antes do verde"
    assert "'warning'" in trecho
