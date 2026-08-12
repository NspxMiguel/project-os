"""Toda tela em português, sem uma frase em inglês no meio.

Cada tela declara os textos dela em inglês, no próprio arquivo, e
``web/lib/strings-pt.js`` é a tradução que o app ativa. O jeito que isso quebra
não é com erro: a chave sem tradução volta o inglês e a tela fica bilíngue --
"Esta caixa não troca o app por conta própria" ao lado de "Install 0.4.7".

Descobri escrevendo o aviso de atualização bloqueada: pus a chave nova só na
tela, e ele veria essa frase em inglês. As 608 chaves anteriores estavam todas
traduzidas, então o furo não era descuido acumulado -- era o próximo descuido.

As chaves montadas na hora (``t('apps.state.' + estado)``) não dão para conferir
assim; ficam de fora, e é por isso que a lista de exceções aqui é explícita em
vez de um "ignore o que não achar".
"""

from __future__ import annotations

import os
import re

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
PT = os.path.join(WEB, "lib", "strings-pt.js")

#: Chaves cujo final vem de uma variável no código. O prefixo termina com ponto
#: justamente porque o resto é concatenado.
USADA_COM_CONCATENACAO = re.compile(r"\.$")

#: Só aparece num exemplo de docstring, não numa tela.
FALSOS = {"greet"}


def chaves_usadas():
    usadas = {}
    for pasta, _, arquivos in os.walk(WEB):
        for nome in arquivos:
            if not nome.endswith(".js") or nome == "strings-pt.js":
                continue
            caminho = os.path.join(pasta, nome)
            with open(caminho, "r", encoding="utf-8") as arquivo:
                texto = arquivo.read()
            for achado in re.finditer(r"""\bt\(\s*['"]([a-zA-Z0-9_.\-]+)['"]""", texto):
                chave = achado.group(1)
                if USADA_COM_CONCATENACAO.search(chave) or chave in FALSOS:
                    continue
                usadas.setdefault(chave, set()).add(os.path.relpath(caminho, WEB))
    return usadas


def chaves_traduzidas():
    with open(PT, "r", encoding="utf-8") as arquivo:
        texto = arquivo.read()
    return set(re.findall(r"""^\s*['"]([a-zA-Z0-9_.\-]+)['"]\s*:""", texto, re.M))


def test_nenhuma_tela_tem_texto_sem_traducao():
    usadas = chaves_usadas()
    traduzidas = chaves_traduzidas()
    faltando = sorted(chave for chave in usadas if chave not in traduzidas)
    detalhe = ["%s (%s)" % (chave, ", ".join(sorted(usadas[chave]))) for chave in faltando]
    assert not faltando, "sem tradução em português: %s" % "; ".join(detalhe)


def test_o_dicionario_tem_o_que_conferir():
    """Se o jeito de declarar textos mudar, este arquivo tem que saber.

    Sem isto, uma mudança de sintaxe faria as duas listas ficarem vazias e o
    teste acima passaria dizendo que está tudo traduzido.
    """
    assert len(chaves_usadas()) > 400
    assert len(chaves_traduzidas()) > 400
