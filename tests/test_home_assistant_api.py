"""Conectar num Home Assistant que já existe -- prometido em três lugares.

``project_os/core/ha.py`` são 472 linhas de cliente REST, com ``summary()``
comentado como *"safe-to-serialise state for the settings screen"* e ``ping()``
como *"the probe behind the Test button"*. A tela nunca existiu, e o módulo
nunca teve um único chamador em todo o repositório. Enquanto isso:

* o painel mostrava "Já existe um Home Assistant em 192.168.x.x -- dá pra
  conectar nele com um token" e o botão levava para ``#/settings/integrations``,
  uma aba que não existia;
* a receita ``home-assistant-link`` mandava fazer o mesmo;
* o ``docs/HOME.md`` descrevia a integração inteira.

Agora existe ``/api/home``. O que estes testes fixam é o comportamento que
diferencia isso de uma tela de mentira: um token que não funciona **não é
salvo**, e o token **nunca volta** numa resposta.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import pytest


class FakeHA(object):
    """Um Home Assistant que responde o que o teste mandar."""

    def __init__(self, ok: bool = True, mensagem: str = "Connected to Casa, running Home Assistant 2026.1.") -> None:
        self.ok = ok
        self.mensagem = mensagem
        self.chamadas = []  # type: list

    def instalar(self, monkeypatch) -> None:
        from project_os.core import ha

        pai = self

        async def ping(self) -> Tuple[bool, str]:
            pai.chamadas.append(("ping", self.url, self.token))
            return pai.ok, pai.mensagem

        async def entities(self, domain=None):
            pai.chamadas.append(("entities", domain))
            return ha.HAResult(True, "", [
                {"entity_id": "light.sala", "domain": "light", "name": "Sala",
                 "state": "off", "attributes": {}},
                {"entity_id": "sensor.temperatura", "domain": "sensor", "name": "Temperatura",
                 "state": "21.4", "attributes": {}},
            ], 200)

        async def call_service(self, domain, service, data=None):
            pai.chamadas.append(("call", domain, service, (data or {}).get("entity_id")))
            return ha.HAResult(True, "", {}, 200)

        monkeypatch.setattr(ha.HomeAssistantClient, "ping", ping)
        monkeypatch.setattr(ha.HomeAssistantClient, "entities", entities)
        monkeypatch.setattr(ha.HomeAssistantClient, "call_service", call_service)


@pytest.fixture()
def casa(monkeypatch) -> FakeHA:
    falso = FakeHA()
    falso.instalar(monkeypatch)
    return falso


def test_a_caixa_comeca_sem_home_assistant(auth_client):
    corpo = auth_client.get("/api/home").json()
    assert corpo["configured"] is False
    assert corpo["has_token"] is False
    # E diz o que fazer, em vez de só "não configurado".
    assert "token" in corpo["message"].lower() or "address" in corpo["message"].lower()


def test_conectar_testa_antes_de_gravar(auth_client, casa):
    resposta = auth_client.post(
        "/api/home/connect",
        json={"url": "homeassistant.local:8123", "token": "tok-de-verdade"},
    )
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["configured"] is True
    assert corpo["connected"] is True
    assert corpo["url"] == "http://homeassistant.local:8123"
    # O ping aconteceu com o que foi digitado, antes de qualquer gravação.
    assert casa.chamadas[0][0] == "ping"


def test_token_que_nao_funciona_nao_fica_salvo(auth_client, casa):
    casa.ok = False
    casa.mensagem = "Home Assistant refused the token (401)."
    resposta = auth_client.post(
        "/api/home/connect", json={"url": "192.168.1.20:8123", "token": "revogado"}
    )
    assert resposta.status_code == 502
    assert "401" in resposta.json()["message"]

    # E a caixa continua dizendo a verdade sobre si mesma.
    depois = auth_client.get("/api/home").json()
    assert depois["configured"] is False
    assert depois["has_token"] is False


def test_o_token_nunca_volta_numa_resposta(auth_client, casa):
    auth_client.post(
        "/api/home/connect", json={"url": "ha.local:8123", "token": "segredo-do-miguel"}
    )
    for caminho in ("/api/home", "/api/settings"):
        texto = auth_client.get(caminho).text
        assert "segredo-do-miguel" not in texto, caminho


def test_testar_nao_grava_nada(auth_client, casa):
    resposta = auth_client.post(
        "/api/home/test", json={"url": "ha.local:8123", "token": "so-testando"}
    )
    assert resposta.status_code == 200
    assert resposta.json()["ok"] is True
    assert auth_client.get("/api/home").json()["configured"] is False


def test_as_entidades_aparecem_depois_de_conectar(auth_client, casa):
    auth_client.post("/api/home/connect", json={"url": "ha.local:8123", "token": "t"})
    corpo = auth_client.get("/api/home/entities").json()
    assert corpo["count"] == 2
    assert {e["entity_id"] for e in corpo["items"]} == {"light.sala", "sensor.temperatura"}


def test_sem_conexao_as_entidades_dizem_o_porque(auth_client):
    resposta = auth_client.get("/api/home/entities")
    assert resposta.status_code == 409
    assert resposta.json()["error"] == "not_configured"


def test_ligar_uma_luz_chama_o_servico_certo(auth_client, casa):
    auth_client.post("/api/home/connect", json={"url": "ha.local:8123", "token": "t"})
    resposta = auth_client.post(
        "/api/home/entities/light.sala/call", json={"service": "turn_on"}
    )
    assert resposta.status_code == 200, resposta.text
    assert ("call", "light", "turn_on", "light.sala") in casa.chamadas


def test_servico_de_fora_da_lista_e_recusado(auth_client, casa):
    auth_client.post("/api/home/connect", json={"url": "ha.local:8123", "token": "t"})
    resposta = auth_client.post(
        "/api/home/entities/light.sala/call", json={"service": "delete_everything"}
    )
    assert resposta.status_code == 400
    assert resposta.json()["error"] == "unknown_service"


def test_desconectar_esquece_endereco_e_token(auth_client, casa):
    auth_client.post("/api/home/connect", json={"url": "ha.local:8123", "token": "t"})
    resposta = auth_client.delete("/api/home")
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["configured"] is False
    assert corpo["url"] == ""


def test_a_aba_de_integracoes_existe_na_tela():
    """O cartão do painel aponta para #/settings/integrations."""
    import os

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "web", "views", "settings.js"), encoding="utf-8") as arquivo:
        texto = arquivo.read()
    assert "'integrations'" in texto
    assert "/home/connect" in texto
