"""Uma rotina que chegou na hora e não tocou tem que aparecer na primeira tela.

*"dnv eu configurei uma rotina pra tocar as 12:30, n tocou nao...."*

O app já anotava o motivo desde a 0.4.17 (``last_attempt`` no ``/schedule``) e
já desenhava o aviso -- só que dentro da aba **Agenda**, e a tela inicial nem
carregava a agenda. Ou seja: o aviso só chegava a quem já desconfiava dela.
Quem marca 12:30, não ouve nada e abre o app cai na tela inicial e não vê
nenhuma palavra sobre o assunto.

Medido numa caixa de verdade, com a janela apontando para uma playlist vazia:
a janela abriu às 14:04 e o ``/schedule`` guardou
``{"ok": false, "code": "no_tracks", "message": "Essa playlist ainda não tem
faixa que dê para tocar."}``. Este teste amarra o caminho até os olhos dele.
"""

from __future__ import annotations

import io
import os

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAINEL = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.js")


def _painel():
    return io.open(PAINEL, encoding="utf-8").read()


def _corpo(nome):
    """O corpo de uma função do painel, do cabeçalho até a próxima função."""
    fonte = _painel()
    inicio = fonte.index("function %s(" % nome)
    resto = fonte[inicio + 1:]
    fim = resto.find("\n    function ")
    return resto[:fim if fim != -1 else len(resto)]


def test_a_tela_inicial_carrega_a_agenda():
    """Sem isto o aviso não teria dado nenhum para desenhar."""
    assert "state.schedule = await safeGet('/schedule');" in _corpo("loadHome")


def test_e_desenha_o_aviso_do_ultimo_horario():
    assert "scheduleWarnings()" in _corpo("homeView")


def test_a_aba_agenda_continua_mostrando():
    """Quem está mexendo na agenda vê o mesmo aviso onde está mexendo."""
    assert "scheduleWarnings()" in _corpo("scheduleView")


def test_e_o_aviso_some_sozinho_quando_um_horario_toca():
    """Nada de aviso preso: some quando a última tentativa deu certo.

    Um aviso que precisa ser dispensado à mão é um aviso que fica lá para
    sempre -- e aí ninguém mais lê.
    """
    corpo = _corpo("scheduleWarnings")
    assert "last && !last.ok" in corpo
    assert "dismiss" not in corpo.lower()


def test_o_texto_do_aviso_existe_em_portugues():
    pt = io.open(os.path.join(RAIZ, "web", "lib", "strings-pt.js"), encoding="utf-8").read()
    assert "'bt.schedule.last_failed':" in pt
