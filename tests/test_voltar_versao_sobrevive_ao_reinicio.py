"""O botão de voltar versão sumia -- e, se aparecesse, deixava a caixa sem python.

Medido contra o release recém-publicado, numa instalação de mentira que foi de
0.4.8 para 0.4.9 pela rede. Depois do reinício (que a própria atualização faz),
``POST /api/updates/rollback`` respondia *"Não existe versão anterior para
voltar"* com a pasta ``…​.previous-0.4.8`` ali do lado, no disco. Dois defeitos
somados:

1. o caminho da versão anterior morava num objeto de memória do processo, e a
   atualização reinicia o processo -- ou seja, o botão desaparecia exatamente
   depois da única ação que o torna útil;

2. e se ele tivesse funcionado, seria pior: a atualização **move** o ``.venv``
   para a árvore nova, então a árvore guardada está sem ele. Voltar restaurava
   uma instalação sem interpretador, o ``bin/project-os`` caía no python do
   sistema -- que na imagem não tem uvicorn -- e o serviço não subia mais. O
   socorro deixava a caixa pior que o acidente.

Agora a versão anterior é procurada no disco a cada pedido, o que fica de pé
volta junto, e a tela tem um cartão próprio em vez de um botão dentro do cartão
do trabalho em andamento.
"""

from __future__ import annotations

import io
import os
import tarfile

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _arvore(caminho, versao, com_venv=True):
    """Uma instalação do project-os de mentira, com o mínimo que o updater exige."""
    os.makedirs(os.path.join(caminho, "project_os"))
    with io.open(os.path.join(caminho, "project_os", "__init__.py"), "w", encoding="utf-8") as f:
        f.write('__version__ = "%s"\n' % versao)
    if com_venv:
        os.makedirs(os.path.join(caminho, ".venv", "bin"))
        with io.open(os.path.join(caminho, ".venv", "bin", "python3"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
    with io.open(os.path.join(caminho, "PEDIDOS.md"), "w", encoding="utf-8") as f:
        f.write("as anotações dele\n")
    return caminho


# --------------------------------------------------------------------------- o disco


def test_a_versao_anterior_e_achada_no_disco(tmp_path):
    from project_os.core import updates

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.4.9")
    _arvore(onde + ".previous-0.4.8", "0.4.8", com_venv=False)

    achadas = updates.previous_versions(root=onde)
    assert [a["version"] for a in achadas] == ["0.4.8"]
    assert achadas[0]["path"] == onde + ".previous-0.4.8"


def test_sem_atualizacao_nenhuma_a_lista_e_vazia(tmp_path):
    from project_os.core import updates

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.4.9")
    assert updates.previous_versions(root=onde) == []


def test_a_mais_nova_vem_primeiro(tmp_path):
    from project_os.core import updates

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.5.0")
    velha = _arvore(onde + ".previous-0.4.7", "0.4.7", com_venv=False)
    nova = _arvore(onde + ".previous-0.4.9", "0.4.9", com_venv=False)
    os.utime(velha, (1000, 1000))
    os.utime(nova, (2000, 2000))

    assert [a["version"] for a in updates.previous_versions(root=onde)] == ["0.4.9", "0.4.7"]


# --------------------------------------------------------------------------- o venv


def test_voltar_traz_o_venv_de_volta(tmp_path):
    """O defeito que transformava o socorro em acidente."""
    from project_os.core import updates

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.4.9")                                    # tem o venv
    anterior = _arvore(onde + ".previous-0.4.8", "0.4.8", com_venv=False)  # ficou sem

    updates.rollback(anterior, root=onde)

    assert os.path.isfile(os.path.join(onde, ".venv", "bin", "python3")), \
        "voltou sem interpretador: o serviço não subiria"
    texto = io.open(os.path.join(onde, "project_os", "__init__.py"), encoding="utf-8").read()
    assert '"0.4.8"' in texto, "o código antigo é que tinha que voltar"
    assert not os.path.exists(anterior), "a pasta anterior virou a instalação"


def test_voltar_nao_atropela_um_venv_que_ja_existe(tmp_path):
    from project_os.core import updates

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.4.9")
    anterior = _arvore(onde + ".previous-0.4.8", "0.4.8", com_venv=True)
    marca = os.path.join(anterior, ".venv", "bin", "marca")
    with io.open(marca, "w", encoding="utf-8") as f:
        f.write("o venv da versão antiga\n")

    updates.rollback(anterior, root=onde)
    assert os.path.isfile(os.path.join(onde, ".venv", "bin", "marca"))


def test_a_ida_e_a_volta_pelo_caminho_de_verdade(tmp_path):
    """apply_tarball e rollback em sequência, sem rede: file:// serve.

    Testar só o rollback isolado esconderia o defeito -- ele nasce do que a
    *atualização* faz com o venv, e não do que o rollback deixa de fazer.
    """
    import hashlib

    from project_os.core import updates

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.4.8")

    novo = str(tmp_path / "empacotar" / "project-os-0.4.9")
    _arvore(novo, "0.4.9", com_venv=False)
    pacote = str(tmp_path / "project-os-0.4.9.tar.gz")
    with tarfile.open(pacote, "w:gz") as tar:
        tar.add(novo, arcname="project-os-0.4.9")
    soma = hashlib.sha256(io.open(pacote, "rb").read()).hexdigest()

    resultado = updates.apply_tarball(
        {"url": "file://" + pacote, "sha256": soma, "latest": "0.4.9", "current": "0.4.8"},
        root=onde, on_line=lambda _l: None,
    )
    assert os.path.isfile(os.path.join(onde, ".venv", "bin", "python3")), "o venv foi para a nova"
    assert not os.path.exists(os.path.join(resultado["previous"], ".venv")), \
        "a árvore guardada fica sem venv -- é essa a premissa do conserto"

    updates.rollback(resultado["previous"], root=onde)
    assert os.path.isfile(os.path.join(onde, ".venv", "bin", "python3"))
    assert '"0.4.8"' in io.open(
        os.path.join(onde, "project_os", "__init__.py"), encoding="utf-8"
    ).read()
    assert io.open(os.path.join(onde, "PEDIDOS.md"), encoding="utf-8").read().strip() == \
        "as anotações dele"


# --------------------------------------------------------------------------- o HTTP


@pytest.fixture()
def instalacao(tmp_path, monkeypatch):
    """Uma instalação com versão anterior guardada, e o serviço apontado para ela."""
    from project_os.core import updates

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.4.9")
    _arvore(onde + ".previous-0.4.8", "0.4.8", com_venv=False)
    monkeypatch.setattr(updates, "root_dir", lambda: onde)
    return onde


def test_o_status_conta_a_versao_guardada(auth_client, instalacao):
    corpo = auth_client.get("/api/updates").json()
    assert corpo["previous"]["version"] == "0.4.8"
    assert corpo["previous"]["path"] == instalacao + ".previous-0.4.8"


def test_sem_nada_guardado_o_status_diz_nada(auth_client, tmp_path, monkeypatch):
    from project_os.core import updates

    onde = str(tmp_path / "sozinho")
    _arvore(onde, "0.4.9")
    monkeypatch.setattr(updates, "root_dir", lambda: onde)
    assert auth_client.get("/api/updates").json()["previous"] is None


def test_voltar_funciona_depois_do_reinicio(auth_client, instalacao):
    """O caso de verdade: memória vazia, pasta no disco.

    Um processo recém-iniciado não tem trabalho nenhum na memória -- é assim
    que a caixa está logo depois de atualizar.
    """
    from project_os.api import updates as api_updates

    api_updates._job.previous = ""
    api_updates._job.state = "idle"

    resposta = auth_client.post("/api/updates/rollback")
    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["version"] == "0.4.8"
    assert '"0.4.8"' in io.open(
        os.path.join(instalacao, "project_os", "__init__.py"), encoding="utf-8"
    ).read()


def test_sem_versao_anterior_a_recusa_explica(auth_client, tmp_path, monkeypatch):
    from project_os.api import updates as api_updates
    from project_os.core import updates

    onde = str(tmp_path / "sozinho")
    _arvore(onde, "0.4.9")
    monkeypatch.setattr(updates, "root_dir", lambda: onde)
    api_updates._job.previous = ""

    resposta = auth_client.post("/api/updates/rollback")
    assert resposta.status_code == 404
    assert resposta.json()["error"] == "no_previous"


def test_reiniciar_e_um_botao_e_nao_um_ssh(auth_client, monkeypatch):
    """Voltar os arquivos não volta a memória; sem reiniciar, a caixa mente."""
    from project_os.core import updates

    chamou = []
    monkeypatch.setattr(updates, "restart", lambda on_line=None: chamou.append(True) or "systemd")

    resposta = auth_client.post("/api/updates/restart")
    assert resposta.status_code == 200, resposta.text

    import time

    for _ in range(50):  # a thread reinicia por fora do pedido
        if chamou:
            break
        time.sleep(0.05)
    assert chamou, "o endpoint respondeu ok e não reiniciou nada"


def test_reiniciar_exige_login(client):
    assert client.post("/api/updates/restart").status_code in (401, 403, 428)


# --------------------------------------------------------------------------- a tela


def _tela():
    return io.open(os.path.join(RAIZ, "web", "views", "updates.js"), encoding="utf-8").read()


def test_o_botao_de_voltar_saiu_do_cartao_do_trabalho():
    fonte = _tela()
    corte = fonte.index("function jobCard()")
    trecho = fonte[corte:fonte.index("function previousCard()")]
    assert "rollback" not in trecho, (
        "o botão dentro do jobCard some quando o serviço reinicia -- que é quando ele importa"
    )


def test_o_cartao_de_voltar_le_o_que_o_status_diz():
    fonte = _tela()
    assert "function previousCard()" in fonte
    assert "state.info || {}).previous" in fonte
    assert "previousCard()" in fonte.split("mount(slot, [")[1].split("]")[0], \
        "o cartão existe e não é montado"


def test_voltar_pergunta_antes_e_reinicia_depois():
    fonte = _tela()
    corte = fonte.index("function previousCard()")
    trecho = fonte[corte:corte + 1400]
    assert "confirm(" in trecho, "voltar versão sem perguntar é surpresa demais"
    assert "restartNow()" in trecho
    assert "/updates/restart" in fonte


def test_as_frases_novas_existem_em_portugues():
    pt = io.open(os.path.join(RAIZ, "web", "lib", "strings-pt.js"), encoding="utf-8").read()
    for chave in ("updates.rollback.confirm", "updates.rollback.done",
                  "updates.previous.title", "updates.previous.body"):
        assert "'%s'" % chave in pt, "falta o português de %s" % chave


def test_o_toast_de_voltar_nao_e_mais_texto_cru_em_ingles():
    fonte = _tela()
    assert "'Rolled back.'" not in fonte


def test_depois_do_reinicio_a_pagina_recarrega():
    """A tela é trocada pela atualização junto com o resto da árvore.

    Sem recarregar, o navegador segue rodando o JavaScript da versão anterior
    contra a API da nova -- e a barra lateral continua anunciando a versão de
    antes do clique, que é a mesma tela dizendo dois números diferentes.
    """
    fonte = _tela()
    corte = fonte.index("function waitForRestart()")
    trecho = fonte[corte:corte + 1600]
    assert "location.reload()" in trecho, "a tela velha continua no ar contra a API nova"
    assert "RELOAD_DELAY_MS" in trecho, "recarregar antes do aviso apaga o aviso"
