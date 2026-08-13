"""Trinta e quatro itens na loja, quatro instalavam.

A conta era: 5 builtins (dos quais 2 escritos), 5 contêineres (2 com bloco
``container:``), 23 serviços e uma receita. Os 23 serviços tinham botão
Instalar, e o botão respondia ``installer_pending`` -- *depois* do clique, com
um cartão que não avisava nada antes.

Duas metades de conserto, e as duas estão aqui:

* o que dá para instalar de verdade passa a instalar: cinco desses serviços são
  pacote do Debian (mosquitto, samba, syncthing, wireguard, docker.io) e a tela
  de Programas já instala pacote com apt, com log ao vivo e job id. A loja usa
  esse mesmo instalador -- não uma segunda fila invisível;
* o que não dá diz no cartão, antes do clique, e com o que fazer em vez disso.
"""

from __future__ import annotations

import pytest


def _item(client, app_id):
    corpo = client.get("/api/store").json()
    for item in corpo["items"]:
        if item["id"] == app_id:
            return item
    raise AssertionError("%s não está na loja" % app_id)


def test_servico_com_pacote_debian_se_diz_instalavel(auth_client):
    assert _item(auth_client, "mosquitto")["installable"] is True
    assert _item(auth_client, "mosquitto")["install_package"] == "mosquitto"


def test_servico_sem_instalador_avisa_no_cartao(auth_client):
    item = _item(auth_client, "home-assistant")
    assert item["installable"] is False
    # E o motivo diz o que fazer, não só que não dá.
    assert "instalador de serviço" in item["install_reason"]
    assert "Terminal" in item["install_reason"] or "Programas" in item["install_reason"]


def test_a_receita_nao_finge_ter_botao(auth_client):
    item = _item(auth_client, "minecraft")
    assert item["installable"] is False
    assert "receita" in item["install_reason"]


def test_instalar_um_servico_vira_uma_tarefa_de_apt(auth_client, monkeypatch):
    from project_os.api import packages as packages_api

    chamadas = []

    class TarefaFalsa(object):
        id = "job-1"

        def as_dict(self, tail=0):
            return {"id": self.id, "action": "install", "package": "mosquitto",
                    "state": "running"}

    class RunnerFalso(object):
        def start(self, action, source, package):
            chamadas.append((action, source, package))
            return TarefaFalsa()

    monkeypatch.setattr(packages_api, "runner", lambda: RunnerFalso())

    resposta = auth_client.post("/api/store/mosquitto/install", json={})
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    # "Começou", não "instalado": apt-get num Pi 3 leva minutos.
    assert corpo["started"] is True
    assert corpo["installed"] is False
    assert corpo["watch"] == "/api/packages/jobs/job-1"
    assert chamadas == [("install", "apt", "mosquitto")]


def test_a_loja_respeita_o_interruptor_de_instalar_programas(auth_client, monkeypatch):
    auth_client.put(
        "/api/settings", json={"values": {"security.allow_package_management": False}}
    )
    resposta = auth_client.post("/api/store/mosquitto/install", json={})
    assert resposta.status_code == 403
    assert resposta.json()["error"] == "package_management_disabled"


def test_servico_sem_pacote_continua_recusando_com_motivo(auth_client):
    resposta = auth_client.post("/api/store/home-assistant/install", json={})
    assert resposta.status_code == 501
    assert resposta.json()["error"] == "installer_pending"


def test_a_conta_da_loja_bate_com_a_realidade(auth_client):
    """Quantos itens hoje dizem que instalam -- e por que cada um diz isso."""
    itens = auth_client.get("/api/store").json()["items"]
    instalaveis = [i for i in itens if i.get("installable")]
    for item in instalaveis:
        if item["kind"] == "builtin":
            continue  # o app existe como código
        if item["kind"] == "container":
            assert item.get("container"), item["id"]
        elif item["kind"] == "service":
            assert item.get("install_package"), item["id"]
        else:
            raise AssertionError("%s diz que instala e não sabe como" % item["id"])
    # Nenhum item pode ficar sem resposta: ou instala, ou explica.
    for item in itens:
        if not item.get("installable"):
            assert item.get("install_reason"), item["id"]


def test_a_busca_acha_pelo_id_que_as_outras_telas_mandam(auth_client):
    """`#/store/home-assistant` e o selo "Está na loja" linkam por id."""
    corpo = auth_client.get("/api/store", params={"q": "home-assistant"}).json()
    assert [i["id"] for i in corpo["items"]] == ["home-assistant"]
