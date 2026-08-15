"""O ajudante sabia fazer quatro coisas; a tela só sabia pedir uma.

``agents/helper_agent.py`` executa ``ping``, ``facts``, ``transcode`` e
``download``, e o firmware do ESP32 executa ``read`` e ``relay``. A tela de
Ajudantes mostrava, para cada máquina pareada, a lista do que ela oferece --
*"baixar arquivos grandes, converter vídeo e áudio"* -- e não tinha botão
nenhum para pedir nem uma coisa nem outra. Um rótulo assim é uma promessa: quem
pareou o PC justamente para ele baixar coisa pesada ficava olhando para a
palavra.

Este arquivo cobre os dois lados:

* a tela oferece um botão para cada tipo de tarefa que algum ajudante sabe
  executar (senão o rótulo volta a ser decoração);
* a tarefa enfileirada chega com o tipo, o destino e a capacidade certos -- e a
  resposta diz se existe alguém capaz de pegá-la, em vez de sumir na fila.
"""

from __future__ import annotations

import io
import os
import re

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELA = os.path.join(RAIZ, "web", "views", "helpers.js")


def _fonte(caminho):
    return io.open(caminho, encoding="utf-8").read()


def _tipos_que_o_agente_executa(caminho):
    """Os ``kind`` que aparecem como ``if kind == "x"`` no agente."""
    fonte = _fonte(caminho)
    return set(re.findall(r'kind == ["\']([a-z_]+)["\']', fonte))


def _tipos_que_a_tela_pede():
    fonte = _fonte(TELA)
    return set(re.findall(r"enviarTarefa\([^,]+,\s*'([a-z_]+)'", fonte))


def test_a_tela_sabe_pedir_tudo_que_o_pc_sabe_fazer():
    tipos = _tipos_que_o_agente_executa(os.path.join(RAIZ, "agents", "helper_agent.py"))
    # ping e facts são do próprio protocolo (o servidor manda sozinho), não
    # trabalho que alguém pede da tela.
    tipos -= {"ping", "facts"}
    faltando = tipos - _tipos_que_a_tela_pede()
    assert not faltando, "o ajudante faz isto e a tela não sabe pedir: %s" % faltando


def test_a_tela_sabe_pedir_tudo_que_o_esp32_sabe_fazer():
    tipos = _tipos_que_o_agente_executa(os.path.join(RAIZ, "agents", "esp32", "main.py"))
    tipos -= {"ping", "facts"}
    faltando = tipos - _tipos_que_a_tela_pede()
    assert not faltando, "o ESP32 faz isto e a tela não sabe pedir: %s" % faltando


def test_a_tela_nao_pede_nada_que_ninguem_saiba_fazer():
    """O contrário também: um botão que manda um tipo que ninguém executa."""
    conhecidos = _tipos_que_o_agente_executa(
        os.path.join(RAIZ, "agents", "helper_agent.py")
    ) | _tipos_que_o_agente_executa(os.path.join(RAIZ, "agents", "esp32", "main.py"))
    sobrando = _tipos_que_a_tela_pede() - conhecidos
    assert not sobrando, "a tela pede tarefas que nenhum ajudante executa: %s" % sobrando


def test_o_aviso_de_que_o_arquivo_fica_la_esta_na_tela():
    """Não há transporte de volta -- e isso é dito antes, não depois."""
    fonte = _fonte(TELA)
    assert "helpers.act.staysThere" in fonte
    pt = _fonte(os.path.join(RAIZ, "web", "lib", "strings-pt.js"))
    linha = [l for l in pt.splitlines() if "helpers.act.staysThere" in l][0]
    assert "não traz o resultado de volta" in linha


# --------------------------------------------------------------------------- pela API


def _parear(client, capacidades, kind="pc"):
    codigo = client.post("/api/helpers/codes", json={}).json()["code"]
    resposta = client.post(
        "/api/helpers/pair",
        json={
            "code": codigo, "name": "PC da sala", "kind": kind,
            "platform": "linux", "capabilities": capacidades,
        },
    )
    assert resposta.status_code in (200, 201), resposta.text
    return resposta.json()["helper"]


def test_enfileirar_um_download_para_um_ajudante_especifico(auth_client):
    ajudante = _parear(auth_client, ["download", "transcode", "cpu"])
    resposta = auth_client.post(
        "/api/helpers/jobs",
        json={
            "kind": "download",
            "payload": {"url": "https://exemplo.invalido/arquivo.iso"},
            "needs": ["download"],
            "helper_id": ajudante["id"],
        },
    )
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["job"]["kind"] == "download"
    assert corpo["job"]["payload"]["url"].endswith("arquivo.iso")
    assert corpo["will_run"] is True, "há um ajudante pareado que sabe baixar"

    # E ela aparece para o próprio ajudante quando ele vem buscar trabalho.
    fila = auth_client.get("/api/helpers/jobs").json()["jobs"]
    assert [j["kind"] for j in fila] == ["download"]


def test_enfileirar_conversao_com_entrada_e_saida(auth_client):
    ajudante = _parear(auth_client, ["transcode"])
    resposta = auth_client.post(
        "/api/helpers/jobs",
        json={
            "kind": "transcode",
            "payload": {"input": "/mnt/midia/a.mkv", "output": "/mnt/midia/a.mp4"},
            "needs": ["transcode"],
            "helper_id": ajudante["id"],
        },
    )
    assert resposta.status_code == 200, resposta.text
    carga = resposta.json()["job"]["payload"]
    assert carga["input"].endswith(".mkv") and carga["output"].endswith(".mp4")


def test_pedir_o_que_ninguem_oferece_diz_que_ninguem_pega(auth_client):
    """Enfileirar sem ninguém capaz é permitido -- desde que a resposta avise."""
    _parear(auth_client, ["cpu"])
    resposta = auth_client.post(
        "/api/helpers/jobs",
        json={"kind": "transcode", "payload": {}, "needs": ["transcode"]},
    )
    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["will_run"] is False


def test_capacidade_inventada_e_recusada(auth_client):
    resposta = auth_client.post(
        "/api/helpers/jobs",
        json={"kind": "download", "payload": {}, "needs": ["telepatia"]},
    )
    assert resposta.status_code == 400
    assert resposta.json()["error"] == "unknown_capability"
