"""Nenhuma tela pode empurrar a página para o lado num celular.

Medido com Chrome sem cabeça, viewport de 375px, nas 22 telas do sistema
(início, apps, aparelhos, configurações, atualizações, loja, BirdTunes e as
telas de dentro: as cinco abas do app, detalhe de app e detalhe de aparelho).

Antes destas duas regras:

===================  =========================================
tela                 largura da página / largura da tela
===================  =========================================
Loja                 826 / 375
BirdTunes            604 / 375
Apps                 402 / 375
===================  =========================================

Depois: 375 / 375 nas 22.

A causa nos três casos é a mesma: **o mínimo automático de um item de flex ou
grade é o conteúdo dele, não zero**. Uma tira de abas com seis botões que não
quebram linha mede uns 520px de conteúdo, e esse número sobe pela árvore até
virar a largura da página. O conserto não é apertar a tira -- é dizer que ela
rola dentro de si mesma (``overflow-x: auto``, que zera o mínimo automático) e,
onde o pai também é item de flex, repetir ``min-width: 0``.
"""

from __future__ import annotations

import io
import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLOBAL = os.path.join(RAIZ, "web", "style.css")
PAINEL = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.css")


def _regra(caminho, seletor):
    """O corpo de uma regra CSS, ou "" se o seletor não existir."""
    fonte = io.open(caminho, encoding="utf-8").read()
    achado = re.search(re.escape(seletor) + r"\s*\{([^}]*)\}", fonte)
    return achado.group(1) if achado else ""


def test_a_tira_de_abas_rola_em_vez_de_esticar_a_pagina():
    """Vale para toda tira, não só para a de seis abas das Configurações.

    A tira de Apps levava a página a 402px e a da Loja ajudava a levá-la a
    826px -- as duas usavam ``.segmented`` sem modificador nenhum.
    """
    corpo = _regra(GLOBAL, ".segmented")
    assert "overflow-x: auto" in corpo
    assert "max-width: 100%" in corpo


def test_e_os_botoes_dela_nao_encolhem_ate_ficarem_ilegiveis():
    fonte = io.open(GLOBAL, encoding="utf-8").read()
    assert ".segmented > * { flex: none; }" in fonte


def test_o_painel_do_birdtunes_pode_encolher():
    """Sem o mínimo zero, o painel fica do tamanho do seu conteúdo mais largo."""
    corpo = _regra(PAINEL, ".bt")
    assert "min-width: 0" in corpo
    assert "max-width: 100%" in corpo


def test_e_a_navegacao_dele_tambem():
    corpo = _regra(PAINEL, ".bt-nav")
    assert "overflow-x: auto" in corpo
    assert "min-width: 0" in corpo


def test_o_modificador_antigo_continua_valendo():
    """Telas já escritas pedem `.segmented--scroll` pelo nome."""
    assert _regra(GLOBAL, ".segmented--scroll") != ""


# --------------------------------------------------------------------------
# Caber na largura não é o mesmo que ficar legível nela
# --------------------------------------------------------------------------


def test_o_aviso_da_agenda_nao_vira_uma_tirinha():
    """Medido em 375px: mensagem com 141px e botão com 152 -- o botão mais largo
    que o aviso, e a frase que explica por que nada vai tocar quebrada em dez
    caracteres por linha.

    A página cabia em 375/375 o tempo todo, então a regra de transbordo passava
    e ninguém via o problema. Cabe e é ilegível são coisas diferentes.

    Depois: 301px para o texto, e o botão desce para a linha de baixo. Em 768 e
    em 1280 nada muda -- os dois continuam lado a lado.
    """
    aviso = _regra(PAINEL, ".bt-warn")
    assert "flex-wrap: wrap" in aviso, "sem quebra, o botão nunca desce"
    texto = _regra(PAINEL, ".bt-warn > .grow")
    assert "flex: 1 1 15rem" in texto, "a base é o que decide quando o botão desce"
    assert "min-width: 0" in texto


def test_e_a_decisao_e_do_espaco_e_nao_da_largura_da_tela():
    """Sem media query de propósito: o texto traduzido e a fonte do sistema
    mudam a conta, e um número de breakpoint não sabe disso.
    """
    fonte = io.open(PAINEL, encoding="utf-8").read()
    trecho = fonte[fonte.index(".bt-warn {"):]
    trecho = trecho[:trecho.index(".bt-cmd")]
    assert "@media" not in trecho


def test_o_comando_do_aviso_usa_a_superficie_de_codigo_do_tema():
    """Um comando para copiar tem que parecer terminal; inventar uma segunda
    aparência para isso só criaria divergência com o resto do sistema.
    """
    painel = io.open(os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.js"),
                     encoding="utf-8").read()
    assert "class: 'code bt-cmd'" in painel
    assert ".code, .log {" in io.open(GLOBAL, encoding="utf-8").read()
    cmd = _regra(PAINEL, ".bt-cmd")
    assert "user-select: all" in cmd, "clique de três dedos pega o comando inteiro"
