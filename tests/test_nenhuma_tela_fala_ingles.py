"""Chave de texto sem tradução some em inglês no meio de uma tela em português.

No painel dele, embaixo de "Aparelhos", estava escrito **"30 found on your
network"** -- porque aquela linha montava a frase na mão em vez de pedir por
chave. Não é erro que quebra nada, e é por isso que durou: nada falha, nenhum
teste reclama, e o texto só aparece para quem abre a tela.

O mesmo buraco engole o caso pior, que é a chave existir no código e faltar na
tradução: aí a tela mostra a chave crua (`dash.clock.wrong`) ou o texto em
inglês, dependendo de como o `t()` resolve.

Este arquivo compara as duas listas: toda chave que as telas pedem tem que ter
tradução, e toda frase visível tem que vir de uma chave.
"""

from __future__ import annotations

import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWS = os.path.join(RAIZ, "web", "views")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")

#: `t('chave')` e `t('chave', {...})`, que é como as telas pedem texto.
#:
#: Exige que a aspas feche em `,` ou `)`: sem isso o padrão também casaria com
#: `t('apps.state.' + estado)`, onde o literal é só o começo da chave e a
#: tradução que existe é a da chave inteira, montada em tempo de execução.
PEDIDO = re.compile(r"\bt\(\s*'([a-z][a-zA-Z0-9_.]*[a-zA-Z0-9])'\s*[,)]")
#: `'chave': '...'` no arquivo de tradução.
TRADUZIDA = re.compile(r"^\s*'([a-zA-Z0-9_.]+)'\s*:", re.M)


def _traduzidas():
    return set(TRADUZIDA.findall(open(PT, encoding="utf-8").read()))


def _telas():
    for nome in sorted(os.listdir(VIEWS)):
        if nome.endswith(".js"):
            yield nome, open(os.path.join(VIEWS, nome), encoding="utf-8").read()


def test_ha_telas_para_conferir():
    assert len(list(_telas())) >= 5


def test_o_padrao_acha_chave_e_ignora_prefixo_montado():
    """Se o padrão parar de achar chave, o teste de baixo passa sem conferir nada."""
    assert PEDIDO.findall("t('dash.card.clock')") == ["dash.card.clock"]
    assert PEDIDO.findall("t('dash.up', {time: x})") == ["dash.up"]
    assert PEDIDO.findall("t('apps.state.' + app.state)") == []


def test_toda_chave_pedida_pelas_telas_tem_traducao():
    tem = _traduzidas()
    faltando = []
    for nome, fonte in _telas():
        for chave in set(PEDIDO.findall(fonte)):
            # A tela pode trazer o próprio inglês como reserva no topo do
            # arquivo; o que não pode é faltar o português.
            if chave not in tem:
                faltando.append("%s pede %s" % (nome, chave))
    assert not faltando, "sem tradução:\n" + "\n".join(sorted(faltando))


def test_o_painel_nao_monta_frase_em_ingles_na_mao():
    """O caso que apareceu na tela dele."""
    fonte = open(os.path.join(VIEWS, "dashboard.js"), encoding="utf-8").read()
    assert "found on your network'" not in fonte.split("'dash.devices.found'")[-1], (
        "a frase voltou a ser montada na mão"
    )
    assert "'dash.devices.found'" in fonte


def test_as_chaves_do_relogio_estao_traduzidas():
    tem = _traduzidas()
    for chave in ("dash.card.clock", "dash.clock.wrong", "dash.clock.running",
                  "dash.clock.vsYou", "dash.clock.noZone", "dash.devices.found"):
        assert chave in tem, "falta %s em strings-pt.js" % chave
