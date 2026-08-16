"""Frase que pede um valor e é chamada sem ele aparece com as chaves na tela.

    'settings.updates.install': 'Instalar a {version}'
    t('settings.updates.install')      ->   "Instalar a {version}"

Foi assim que o botão de instalar apareceu no navegador, com as chaves à mostra.
Nenhum teste de rota pega isso -- a tela renderiza, responde, não dá erro no
console; ela só mostra uma frase que ninguém escreveria. Quem pega é o olho, e o
olho não passa em todas as telas toda vez.

Este teste passa: junta todas as frases declaradas (as de cada tela, as do
português, as dos painéis de app), separa as que têm ``{buraco}``, e procura
chamadas ``t('chave')`` sem o segundo argumento. Chave montada em tempo de
execução -- ``t('apps.state.' + estado)`` -- fica de fora, porque não dá para
saber a chave sem rodar.
"""

from __future__ import annotations

import io
import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(RAIZ, "web")
APPS = os.path.join(RAIZ, "project_os", "apps")

#: ``'alguma.chave': 'o texto',`` -- como as frases são declaradas em todo lugar.
DECLARACAO = re.compile(
    r"^\s*'([a-zA-Z][\w.\-]*)'\s*:\s*(['\"])(.*?)\2\s*,?\s*$", re.M
)
#: ``{version}``, ``{count}`` -- e não ``{}`` nem ``{ algo }`` de JavaScript.
BURACO = re.compile(r"\{([a-zA-Z_]\w*)\}")


def _arquivos_js():
    achados = []
    for base in (WEB, APPS):
        for pasta, _dirs, nomes in os.walk(base):
            for nome in nomes:
                if nome.endswith(".js"):
                    achados.append(os.path.join(pasta, nome))
    return sorted(achados)


def _ler(caminho):
    return io.open(caminho, encoding="utf-8").read()


def _frases_com_buraco():
    """Toda chave cuja frase pede um valor, em qualquer língua."""
    com_buraco = {}
    for caminho in _arquivos_js():
        for chave, _aspa, texto in DECLARACAO.findall(_ler(caminho)):
            if BURACO.search(texto):
                com_buraco.setdefault(chave, set()).update(BURACO.findall(texto))
    return com_buraco


def _argumentos(fonte, abre):
    """O texto entre os parênteses de uma chamada, respeitando aninhamento e aspas."""
    profundidade = 0
    i = abre
    aspa = ""
    while i < len(fonte):
        c = fonte[i]
        if aspa:
            if c == "\\":
                i += 2
                continue
            if c == aspa:
                aspa = ""
        elif c in "'\"`":
            aspa = c
        elif c in "([{":
            profundidade += 1
        elif c in ")]}":
            profundidade -= 1
            if profundidade == 0:
                return fonte[abre + 1:i]
        i += 1
    return ""


def _tem_virgula_no_topo(texto):
    """Se a chamada passa um segundo argumento -- vírgula fora de aninhamento."""
    profundidade = 0
    aspa = ""
    i = 0
    while i < len(texto):
        c = texto[i]
        if aspa:
            if c == "\\":
                i += 2
                continue
            if c == aspa:
                aspa = ""
        elif c in "'\"`":
            aspa = c
        elif c in "([{":
            profundidade += 1
        elif c in ")]}":
            profundidade -= 1
        elif c == "," and profundidade == 0:
            return True
        i += 1
    return False


def _chamadas_sem_valores(caminho):
    """Toda chamada a ``t(...)`` de um argumento só, e as chaves citadas dentro.

    Não é só ``t('chave')``: a forma que escapou na tela de Configurações era
    ``t(temNova ? 'a' : 'b')``, com o ternário dentro da chamada. Por isso aqui
    a chamada é lida inteira -- parênteses equilibrados, aspas respeitadas -- e
    todas as chaves citadas contam.
    """
    fonte = _ler(caminho)
    chaves = set()
    for achado in re.finditer(r"(?<![\w$.])t\(", fonte):
        argumentos = _argumentos(fonte, achado.end() - 1)
        if _tem_virgula_no_topo(argumentos):
            continue
        chaves.update(re.findall(r"['\"]([\w][\w.\-]*\.[\w.\-]+)['\"]", argumentos))
    return chaves


def test_nenhuma_frase_com_buraco_e_chamada_sem_valor():
    com_buraco = _frases_com_buraco()
    assert com_buraco, "não achei frase nenhuma com {buraco} -- o teste parou de ler os arquivos"

    faltando = []
    for caminho in _arquivos_js():
        for chave in _chamadas_sem_valores(caminho):
            if chave in com_buraco:
                faltando.append("%s: t('%s') -- a frase pede %s" % (
                    os.path.relpath(caminho, RAIZ), chave,
                    ", ".join("{%s}" % b for b in sorted(com_buraco[chave])),
                ))

    assert not faltando, (
        "frase que pede valor chamada sem ele -- as chaves aparecem na tela:\n  "
        + "\n  ".join(sorted(faltando))
    )


def test_o_portugues_e_o_ingles_pedem_os_mesmos_valores():
    """Uma língua com {version} e a outra sem é a mesma tela quebrada em metade dos casos."""
    pt = {}
    for chave, _aspa, texto in DECLARACAO.findall(
        _ler(os.path.join(WEB, "lib", "strings-pt.js"))
    ):
        pt[chave] = set(BURACO.findall(texto))

    ingles = {}
    for caminho in _arquivos_js():
        if caminho.endswith("strings-pt.js"):
            continue
        for chave, _aspa, texto in DECLARACAO.findall(_ler(caminho)):
            ingles.setdefault(chave, set()).update(BURACO.findall(texto))

    diferentes = [
        "%s: pt pede %s, en pede %s" % (
            chave, sorted(pt[chave]) or "nada", sorted(ingles[chave]) or "nada")
        for chave in sorted(set(pt) & set(ingles))
        if pt[chave] != ingles[chave]
    ]
    assert not diferentes, "\n  ".join([""] + diferentes)
