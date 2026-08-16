"""A tela do BirdTunes: navegação, playlist com tela própria, e o que quebrava.

*"bird tunes bem confuso, muito ruim interface, playlist e etc"* -- a tela
anterior era uma coluna de sete cartões abertos ao mesmo tempo (tocar,
compatibilidade, acervo, playlists, importar, agenda, saída). Tudo pesava igual,
nada tinha dono, e achar uma faixa era rolar a página inteira.

Este arquivo cobre a reforma pelos pontos que dá para conferir sem navegador --
e os três defeitos que só apareceram abrindo a tela de verdade:

* ``appApi.delete`` **não existe**. O helper expõe ``del``. Ou seja, o botão de
  apagar playlist da tela antiga lançava ``TypeError`` a cada clique e virava um
  "Ação falhou" genérico. Nunca funcionou.
* ``replaceChildren(null)`` escreve o *texto* ``"null"`` na tela -- ao contrário
  do ``h()``, que ignora. O painel de saída mostrava "nullnull" embaixo do
  seletor.
* ``display: flex`` ganha de ``[hidden]``, então o painel de saída fechado
  aparecia como uma faixa vazia no meio da página.

E o defeito de dados que a tela nova encontrou ao ser povoada: duas rádios pela
URL só entravam uma vez, caladas (ver ``test_duas_radios_entram_as_duas``).
"""

from __future__ import annotations

import io
import json
import os

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(RAIZ, "project_os", "apps", "birdtunes")


def _painel():
    return io.open(os.path.join(APP, "web", "panel.js"), encoding="utf-8").read()


def _estilo():
    return io.open(os.path.join(APP, "web", "panel.css"), encoding="utf-8").read()


# --------------------------------------------------------------------------- estrutura


def test_a_tela_tem_navegacao_em_vez_de_sete_cartoes():
    fonte = _painel()
    for aba in ("'home'", "'library'", "'playlists'", "'add'", "'schedule'"):
        assert aba in fonte, "falta a aba %s" % aba
    assert "const TABS = [" in fonte
    # Uma seção por vez: o render escolhe uma das cinco, não empilha todas.
    assert "state.view === 'library' ? libraryView()" in fonte


def test_a_barra_do_que_esta_tocando_fica_em_todas_as_secoes():
    fonte = _painel()
    assert "function renderPlayer()" in fonte
    # Montada uma vez, fora do conteúdo que troca de seção.
    assert "const playerBar = h('div', {class: 'bt-player'})" in fonte
    assert ".bt-player {" in _estilo()
    assert "position: sticky" in _estilo()


def test_playlist_abre_em_tela_propria():
    fonte = _painel()
    assert "function playlistDetail(" in fonte
    assert "/playlists/' + pl.id + '/reorder'" in fonte, "reordenar dentro da playlist"
    assert "bt.playlists.back" in fonte, "e um caminho de volta"


def test_a_busca_e_a_ordenacao_sao_as_do_servidor():
    """Filtrar no navegador o que o SQLite já filtra não sobrevive a um acervo grande."""
    fonte = _painel()
    assert "safeGet('/library', {search: state.search, sort: state.sort})" in fonte
    assert "setTimeout(async () => {" in fonte, "uma consulta por pausa de digitação"


def test_a_busca_nao_redesenha_a_secao_inteira():
    """Refazer a seção a cada tecla tirava o foco do campo no meio da palavra."""
    fonte = _painel()
    assert "function renderList()" in fonte
    assert "listHost.replaceChildren(" in fonte


def test_a_saida_de_som_mora_na_barra():
    fonte = _painel()
    assert "bt-chip" in fonte and "function renderSheet()" in fonte
    assert "state.outputOpen" in fonte


# --------------------------------------------------------------------------- os defeitos


def test_a_tela_usa_o_del_que_existe_no_helper():
    """appApi.delete não existe; o helper expõe del (web/lib/api.js)."""
    api = io.open(os.path.join(RAIZ, "web", "lib", "api.js"), encoding="utf-8").read()
    assert "del: (path, body, options)" in api
    assert "delete:" not in api.split("export function apiFor")[1][:600]

    fonte = _painel()
    assert "appApi.delete(" not in fonte, "esse método não existe: o clique vira TypeError"
    assert "appApi.del(" in fonte


def test_nenhum_replace_children_recebe_null_solto():
    """replaceChildren(null) escreve "null" na tela; h() ignora. Não é a mesma coisa."""
    fonte = _painel()
    import re

    for trecho in re.findall(r"replaceChildren\((.*?)\);", fonte, re.S):
        if "filter(Boolean)" in trecho:
            continue
        assert " null," not in trecho and not trecho.strip().endswith("null"), (
            "replaceChildren com null solto: %s" % trecho[:80]
        )


def test_o_painel_de_saida_fechado_some_de_verdade():
    """[hidden] não vence display:flex -- sem esta regra sobra uma faixa vazia."""
    css = _estilo()
    assert ".bt-sheet[hidden] { display: none; }" in css


def test_o_inicio_e_a_barra_contam_a_mesma_faixa():
    """Atualizar só a barra deixava as duas anunciando músicas diferentes."""
    fonte = _painel()
    corte = fonte.index("app.birdtunes.state")
    trecho = fonte[corte:corte + 500]
    assert "state.view === 'home'" in trecho and "render()" in trecho


def test_todo_icone_pedido_existe_no_conjunto():
    """Ícone inexistente vira três pontinhos e um aviso no console."""
    import re

    icones = io.open(os.path.join(RAIZ, "web", "lib", "icons.js"), encoding="utf-8").read()
    disponiveis = set(re.findall(r"^  '?([a-z0-9-]+)'?:", icones, re.M))
    fonte = _painel()
    pedidos = set(re.findall(r"icon\('([a-z0-9-]+)'", fonte))
    pedidos |= set(re.findall(r"iconBtn\('([a-z0-9-]+)'", fonte))
    pedidos |= set(re.findall(r"icon: '([a-z0-9-]+)'", fonte))
    faltando = sorted(pedidos - disponiveis)
    assert not faltando, "ícone que não existe: %s" % faltando


def test_a_folha_de_estilo_e_servida_pelo_app():
    manifesto = json.load(io.open(os.path.join(APP, "manifest.json"), encoding="utf-8"))
    assert manifesto["ui"]["styles"] == "panel.css"
    assert os.path.isfile(os.path.join(APP, "web", "panel.css"))


def test_o_css_do_app_chega_pelo_http(auth_client):
    resposta = auth_client.get("/api/apps/birdtunes/ui/panel.css")
    assert resposta.status_code == 200
    assert "text/css" in resposta.headers.get("content-type", "")
    assert ".bt-player" in resposta.text


def test_a_tela_fala_portugues():
    pt = io.open(os.path.join(RAIZ, "web", "lib", "strings-pt.js"), encoding="utf-8").read()
    for chave in ("bt.nav.home", "bt.nav.library", "bt.nav.playlists", "bt.nav.add",
                  "bt.nav.schedule", "bt.queue", "bt.library.search", "bt.playlists.rename",
                  "bt.output.silent", "bt.stats.plays"):
        assert "'%s'" % chave in pt, "falta o português de %s" % chave


# --------------------------------------------------------------------------- os dados


def test_duas_radios_entram_as_duas(auth_client):
    """A coluna path é UNIQUE e as rádios entravam todas com ''.

    A primeira ocupava o valor e as seguintes batiam no ON CONFLICT DO NOTHING:
    resposta 200, nada inserido, nenhuma palavra. Quem colava o segundo link via
    a tela não mudar e não tinha como saber por quê.
    """
    base = "/api/apps/birdtunes"
    for i in range(1, 4):
        resposta = auth_client.post(
            base + "/library/add-url",
            json={"url": "https://exemplo.org/radio/%d" % i, "title": "Rádio %d" % i},
        )
        assert resposta.status_code == 200, resposta.text

    faixas = auth_client.get(base + "/library").json()["tracks"]
    titulos = sorted(f["title"] for f in faixas)
    assert titulos == ["Rádio 1", "Rádio 2", "Rádio 3"], titulos


def test_a_mesma_radio_duas_vezes_continua_sendo_uma(auth_client):
    base = "/api/apps/birdtunes"
    for _ in range(2):
        auth_client.post(base + "/library/add-url",
                         json={"url": "https://exemplo.org/radio/igual", "title": "Igual"})
    faixas = auth_client.get(base + "/library").json()["tracks"]
    assert len([f for f in faixas if f["title"] == "Igual"]) == 1


def test_uma_radio_continua_sem_arquivo_mas_com_texto(auth_client):
    """Quem lê espera string; a coluna agora guarda NULL e a diferença morre no shape."""
    base = "/api/apps/birdtunes"
    auth_client.post(base + "/library/add-url",
                     json={"url": "https://exemplo.org/radio/x", "title": "X"})
    faixa = auth_client.get(base + "/library").json()["tracks"][0]
    assert faixa["path"] == "", "None atravessando a API viraria erro no player"
    assert faixa["source"] == "url"
    assert faixa["source_url"] == "https://exemplo.org/radio/x"


def test_as_radios_aparecem_na_playlist_tambem(auth_client):
    base = "/api/apps/birdtunes"
    ids = []
    for i in range(1, 3):
        corpo = auth_client.post(
            base + "/library/add-url",
            json={"url": "https://exemplo.org/lista/%d" % i, "title": "Faixa %d" % i},
        ).json()
        ids.append(corpo["track"]["id"])
    playlist = auth_client.post(base + "/playlists", json={"name": "Manhã"}).json()["playlist"]
    auth_client.post(base + "/playlists/%s/tracks" % playlist["id"], json={"track_ids": ids})

    detalhe = auth_client.get(base + "/playlists/%s" % playlist["id"]).json()["playlist"]
    assert [f["title"] for f in detalhe["tracks"]] == ["Faixa 1", "Faixa 2"]
