"""Um ajudante pareado tinha uma única coisa a fazer na vida: responder um ping.

A tela e o README vendem "empresta um PC pro Pi", e o ajudante aparece com as
etiquetas certas -- processamento, converter vídeo e áudio, sensores, relés.
Só que o único lugar do repositório que criava tarefa era o ``POST
/helpers/jobs``, e o único chamador dele na interface era o botão *Submit test
job*, que manda ``{"kind": "ping"}``. Nenhum app, nenhuma rotina, nenhum
serviço do Pi enfileirava nada.

O ESP32 era o caso mais claro: o firmware manda a leitura do sensor e o clique
do botão a cada 15 segundos, e nada em ``web/`` lia ``facts`` -- a temperatura
do quarto dos passarinhos virava um número sobrescrito numa linha do SQLite. O
mesmo firmware já sabia executar ``read`` e ``relay`` desde o primeiro dia;
faltava alguém pedir.

E faltava endereço: a fila entrega para quem puder fazer, o que é certo para
"converta este vídeo" e errado para "ligue o relé", que é *daquela* placa.
"""

from __future__ import annotations

import json

import pytest


def _parear(client, kind="esp32", capabilities=("sensor", "actuator")):
    code = client.post("/api/helpers/codes", json={"kind": kind}).json()["code"]
    resposta = client.post(
        "/api/helpers/pair",
        json={"code": code, "name": "esp da varanda", "kind": kind,
              "capabilities": list(capabilities), "facts": {"board": "esp32"}},
    )
    assert resposta.status_code in (200, 201), resposta.text
    return resposta.json()


def test_a_tarefa_endereçada_so_e_pega_pelo_dono(auth_client):
    um = _parear(auth_client)
    outro = _parear(auth_client)

    criada = auth_client.post(
        "/api/helpers/jobs",
        json={"kind": "relay", "payload": {"on": True}, "needs": ["actuator"],
              "helper_id": um["helper"]["id"]},
    )
    assert criada.status_code == 200, criada.text

    # O outro ESP32 bate primeiro e não pode levar a tarefa embora: com dois na
    # casa, a lâmpada acesa seria a do cômodo errado.
    batida = auth_client.post(
        "/api/helpers/agent/heartbeat", json={"token": outro["token"], "facts": {}}
    )
    assert batida.status_code == 200, batida.text
    assert batida.json()["job"] is None

    minha = auth_client.post(
        "/api/helpers/agent/heartbeat", json={"token": um["token"], "facts": {}}
    )
    tarefa = minha.json()["job"]
    assert tarefa is not None
    assert tarefa["kind"] == "relay"
    assert tarefa["payload"] == {"on": True}


def test_tarefa_sem_endereco_continua_indo_pra_quem_puder(auth_client):
    """Não foi trocado um comportamento pelo outro: os dois existem."""
    um = _parear(auth_client)
    auth_client.post(
        "/api/helpers/jobs", json={"kind": "ping", "payload": {}, "needs": []}
    )
    batida = auth_client.post(
        "/api/helpers/agent/heartbeat", json={"token": um["token"], "facts": {}}
    )
    assert batida.json()["job"]["kind"] == "ping"


def test_endereco_inexistente_e_recusado_na_hora(auth_client):
    resposta = auth_client.post(
        "/api/helpers/jobs",
        json={"kind": "relay", "payload": {}, "needs": [], "helper_id": "nao-existe"},
    )
    assert resposta.status_code == 404
    assert resposta.json()["error"] == "helper_not_found"


def test_o_que_o_esp32_conta_fica_guardado_e_visivel(auth_client):
    par = _parear(auth_client)
    auth_client.post(
        "/api/helpers/agent/heartbeat",
        json={"token": par["token"], "facts": {"reading": 21500, "button": "pressed"}},
    )
    lista = auth_client.get("/api/helpers").json()["helpers"]
    meu = [h for h in lista if h["id"] == par["helper"]["id"]][0]
    assert meu["facts"]["reading"] == 21500
    assert meu["facts"]["button"] == "pressed"


def test_a_tela_mostra_os_fatos_e_os_botoes():
    import os

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "web", "views", "helpers.js"), encoding="utf-8") as arquivo:
        texto = arquivo.read()
    assert "helper.facts" in texto, "a tela ainda não lê os fatos"
    assert "'relay'" in texto and "'read'" in texto, "a tela não manda trabalho de verdade"
    assert "helper_id: helper.id" in texto, "o trabalho não é endereçado"


def test_a_batida_http_avisa_a_tela(auth_client):
    """A tela escutava eventos que só o websocket emitia -- e os dois agentes
    que este projeto entrega falam por HTTP."""
    par = _parear(auth_client)
    db = auth_client.app.state.db
    # Envelhece a última batida: é assim que um ajudante fica offline, sem
    # evento nenhum -- ele simplesmente para de bater.
    db.execute(
        "UPDATE helpers SET last_seen = ? WHERE id = ?",
        ("2020-01-01T00:00:00Z", par["helper"]["id"]),
    )

    recebidos = []
    bus = auth_client.app.state.bus
    bus.publish_nowait = lambda topico, dados: recebidos.append((topico, dados))

    auth_client.post(
        "/api/helpers/agent/heartbeat",
        json={"token": par["token"], "facts": {"reading": 300}},
    )
    topicos = [t for t, _ in recebidos]
    assert "helpers.online" in topicos, topicos
    assert "helpers.event" in topicos, topicos


def test_quem_ja_estava_online_nao_vira_evento_a_cada_15s(auth_client):
    """Um ESP32 bate quatro vezes por minuto; anunciar "entrou" toda vez faria
    a tela recarregar sozinha o dia inteiro."""
    par = _parear(auth_client)
    auth_client.post("/api/helpers/agent/heartbeat", json={"token": par["token"]})

    recebidos = []
    auth_client.app.state.bus.publish_nowait = lambda t, d: recebidos.append((t, d))
    auth_client.post("/api/helpers/agent/heartbeat", json={"token": par["token"]})
    assert "helpers.online" not in [t for t, _ in recebidos]


def test_a_loja_nao_promete_mais_offload_que_nao_existe():
    """Três cartões diziam que a conversão/compilação/OCR sairia da placa."""
    import os

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "project_os", "data", "catalog.yaml"), encoding="utf-8") as arquivo:
        texto = arquivo.read()
    for frase in (
        "manda a compilação",
        "a conversão vai pra",
        "com um ajudante ligado, ele faz essa parte",
    ):
        assert frase not in texto, "a loja ainda promete: %s" % frase
