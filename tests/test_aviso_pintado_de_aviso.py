"""Nove avisos do sistema estavam pintados de nada.

O tema define ``.notice--warn`` e ``.notice--error``. O código escrevia
``.notice--warning`` -- uma classe que não existe em lugar nenhum. CSS não
reclama de classe desconhecida: ela simplesmente não pinta, e o aviso sai com a
borda cinza de uma caixa qualquer.

Os nove: gerenciamento de pacotes desligado, atualizações desligadas, falha ao
conferir atualização, senha do SSH não configurada, horário de silêncio, faixas
incompatíveis, sem yt-dlp (duas telas) e o baixador velho.

Nenhum teste podia pegar isso porque nenhum teste comparava as classes que o
código usa com as que o tema define. Este compara.
"""

from __future__ import annotations

import io
import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMA = os.path.join(RAIZ, "web", "style.css")


def _ler(caminho):
    return io.open(caminho, encoding="utf-8").read()


def _arquivos_de_tela():
    for base in (os.path.join(RAIZ, "web"), os.path.join(RAIZ, "project_os", "apps")):
        for pasta, _, nomes in os.walk(base):
            for nome in nomes:
                if nome.endswith(".js"):
                    yield os.path.join(pasta, nome)


def test_todo_modificador_de_aviso_existe_no_tema():
    tema = _ler(TEMA)
    definidos = set(re.findall(r"\.notice--([a-z0-9-]+)", tema))
    usados = {}
    for caminho in _arquivos_de_tela():
        for achado in re.findall(r"notice--([a-z0-9-]+)", _ler(caminho)):
            usados.setdefault(achado, os.path.relpath(caminho, RAIZ))
    faltando = sorted("%s (em %s)" % (m, onde) for m, onde in usados.items()
                      if m not in definidos)
    assert not faltando, (
        "classe de aviso que o tema não define: %s.\nCSS não reclama de classe "
        "desconhecida -- ela só não pinta." % ", ".join(faltando))


def test_o_tema_define_os_dois_que_o_sistema_usa():
    tema = _ler(TEMA)
    for classe in (".notice--warn", ".notice--error"):
        assert classe in tema, classe


def test_o_aviso_do_baixador_velho_e_amarelo():
    """O caso que fez a coisa aparecer: "o baixador tem 10 meses" saía com a
    mesma borda cinza de um recado qualquer."""
    painel = _ler(os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.js"))
    corpo = painel[painel.index("function baixadorVelho()"):]
    corpo = corpo[:corpo.index("function addView()")]
    assert "notice--warn'" in corpo
