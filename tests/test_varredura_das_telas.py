"""O que a varredura de tela por tela encontrou, e não pode voltar.

*"quero q vc faça um novo bughunt, testando literalmente tudo do app, clicando em
tudo"* -- feito no navegador, nas quinze telas, nos dois modos. O que apareceu:

* a tela de Serviços mostrava o estado do app cru, do jeito que o servidor manda
  (``disabled``), numa interface em português -- enquanto a tela de Aplicativos,
  com o mesmo dado, mostra "desligado";
* o cartão de aplicativo repetia a descrição duas vezes, uma cortada no subtítulo
  e outra inteira no corpo, sempre que o app estava parado -- que é o estado
  normal da maioria deles.

O terceiro achado, o pior, tem teste próprio em ``test_web_router.py``: abrir o
endereço da caixa sem hash deixava a tela de carregamento para sempre.
"""

from __future__ import annotations

import io
import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(RAIZ, "web", "views", "services.js")
APPS = os.path.join(RAIZ, "web", "views", "apps.js")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")


def _ler(caminho):
    return io.open(caminho, encoding="utf-8").read()


def _funcao(fonte, nome):
    achado = re.search(r"\nfunction %s\(([^)]*)\) \{\n(.*?)\n\}\n" % nome, fonte, re.S)
    assert achado, "não achei a função %s" % nome
    return achado.group(2)


# --------------------------------------------------------------------------- serviços


def test_a_tela_de_servicos_traduz_o_estado_do_app():
    corpo = _funcao(_ler(SERVICES), "appBadge")
    assert "t('apps.state.'" in corpo, (
        "a etiqueta saía crua do servidor: numa tela em português dizia 'disabled'"
    )


def test_e_declara_as_palavras_que_usa():
    """As telas carregam sob demanda; depender de outra ter sido aberta é sorte."""
    fonte = _ler(SERVICES)
    for estado in ("running", "stopped", "error", "disabled"):
        assert "'apps.state.%s':" % estado in fonte, "falta declarar apps.state.%s" % estado


def test_o_portugues_dessas_palavras_existe():
    pt = _ler(PT)
    for estado, palavra in (("running", "rodando"), ("disabled", "desligado")):
        linha = [l for l in pt.splitlines() if "'apps.state.%s':" % estado in l]
        assert linha and palavra in linha[0], "apps.state.%s não está em português" % estado


def test_uma_etiqueta_desconhecida_nao_vira_chave_na_tela():
    """t() devolve a própria chave quando não conhece a frase, e chave na tela é pior."""
    corpo = _funcao(_ler(SERVICES), "appBadge")
    assert "=== 'apps.state.' + state" in corpo or "rotulo === " in corpo, (
        "sem esta volta, um estado novo do servidor apareceria como apps.state.seja-o-que-for"
    )


# --------------------------------------------------------------------------- cartão de app


def test_o_cartao_nao_repete_a_descricao():
    """Parado é o estado normal da maioria dos apps -- e era nele que repetia."""
    fonte = _ler(APPS)
    achado = re.search(r"const sub = status\.summary \|\| app\.description \|\| null;", fonte)
    assert achado, "o subtítulo do cartão mudou de forma -- teste desatualizado"
    assert "app.description !== sub" in fonte, (
        "o corpo mostra a descrição de novo quando ela já é o subtítulo"
    )


# --------------------------------------------------------------------------- resumo do app


def test_o_resumo_do_birdtunes_esta_em_portugues():
    """O que o Painel e o cartão de Aplicativos mostram vem pronto do servidor.

    Não passa por t(): é texto que o app escreve. O BirdTunes escrevia em inglês
    ("No speaker chosen yet. Pick one in the app to start playing.") e aparecia
    assim na primeira tela, ao lado de tudo o mais em português. O bot de
    WhatsApp, do mesmo repositório, já escrevia em português -- era o BirdTunes
    fora da regra, e não a regra que faltava.
    """
    from project_os.apps.birdtunes.app import BirdTunesApp

    resumo = BirdTunesApp._status_summary

    def falar(snapshot, quiet=False):
        return resumo(None, snapshot, quiet)

    assert "Nenhuma caixa de som" in falar({"output": "null"})
    assert "silêncio" in falar({}, quiet=True)
    assert falar({"state": "playing", "track": {"title": "Sabiá"}}) == "Tocando Sabiá"
    assert "esperando" in falar({"output": "airplay:1"})

    for texto in (falar({"output": "null"}), falar({}, True), falar({"output": "x"})):
        for palavra in ("speaker", "Idle", "Waiting", "Quiet hours", "chosen"):
            assert palavra not in texto, "sobrou inglês no resumo: %r" % texto


def test_os_campos_do_cartao_tambem():
    from project_os.apps.birdtunes.app import BirdTunesApp

    campos = BirdTunesApp._status_fields(None, {
        "queue_len": 2,
        # A forma de verdade: `next_change` é o dicionário que o scheduler
        # devolve, não um número de segundos.
        "next_change": {"event": "starts", "at": "2026-08-18T17:00:00",
                        "window_id": "tarde", "message": "Toca às 17:00"},
    })
    rotulos = [c["label"] for c in campos]
    assert "Caixa de som" in rotulos and "Tocando agora" in rotulos
    for proibido in ("Speaker", "Now playing", "Next change"):
        assert proibido not in rotulos, "campo em inglês no cartão: %s" % proibido
    proximo = [c for c in campos if c["label"] == "Próxima mudança"]
    assert proximo and proximo[0]["value"] == "Toca às 17:00", (
        "o cartão mandava o dicionário inteiro e a tela mostrava um travessão"
    )
