"""A receita contava passos que só podiam terminar em erro.

O cartão do aparelho abre com uma frase que é a razão de alguém começar:
*"4 de 5 passos são um clique"*. A conta somava todo passo de tipo automático
-- e três coisas nesse conjunto não eram automáticas coisa nenhuma:

* **install de app que não instala.** Vinte e três itens do catálogo são
  serviço sem instalador, e dois dos ``builtin`` (mqtt, kasa) ainda não foram
  escritos. "Instalar o app MQTT" era um botão azul que só podia responder
  *installer_pending* -- depois do clique;
* **config de chave que ninguém lê.** ``birdtunes.output_device``,
  ``mqtt.host``, ``zigbee2mqtt.serial_port`` e ``home_assistant.base_url`` não
  existem em lugar nenhum do sistema: o passo gravava no config.yaml uma chave
  inventada, dizia "pronto", e o BirdTunes continuava tocando no lugar errado.
  As de verdade são ``apps.settings.birdtunes.output.*`` e
  ``integrations.home_assistant.url``;
* **a porta serial do Zigbee2MQTT**, que mora no configuration.yaml *dele* --
  guardar aqui não chegaria a lugar nenhum, então virou passo manual com o
  caminho do arquivo.

Os dois primeiros são erros que só aparecem em produção, um clique de cada vez.
Este arquivo os torna erro de teste.
"""

from __future__ import annotations

import json
import os

import pytest

from project_os.core import catalog, recipes

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- chaves de config


def _chaves_do_app(app_id):
    """As chaves que o manifesto do app declara -- as que ele realmente lê."""
    caminho = os.path.join(RAIZ, "project_os", "apps", app_id, "manifest.json")
    if not os.path.isfile(caminho):
        return None
    with open(caminho, encoding="utf-8") as arquivo:
        manifesto = json.load(arquivo)
    return {campo["key"] for campo in manifesto.get("config_schema", [])}


def _existe_nos_defaults(caminho):
    from project_os.config import DEFAULTS

    atual = DEFAULTS
    for parte in caminho.split("."):
        if not isinstance(atual, dict) or parte not in atual:
            return False
        atual = atual[parte]
    return True


def _passos_de_config():
    for receita in recipes.all_recipes():
        for passo in receita["steps"]:
            if passo["kind"] != recipes.STEP_CONFIG:
                continue
            valores = passo.get("values") or {passo["key"]: passo.get("value", "")}
            for chave in valores:
                yield receita["id"], chave


def test_toda_chave_gravada_por_receita_existe_de_verdade():
    mortas = []
    for receita_id, chave in _passos_de_config():
        if chave.startswith("apps.settings."):
            resto = chave[len("apps.settings."):]
            app_id, _, dentro = resto.partition(".")
            declaradas = _chaves_do_app(app_id)
            if declaradas is None:
                mortas.append((receita_id, chave, "o app %s não existe" % app_id))
            elif dentro not in declaradas:
                mortas.append((receita_id, chave, "o manifesto de %s não declara %s" % (app_id, dentro)))
        elif not _existe_nos_defaults(chave):
            mortas.append((receita_id, chave, "não está no config"))
    assert not mortas, "receitas gravando em chave que ninguém lê: %s" % mortas


def test_a_chave_do_birdtunes_e_a_que_o_app_le(auth_client):
    """Não basta existir no manifesto: tem que ser a que o app consulta."""
    config = auth_client.app.state.config
    config.set("apps.settings.birdtunes.output.type", "chromecast")
    config.set("apps.settings.birdtunes.output.device_id", "dev-123")

    corpo = auth_client.get("/api/apps/birdtunes/outputs").json()
    assert corpo["current"] == "chromecast", "a tela do app lê esta chave"


# --------------------------------------------------------------------------- passos de instalar


def test_nenhum_passo_de_instalar_aponta_para_fora_do_catalogo():
    ids = {entrada["id"] for entrada in catalog.all_entries()}
    for receita in recipes.all_recipes():
        for passo in receita["steps"]:
            if passo["kind"] == recipes.STEP_INSTALL:
                assert passo["app"] in ids, "%s instala %r, que não está no catálogo" % (
                    receita["id"], passo["app"]
                )


def test_um_app_que_nao_instala_deixa_de_contar_como_um_clique():
    """`mqtt` está no catálogo e não foi escrito: o passo diz isso."""
    receita = recipes.get("mqtt-broker-found")
    assert receita is not None
    render = recipes.render(receita, {"id": "dev-1", "address": "192.168.1.5", "port": 1883})
    passo = render["steps"][0]
    assert passo["kind"] == "install" and passo["app"] == "mqtt"
    assert passo["automatic"] is False
    assert "ainda não foi feito" in passo["blocked_reason"]
    assert render["automatic"] == 0, "nenhum passo desta receita é um clique hoje"


def test_um_app_que_instala_continua_contando():
    receita = recipes.get("birdtunes-cast")
    render = recipes.render(receita, {"id": "dev-1", "name": "Caixa"})
    instalar = [p for p in render["steps"] if p["kind"] == "install"][0]
    assert instalar["automatic"] is True
    assert "blocked_reason" not in instalar


def test_app_ja_instalado_nao_vira_bloqueado():
    receita = recipes.get("mqtt-broker-found")
    render = recipes.render(
        receita, {"id": "dev-1", "address": "192.168.1.5", "port": 1883}, installed=["mqtt"]
    )
    passo = render["steps"][0]
    assert passo["done"] is True
    assert "blocked_reason" not in passo


def test_a_loja_e_a_receita_dao_o_mesmo_motivo(auth_client):
    """Duas telas, uma regra: era possível a loja recusar e a receita oferecer."""
    itens = auth_client.get("/api/store").json()["items"]
    da_loja = {item["id"]: item.get("install_reason") for item in itens}
    for receita in recipes.all_recipes():
        render = recipes.render(receita, {"id": "d", "address": "1.2.3.4", "port": 80})
        for passo in render["steps"]:
            if passo.get("blocked_reason"):
                assert passo["blocked_reason"] == da_loja[passo["app"]], passo["app"]


# --------------------------------------------------------------------------- rodando o passo


def _aparelho(client, kind, **extra):
    campos = {
        "id": "dev-%s" % kind, "kind": kind, "name": extra.get("name", "Coisa"),
        "address": extra.get("address", "192.168.1.60"), "port": extra.get("port", 8009),
    }
    client.app.state.db.execute(
        "INSERT INTO devices (id, kind, name, address, port, properties, capabilities,"
        " first_seen, last_seen, pinned, ignored) VALUES (?,?,?,?,?,?,?,?,?,0,0)",
        (campos["id"], campos["kind"], campos["name"], campos["address"], campos["port"],
         "{}", json.dumps(extra.get("capabilities", [])),
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    return campos["id"]


def test_rodar_o_passo_do_birdtunes_grava_as_duas_chaves(auth_client):
    device_id = _aparelho(
        auth_client, "cast_audio", name="Caixa da sala",
        capabilities=["audio_out", "cast_media"],
    )
    receita = recipes.get("birdtunes-cast")
    indice = [i for i, p in enumerate(receita["steps"]) if p["kind"] == "config"][0]

    resposta = auth_client.post(
        "/api/recipes/birdtunes-cast/run", json={"device_id": device_id, "step": indice}
    )
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["values"] == {
        "apps.settings.birdtunes.output.type": "chromecast",
        "apps.settings.birdtunes.output.device_id": device_id,
    }

    # E o app, do outro lado, lê exatamente isso: a tela de saída já abre com o
    # backend certo, e é esse device_id que o player resolve na hora de tocar.
    assert auth_client.get("/api/apps/birdtunes/outputs").json()["current"] == "chromecast"
    config = auth_client.app.state.config
    assert config.get("apps.settings.birdtunes.output.device_id") == device_id


def test_rodar_o_passo_do_home_assistant_grava_onde_a_integracao_le(auth_client):
    device_id = _aparelho(auth_client, "home_assistant", name="HA", port=8123)
    receita = recipes.get("home-assistant-found")
    indice = [i for i, p in enumerate(receita["steps"]) if p["kind"] == "config"][0]

    resposta = auth_client.post(
        "/api/recipes/home-assistant-found/run",
        json={"device_id": device_id, "step": indice},
    )
    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["key"] == "integrations.home_assistant.url"

    # A tela de Integrações mostra o endereço que o passo acabou de gravar.
    estado = auth_client.get("/api/home").json()
    assert estado["url"] == "http://192.168.1.60:8123"


def test_rodar_um_passo_bloqueado_da_o_motivo_e_nao_um_erro_generico(auth_client):
    device_id = _aparelho(auth_client, "mqtt_broker", name="Broker", port=1883)
    resposta = auth_client.post(
        "/api/recipes/mqtt-broker-found/run", json={"device_id": device_id, "step": 0}
    )
    assert resposta.status_code == 501
    assert resposta.json()["error"] == "installer_pending"
    assert "ainda não foi feito" in resposta.json().get("message", resposta.text)
