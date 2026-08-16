"""Metade da rede dele aparecia como "desconhecido", e parte disso não tem conserto.

Medido na casa dele em 16/08/2026: 26 aparelhos, 13 sem nome. Desses 13, **seis
têm MAC sorteado pelo próprio aparelho** -- o bit "locally administered" ligado,
que é o que todo celular e notebook moderno faz para não ser seguido de rede em
rede. Um endereço desses não pertence a fabricante nenhum: instalar a base OUI
da IEEE, que é o que a tela oferecia, não muda uma linha para eles.

Então a tela prometia um conserto que não acontece em quase metade dos casos. O
que este teste amarra: o bit é lido do próprio MAC (sem base de dados, sem
rede), esses aparelhos saem da conta que dispara o convite de instalar, e a
coluna do fabricante passa a dizer "endereço privado" em vez de ficar muda.

Os seis da casa dele, para o registro: ``da:4e:10:b8:53:cb``, ``f6:34:f0:70:54:c9``,
``12:47:66:c5:54:4d``, ``be:ba:ec:e6:06:f3``, ``1e:fe:54:3b:4b:b0`` e
``f6:34:f0:6f:7c:be``.
"""

from __future__ import annotations

import io
import os

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------- o bit no MAC


@pytest.mark.parametrize("mac", [
    "da:4e:10:b8:53:cb",
    "f6:34:f0:70:54:c9",
    "12:47:66:c5:54:4d",
    "be:ba:ec:e6:06:f3",
    "1e:fe:54:3b:4b:b0",
    "f6:34:f0:6f:7c:be",
])
def test_os_seis_da_casa_dele_sao_privados(mac):
    from project_os.core import lan

    assert lan.mac_privado(mac) is True


@pytest.mark.parametrize("mac", [
    "c4:eb:ff:e6:50:a9",   # o roteador
    "c4:de:e2:11:ee:94",
    "10:b4:1d:c6:a7:e4",
    "9c:64:8b:58:bf:aa",
    "70:ae:d5:38:75:c9",
    "68:ef:dc:a3:e2:e9",
    "90:6a:eb:8d:3b:6c",
    "b8:27:eb:cc:27:e9",   # o próprio Pi
])
def test_e_os_outros_sete_tem_fabricante_de_verdade(mac):
    """Esses a base da IEEE resolve -- o convite de instalar faz sentido por eles."""
    from project_os.core import lan

    assert lan.mac_privado(mac) is False


@pytest.mark.parametrize("entrada", ["", None, "nada", ":", "z"])
def test_lixo_nao_vira_privado(entrada):
    from project_os.core import lan

    assert lan.mac_privado(entrada) is False


def test_o_formato_do_mac_nao_importa():
    from project_os.core import lan

    assert lan.mac_privado("DA:4E:10:B8:53:CB") is True
    assert lan.mac_privado("da-4e-10-b8-53-cb") is True
    assert lan.mac_privado("da4e10b853cb") is True


# ------------------------------------------------------------------ a conta


class _Aparelho(object):
    def __init__(self, mac, vendor=""):
        self.properties = {"mac": mac, "vendor": vendor}


def test_o_convite_de_instalar_conta_so_quem_a_base_resolveria(monkeypatch):
    from project_os.core import discovery, lan

    monkeypatch.setattr(lan, "oui_available", lambda: {"available": False})
    registro = discovery.DeviceRegistry.__new__(discovery.DeviceRegistry)
    estado = registro._estado_dos_fabricantes([
        _Aparelho("da:4e:10:b8:53:cb"),   # privado
        _Aparelho("f6:34:f0:70:54:c9"),   # privado
        _Aparelho("c4:eb:ff:e6:50:a9"),   # a base resolveria
        _Aparelho("90:6a:eb:8d:3b:6c"),   # a base resolveria
        _Aparelho("9c:64:8b:58:bf:aa", vendor="Já tem nome"),
    ])

    assert estado["unnamed"] == 2, "só os que instalar a base resolve"
    assert estado["private_macs"] == 2


# -------------------------------------------------------------------- a tela


def test_a_lista_diz_endereco_privado_em_vez_de_nada():
    fonte = io.open(os.path.join(RAIZ, "web", "views", "devices.js"), encoding="utf-8").read()
    corpo = fonte[fonte.index("function vendorOf("):]
    corpo = corpo[:corpo.index("\nfunction statusBadges")]
    assert "props.private_mac" in corpo
    assert "devices.vendor.private" in corpo


def test_e_nao_oferece_botao_que_nao_resolveria():
    fonte = io.open(os.path.join(RAIZ, "web", "views", "devices.js"), encoding="utf-8").read()
    assert "fab.unnamed === 0 && fab.private_macs > 0" in fonte


def test_o_texto_existe_nas_duas_linguas():
    pt = io.open(os.path.join(RAIZ, "web", "lib", "strings-pt.js"), encoding="utf-8").read()
    en = io.open(os.path.join(RAIZ, "web", "views", "devices.js"), encoding="utf-8").read()
    for chave in ("'devices.vendor.private':", "'devices.vendors.private':"):
        assert chave in pt, chave
        assert chave in en, chave


def test_o_aparelho_leva_a_marca_ate_a_tela():
    """Derivado no servidor, não recalculado no navegador: uma regra, um lugar."""
    fonte = io.open(os.path.join(RAIZ, "project_os", "core", "discovery.py"), encoding="utf-8").read()
    corpo = fonte[fonte.index("    def to_dict(self)"):]
    corpo = corpo[:corpo.index("    @classmethod")]
    assert 'props["private_mac"] = lan.mac_privado(mac)' in corpo
    assert 'not props.get("vendor")' in corpo, "com fabricante conhecido, a marca não faz falta"
