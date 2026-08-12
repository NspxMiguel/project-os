"""O botão de Registros de cada serviço e de cada app caía sempre em tela vazia.

Dois furos diferentes, um em cada metade da tela de Serviços:

**Apps.** O link era ``#/logs?source=app.birdtunes`` e o filtro do banco é
``source LIKE 'app.birdtunes%'``. Só que o que fica gravado na coluna ``source``
é o nome do logger Python, e o de um app é ``project_os.app.birdtunes``
(``core/plugins.py``). Prefixo que nunca casa, em máquina nenhuma -- e
``GET /api/apps/{id}/logs`` usava exatamente o mesmo prefixo errado, então a
aba de registros do app também vinha vazia.

**Unidades do systemd.** Essas nunca escreveram uma linha na tabela ``log``: o
project-os só grava o que ele mesmo loga. Quem tem o registro do mosquitto, do
home-assistant e do próprio project-os é o journald. Não havia nada no código
que falasse com o journal, e a tela oferecia um botão de Registros para cada
unidade assim mesmo.
"""

from __future__ import annotations

import os

import pytest


def test_o_filtro_acha_o_log_do_app_pelo_prefixo_curto(db):
    db.add_log("INFO", "project_os.app.birdtunes", "tocando alvorada")
    db.add_log("INFO", "project_os.core.plugins", "outra coisa qualquer")

    linhas = db.recent_log(source="app.birdtunes")
    assert [l["message"] for l in linhas] == ["tocando alvorada"]


def test_o_prefixo_longo_continua_valendo(db):
    """Quem digitar o nome do logger inteiro no filtro da tela também acha."""
    db.add_log("INFO", "project_os.app.birdtunes", "tocando alvorada")
    assert db.recent_log(source="project_os.app.birdtunes")


def test_o_filtro_nao_pega_o_app_errado(db):
    db.add_log("INFO", "project_os.app.birdtunes", "meu")
    db.add_log("INFO", "project_os.app.whatsapp-bot", "do outro")
    linhas = db.recent_log(source="app.birdtunes")
    assert [l["message"] for l in linhas] == ["meu"]


def test_a_rota_de_log_do_app_devolve_as_linhas(auth_client):
    """O caminho inteiro: o que o app logou aparece em /api/apps/{id}/logs."""
    auth_client.app.state.db.add_log(
        "INFO", "project_os.app.birdtunes", "acordei os passarinhos"
    )
    resposta = auth_client.get("/api/apps/birdtunes/logs")
    assert resposta.status_code == 200, resposta.text
    mensagens = [l["message"] for l in resposta.json()["lines"]]
    assert "acordei os passarinhos" in mensagens


def test_a_tela_de_servicos_manda_unidade_para_o_journal():
    """Unidade do systemd vai por ?unit=, não por ?source= -- são bancos diferentes."""
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "web", "views", "services.js"), encoding="utf-8") as arquivo:
        texto = arquivo.read()
    assert "#/logs?unit=" in texto
    # E o app continua indo pelo banco do próprio project-os.
    assert "#/logs?source=" in texto


def test_journalctl_leva_sudo_quando_nao_sou_root(monkeypatch):
    from project_os.core import updates

    monkeypatch.setattr(updates.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(updates.shutil, "which", lambda nome: "/usr/bin/%s" % nome)
    assert updates.systemctl_argv(binary="journalctl") == ["sudo", "-n", "journalctl"]


def test_o_journal_vira_a_mesma_forma_que_a_tela_desenha():
    from project_os.api import system

    saida = (
        "2026-08-12T15:04:05+0000 pos mosquitto[412]: mosquitto version 2.0.11 starting\n"
        "2026-08-12T15:04:06+0000 pos mosquitto[412]: Error: Address already in use\n"
        "-- Boot 3f2a --\n"
    )
    linhas = system._parse_journal(saida)
    assert len(linhas) == 3
    assert linhas[0]["source"] == "mosquitto[412]"
    assert linhas[0]["message"].startswith("mosquitto version")
    assert linhas[1]["level"] == "ERROR"
    # A linha que não é do formato não some -- vira mensagem.
    assert linhas[2]["message"] == "-- Boot 3f2a --"


def test_unidade_de_fora_da_lista_e_recusada(auth_client):
    resposta = auth_client.get("/api/system/logs/unit/sshd")
    assert resposta.status_code == 403
    assert resposta.json()["error"] == "unit_not_managed"


def test_sem_journalctl_a_resposta_diz_isso(auth_client, monkeypatch):
    """Numa máquina sem journal, dizer isso é melhor que uma lista vazia."""
    from project_os.api import system

    monkeypatch.setattr(system.shutil, "which", lambda nome: None)
    resposta = auth_client.get("/api/system/logs/unit/mosquitto")
    assert resposta.status_code == 503
    assert resposta.json()["error"] == "no_journal"
