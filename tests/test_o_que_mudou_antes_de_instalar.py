""""O que mudou" mostrava um link, e link nenhum responde essa pergunta.

*"coloca log/oq mudou la nas config, pro user escolher se vai atualizar ou nai"*

O campo já existia na tela; o que chegava nele é que era um endereço. O
``release.yml`` gravava ``notes: "https://github.com/.../releases/tag/vX"`` no
manifesto, e a tela imprimia isso embaixo do rótulo "O que mudou". Quem estava
decidindo se instalava agora via uma URL -- e para ler o que muda tinha que sair
da caixa, abrir o GitHub e voltar.

Agora os dois campos são coisas separadas: ``notes`` é texto (a mensagem do
commit da tag, que é o changelog que já se escreve a cada versão) e
``notes_url`` é o endereço, para quem quiser o release inteiro. As duas telas
mostram o texto -- Atualizações no cartão da versão nova e no do sistema,
Configurações no cartão de atualização, que é onde ele pediu.

Um manifesto antigo continua funcionando, e é o caso de qualquer caixa que ficar
sem atualizar por um tempo: uma URL sozinha em ``notes`` sai de lá e reaparece
como link. O campo não fica mentindo que aquilo é um changelog.
"""

from __future__ import annotations

import io
import os

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(RAIZ, "web", "views", "settings.js")
UPDATES = os.path.join(RAIZ, "web", "views", "updates.js")
CSS = os.path.join(RAIZ, "web", "style.css")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")
RELEASE_YML = os.path.join(RAIZ, ".github", "workflows", "release.yml")
IMAGE_YML = os.path.join(RAIZ, ".github", "workflows", "image.yml")


def _ler(caminho):
    return io.open(caminho, encoding="utf-8").read()


# --------------------------------------------------------------------------- texto x endereço


def test_um_endereco_sozinho_nao_e_changelog():
    from project_os.core.updates import _notes_text

    assert _notes_text("https://github.com/x/y/releases/tag/v1") == ""
    assert _notes_text("  https://exemplo/x  ") == ""


def test_mas_ele_reaparece_onde_serve():
    from project_os.core.updates import _notes_link

    velho = {"notes": "https://github.com/x/y/releases/tag/v1"}
    assert _notes_link(velho) == "https://github.com/x/y/releases/tag/v1"
    novo = {"notes": "mudou isto", "notes_url": "https://github.com/x/y/releases/tag/v2"}
    assert _notes_link(novo) == "https://github.com/x/y/releases/tag/v2"


def test_um_texto_que_comeca_com_link_continua_sendo_texto():
    """Só some quando é *só* o endereço; senão apagaria changelog de verdade."""
    from project_os.core.updates import _notes_text

    assert _notes_text("https://exemplo/x\ne mais isto") == "https://exemplo/x\ne mais isto"


def test_o_texto_tem_teto():
    """Uma mensagem enorme não pode virar uma página de rolagem no cartão."""
    from project_os.core.updates import NOTES_MAX, _notes_text

    assert len(_notes_text("a" * (NOTES_MAX * 2))) == NOTES_MAX
    assert 1000 <= NOTES_MAX <= 20000


def test_o_que_o_servidor_devolve_na_conferida(auth_client, monkeypatch):
    from project_os.core import updates

    manifesto = {
        "version": "9.9.9",
        "url": "https://exemplo/pacote.tar.gz",
        "sha256": "a" * 64,
        "notes": "9.9.9: assunto\n\nmotivo em outra linha",
        "notes_url": "https://github.com/x/y/releases/tag/v9.9.9",
    }
    monkeypatch.setattr(updates, "_fetch_json", lambda url: manifesto)
    corpo = updates.check_tarball("https://exemplo/latest.json")
    assert corpo["notes"].startswith("9.9.9: assunto")
    assert "\n" in corpo["notes"], "as quebras de linha são o que separa assunto de motivo"
    assert corpo["notes_url"].endswith("/v9.9.9")


def test_no_git_a_mensagem_inteira_e_nao_so_o_assunto():
    """Numa instalação por clone, o changelog é o corpo do commit."""
    fonte = _ler(os.path.join(RAIZ, "project_os", "core", "updates.py"))
    trecho = fonte[fonte.index("def check_git("):]
    trecho = trecho[:trecho.index("\ndef ")]
    assert "--pretty=%B" in trecho, "%s traz só a primeira linha"


def test_o_sistema_tambem(monkeypatch):
    """880 MB e um reinício: é a atualização em que mais importa poder ler."""
    from project_os.core import sysupdate

    manifesto = {
        "system": {
            "version": "9.9.9", "url": "https://exemplo/rootfs.tar.gz",
            "sha256": "b" * 64, "size": 10,
            "notes": "9.9.9: sistema novo\n\ncom motivo",
            "notes_url": "https://github.com/x/y/releases/tag/v9.9.9",
        }
    }
    monkeypatch.setattr(sysupdate.updates, "_fetch_json", lambda url: manifesto)
    corpo = sysupdate.check("https://exemplo/latest.json")
    assert corpo["notes"].startswith("9.9.9: sistema novo")
    assert corpo["notes_url"].endswith("/v9.9.9")


# --------------------------------------------------------------------------- as telas


def test_configuracoes_mostra_o_que_mudou():
    """Foi onde ele pediu: decidir sem ter que abrir outra tela."""
    fonte = _ler(SETTINGS)
    trecho = fonte[fonte.index("function updatesSection()"):]
    trecho = trecho[:trecho.index("\n    async function power(")]
    assert "changelog__box" in trecho
    assert "achado.notes" in trecho
    assert "achado.notes_url" in trecho, "o link continua existindo, à parte do texto"


def test_atualizacoes_mostra_nos_dois_cartoes():
    fonte = _ler(UPDATES)
    assert "function notesBlock(" in fonte
    assert fonte.count("notesBlock(") >= 4, "cartão da versão nova, o de em dia, e o do sistema"
    assert "sistema.update_available ? notesBlock(sistema)" in fonte


def test_as_quebras_de_linha_sobrevivem():
    """<p> junta tudo num parágrafo só, e o texto vem de uma mensagem de commit."""
    assert "h('pre', {class: 'changelog'}" in _ler(UPDATES)
    assert "h('pre', {class: 'changelog'}" in _ler(SETTINGS)
    css = _ler(CSS)
    trecho = css[css.index(".changelog {"):]
    trecho = trecho[:trecho.index("}")]
    assert "white-space: pre-wrap" in trecho


def test_o_bloco_nao_empurra_a_pagina():
    """Item de flex/grid nasce com min-width auto; a linha mais comprida mandava.

    Medido no navegador antes disto: bloco de 451px numa janela de 375, e a
    página inteira ganhando rolagem lateral.
    """
    css = _ler(CSS)
    trecho = css[css.index(".changelog {"):]
    trecho = trecho[:trecho.index("}")]
    assert "min-width: 0" in trecho
    assert "max-width: 100%" in trecho
    assert "max-height" in trecho, "o botão de instalar tem que continuar alcançável"


def test_o_portugues_do_link_existe():
    assert "'updates.notes.full':" in _ler(PT)


# --------------------------------------------------------------------------- quem enche o campo


def test_o_release_grava_texto_e_endereco_separados():
    yml = _ler(RELEASE_YML)
    assert "git log -1 --format=%B HEAD" in yml, "sem isto o campo volta a ser um link"
    assert 'dados["notes_url"] =' in yml
    assert 'dados["notes"] = notas' in yml


def test_a_imagem_tambem():
    yml = _ler(IMAGE_YML)
    assert "notas-sistema.txt" in yml
    assert '"notes_url":' in yml


def test_a_mensagem_e_colhida_antes_de_trocar_de_branch():
    """Depois do checkout o HEAD é outro commit, e a mensagem seria a errada."""
    yml = _ler(IMAGE_YML)
    assert yml.index("notas-sistema.txt") < yml.index("git checkout -B main origin/main")


# --------------------------------------------------------------------------- a tira de abas


def test_a_tira_de_abas_rola_dentro_de_si():
    """Seis abas somam mais que a largura de um celular.

    Sem rolagem própria, a tira definia a largura da coluna da grade: os cartões
    saíam pela direita e a página inteira rolava de lado. Medido no navegador
    antes: página de 491px numa janela de 375, com o cartão terminando fora da
    tela. Depois: 375 exatos, e a tira rolando 522px dentro de 349.
    """
    assert "segmented segmented--scroll" in _ler(SETTINGS)
    css = _ler(CSS)
    trecho = css[css.index(".segmented--scroll {"):]
    trecho = trecho[:trecho.index("}")]
    assert "overflow-x: auto" in trecho
    assert "max-width: 100%" in trecho
