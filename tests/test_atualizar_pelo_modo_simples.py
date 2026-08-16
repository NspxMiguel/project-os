"""No modo padrão, a caixa não tinha por onde ser atualizada.

*"como checo atualizacao? como atualizo? msm versao a anos"* -- com um print de
Configurações > Atualizações e sobre, no modo Simples, mostrando o número da
versão, uma frase sobre a Loja e um link para ``/api/docs``. Nenhum botão.

A tela que procura e instala existe e funciona, só que ela mora no
``NAV_ADVANCED``: no modo Simples não há item de menu para ela, e a aba de
Configurações que se chama "Atualizações e sobre" não atualizava nada. Quem não
soubesse digitar ``#/updates`` na barra de endereço ficava na versão de fábrica
para sempre -- e ficar parado numa versão antiga não é uma escolha que alguém
fez.

A rota nunca dependeu do modo; o que faltava era porta. Agora a aba procura ali
mesmo, e leva para a tela de Atualizações na hora de instalar -- porque é ela que
sabe seguir o registro do trabalho, esperar o reinício e voltar versão, e manter
duas cópias de uma coisa que reinicia o serviço no meio seria pior que o defeito.
"""

from __future__ import annotations

import io
import os
import re

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(RAIZ, "web", "app.js")
SETTINGS = os.path.join(RAIZ, "web", "views", "settings.js")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")


def _ler(caminho):
    return io.open(caminho, encoding="utf-8").read()


def _bloco(fonte, nome):
    """O corpo de uma função do arquivo, até a linha que fecha na mesma coluna."""
    achado = re.search(r"\n(\s*)function %s\(\) \{\n(.*?)\n\1\}\n" % nome, fonte, re.S)
    assert achado, "não achei a função %s" % nome
    return achado.group(2)


# --------------------------------------------------------------------------- o buraco


def test_a_tela_de_atualizacoes_continua_fora_do_menu_simples():
    """Não é o que se conserta aqui -- é a premissa que obriga a porta a existir.

    O menu Simples é uma tela por assunto, de propósito. Se um dia alguém puser
    Atualizações nele, este teste cai e o de baixo passa a ser redundante -- que
    é a hora certa de reler os dois.
    """
    fonte = _ler(APP)
    simples = re.search(r"const NAV_SIMPLE = \[(.*?)\];", fonte, re.S).group(1)
    avancado = re.search(r"const NAV_ADVANCED = \[(.*?)\];", fonte, re.S).group(1)
    assert "'updates'" not in simples
    assert "'updates'" in avancado


def test_a_rota_nao_depende_do_modo():
    """É o caminho que funciona hoje no cartão dele: digitar #/updates."""
    fonte = _ler(APP)
    assert "'#/updates': shellRoute('updates'" in fonte


# --------------------------------------------------------------------------- a porta


def test_a_aba_procura_atualizacao_ali_mesmo():
    fonte = _ler(SETTINGS)
    assert "async function procurarAtualizacao()" in fonte
    assert "api.post('/updates/check'" in fonte
    corpo = _bloco(fonte, "updatesSection")
    assert "procurarAtualizacao()" in corpo, "o botão de procurar não chama nada"


def test_a_aba_leva_para_a_tela_que_instala():
    """Instalar reinicia o serviço; quem sabe seguir isso é a tela de Atualizações."""
    corpo = _bloco(_ler(SETTINGS), "updatesSection")
    assert "href: '#/updates'" in corpo
    assert "api.post('/updates/install'" not in corpo, (
        "duas cópias do instalar é duas coisas para consertar quando ele quebrar"
    )


def test_o_botao_de_instalar_diz_qual_versao():
    """A tela mostrava "Instalar a {version}", com as chaves na cara de quem olha."""
    corpo = _bloco(_ler(SETTINGS), "updatesSection")
    achado = re.search(r"t\('settings\.updates\.install'([^)]*)\)", corpo)
    assert achado, "o botão de instalar sumiu"
    assert "version:" in achado.group(1), (
        "t() sem o valor deixa o {version} cru na tela -- foi o que apareceu no navegador"
    )


def test_o_estado_da_procura_e_da_tela_e_nao_global():
    fonte = _ler(SETTINGS)
    for chave in ("updateCheck:", "updateChecking:", "updateError:"):
        assert chave in fonte, "falta %s no estado da tela" % chave


def test_o_resultado_aparece_nos_dois_casos():
    corpo = _bloco(_ler(SETTINGS), "updatesSection")
    assert "settings.updates.uptodate" in corpo, "sem isto, procurar e não achar não diz nada"
    assert "settings.updates.found" in corpo
    assert "state.updateError" in corpo, "erro de rede que some é pior que erro de rede"


# --------------------------------------------------------------------------- as frases


CHAVES = (
    "settings.updates.check",
    "settings.updates.checking",
    "settings.updates.uptodate",
    "settings.updates.found",
    "settings.updates.install",
    "settings.updates.open",
)


@pytest.mark.parametrize("chave", CHAVES)
def test_a_frase_existe_nas_duas_linguas(chave):
    assert "'%s':" % chave in _ler(SETTINGS), "falta o inglês de %s" % chave
    assert "'%s':" % chave in _ler(PT), "falta o português de %s" % chave


def test_o_portugues_nao_ficou_em_ingles():
    pt = _ler(PT)
    linha = [l for l in pt.splitlines() if "'settings.updates.check':" in l][0]
    assert "Procurar" in linha, linha


# --------------------------------------------------------------------------- a ponta do servidor


def test_procurar_responde_sem_precisar_do_modo_avancado(auth_client, monkeypatch):
    """O botão novo chama esta rota; ela não sabe nem quer saber de modo de tela."""
    from project_os.core import updates as live

    monkeypatch.setattr(
        live, "check",
        lambda *a, **k: {"method": "tarball", "current": "0.4.6", "latest": "0.4.14",
                         "update_available": True, "can_install": True,
                         "install_blocked": "", "notes": "coisas novas"},
    )
    corpo = auth_client.post("/api/updates/check").json()
    assert corpo["update_available"] is True
    assert corpo["latest"] == "0.4.14"
