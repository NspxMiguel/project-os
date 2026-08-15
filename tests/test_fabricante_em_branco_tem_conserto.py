"""Vinte e oito linhas chamadas "10.0.0.117", e a tela não dizia por quê.

Medido na rede de casa: 28 aparelhos de tipo ``unknown``, todos com MAC, nenhum
com fabricante. Dezenove desses MACs são fixos -- ou seja, dariam nome se
houvesse contra o que consultar.

A razão é uma decisão do projeto, e está certa: os três primeiros bytes de um
MAC só viram "Samsung" contra o registro da IEEE, que não é nosso para embutir
nem para chutar. ``lan.oui_available()`` já dizia isso em português, com o
pacote que resolve (``ieee-data``)...  e não era chamado por ninguém. A coluna
aparecia vazia e calada, e quem quisesse consertar tinha que adivinhar sozinho
que existe um pacote com esse nome.

Agora o estado sai no resumo de ``GET /api/devices`` e a tela mostra o motivo
junto de um botão que instala pelo mesmo apt da tela de Programas.
"""

from __future__ import annotations

import io
import json
import os

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELA = os.path.join(RAIZ, "web", "views", "devices.js")


def _aparelho(client, device_id, mac=None, vendor=""):
    props = {}
    if mac:
        props["mac"] = mac
        props["vendor"] = vendor
    client.app.state.db.execute(
        "INSERT INTO devices (id, kind, name, address, port, properties, capabilities,"
        " first_seen, last_seen, pinned, ignored) VALUES (?,?,?,?,?,?,?,?,?,0,0)",
        (device_id, "unknown", device_id, "10.0.0.9", 0, json.dumps(props), "[]",
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )


# --------------------------------------------------------------------------- o estado


def test_o_resumo_diz_se_da_pra_saber_o_fabricante(auth_client):
    corpo = auth_client.get("/api/devices").json()
    fab = corpo["summary"]["vendors"]
    assert set(["available", "reason", "install_hint", "unnamed", "package"]) <= set(fab)
    assert fab["package"] == "ieee-data"


def test_sem_a_base_o_motivo_vem_junto(auth_client, monkeypatch):
    from project_os.core import lan

    monkeypatch.setattr(lan, "_load_oui", lambda: {})
    lan.reset_cache()
    fab = auth_client.get("/api/devices").json()["summary"]["vendors"]
    assert fab["available"] is False
    assert "IEEE" in fab["reason"]
    assert "ieee-data" in fab["install_hint"]


def test_conta_so_quem_tem_mac_e_nao_tem_fabricante(auth_client, monkeypatch):
    from project_os.core import lan

    monkeypatch.setattr(lan, "_load_oui", lambda: {})
    lan.reset_cache()
    _aparelho(auth_client, "sem-nome-1", mac="c4:eb:ff:e6:50:a9")
    _aparelho(auth_client, "sem-nome-2", mac="a8:51:ab:11:22:33")
    _aparelho(auth_client, "com-nome", mac="00:11:22:33:44:55", vendor="Samsung")
    _aparelho(auth_client, "sem-mac-nenhum")

    fab = auth_client.get("/api/devices").json()["summary"]["vendors"]
    assert fab["unnamed"] == 2, "o que tem fabricante e o que nem MAC tem não contam"


def test_com_a_base_instalada_o_aviso_some(auth_client, monkeypatch):
    from project_os.core import lan

    monkeypatch.setattr(lan, "_load_oui", lambda: {"c4ebff": "Fabricante Qualquer"})
    lan.reset_cache()
    _aparelho(auth_client, "sem-nome-1", mac="c4:eb:ff:e6:50:a9")
    fab = auth_client.get("/api/devices").json()["summary"]["vendors"]
    assert fab["available"] is True
    assert fab["install_hint"] is None


def test_o_chute_continua_proibido():
    """A regra que motiva tudo: em branco é melhor que inventado."""
    from project_os.core import lan

    lan.reset_cache()
    assert lan.vendor_of("62:47:ee:e4:db:8b") in (None, "")


# --------------------------------------------------------------------------- a tela


def _fonte():
    return io.open(TELA, encoding="utf-8").read()


def test_o_resumo_diz_se_da_pra_instalar_daqui(auth_client, monkeypatch):
    """Botão só onde ele pode funcionar: numa máquina sem apt, vale o texto."""
    from project_os.core import lan, packages

    monkeypatch.setattr(lan, "_load_oui", lambda: {})
    lan.reset_cache()
    monkeypatch.setattr(
        packages, "backends",
        lambda: [{"id": "apt", "can_install": False, "reason": "O apt não existe nesta máquina."}],
    )
    fab = auth_client.get("/api/devices").json()["summary"]["vendors"]
    assert fab["can_install"] is False
    assert "apt" in fab["install_reason"]

    monkeypatch.setattr(
        packages, "backends", lambda: [{"id": "apt", "can_install": True, "reason": ""}]
    )
    fab = auth_client.get("/api/devices").json()["summary"]["vendors"]
    assert fab["can_install"] is True


def test_a_tela_troca_o_botao_por_texto_quando_nao_da_pra_instalar():
    fonte = _fonte()
    assert "fab.can_install === false" in fonte
    corte = fonte.index("fab.can_install === false")
    trecho = fonte[corte:corte + 700]
    assert "install_hint" in trecho, "sem botão, sobra o comando escrito"


def test_a_tela_mostra_o_aviso_e_o_botao():
    fonte = _fonte()
    assert "vendorNotice" in fonte
    assert "devices.vendors.install" in fonte
    # O botão usa o instalador que já existe, não uma segunda fila invisível.
    assert "/packages/install" in fonte


def test_o_aviso_so_aparece_quando_ha_o_que_consertar():
    fonte = _fonte()
    assert "fab.available === false && fab.unnamed > 0" in fonte


def test_o_fabricante_aparece_nas_duas_telas():
    """Lista e ficha leem a mesma função -- e ela é do módulo, não do closure.

    A primeira versão definiu ``vendorOf`` dentro do closure da lista: a lista
    funcionava e a ficha do aparelho respondia *"vendorOf is not defined"*, um
    erro que teste de sintaxe nenhum pega. Só apareceu abrindo a tela.
    """
    fonte = _fonte()
    assert "\nfunction vendorOf(device) {" in fonte, "vendorOf tem que estar no escopo do módulo"
    assert fonte.count("vendorOf(device)") >= 3, "a lista e a ficha usam a mesma função"
    assert "devices.detail.info.vendor" in fonte


def test_a_frase_existe_em_portugues():
    pt = io.open(os.path.join(RAIZ, "web", "lib", "strings-pt.js"), encoding="utf-8").read()
    for chave in ("devices.vendors.title", "devices.vendors.install", "devices.vendors.started"):
        assert chave in pt


def test_instalar_respeita_o_interruptor_de_programas(auth_client):
    """O mesmo freio da tela de Programas: o botão não passa por fora dele."""
    auth_client.put(
        "/api/settings", json={"values": {"security.allow_package_management": False}}
    )
    resposta = auth_client.post(
        "/api/packages/install", json={"source": "apt", "package": "ieee-data"}
    )
    assert resposta.status_code == 403
    assert resposta.json()["error"] == "package_management_disabled"
