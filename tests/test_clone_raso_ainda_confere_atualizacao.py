"""Num clone raso a tela de atualizações respondia 500.

O `pytest` que passou a barrar o release rodou primeiro num checkout de **tag**,
que o actions/checkout traz raso (`--depth 1`) e destacado -- e ali dois testes
caíram com `git_failed: git exited 1`. Não era falha do CI: é o que acontece em
qualquer `git clone --depth 1`.

O refspec de um clone assim só traz a referência pedida. O `git fetch origin
main` seguinte funciona e deposita o resultado em `FETCH_HEAD`, mas **não cria**
`origin/main`. Como `check_git` resolvia `origin/<branch>` direto, o `rev-parse`
saía com código 1, virava `UpdateError`, e a rota devolvia 500: a tela não
dizia nem que havia versão nova, nem que não dava para conferir.

`apply_git` tinha o mesmo `origin/<branch>` no `reset --hard` -- ali o estrago
seria pior, porque a caixa ficaria na versão velha depois de dizer que atualizou.

Também trava a contagem: num clone raso o histórico não alcança a base comum e
`rev-list --count` devolve o repositório inteiro (213, medido), o que na tela
vira "213 commits atrás" de uma versão publicada no mesmo dia.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from project_os.core import updates

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="precisa de git")


def _rodar(*args, **kwargs):
    subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


@pytest.fixture
def clone_raso(tmp_path):
    """Uma origem de verdade e um clone `--depth 1 --branch <tag>` dela.

    Sem repositório real isto não prova nada: o defeito estava exatamente no
    que o git faz com o refspec de um clone assim.
    """
    origem = tmp_path / "origem"
    origem.mkdir()
    _rodar("git", "init", "--quiet", "--initial-branch=main", str(origem))
    _rodar("git", "-C", str(origem), "config", "user.email", "t@t")
    _rodar("git", "-C", str(origem), "config", "user.name", "t")
    (origem / "a.txt").write_text("um\n")
    _rodar("git", "-C", str(origem), "add", "-A")
    _rodar("git", "-C", str(origem), "commit", "--quiet", "-m", "primeiro")
    _rodar("git", "-C", str(origem), "tag", "v1")

    destino = tmp_path / "raso"
    _rodar("git", "clone", "--quiet", "--depth", "1", "--branch", "v1",
           "file://%s" % origem, str(destino))

    # A origem anda depois do clone: é o caso que interessa, "tem versão nova".
    (origem / "a.txt").write_text("dois\n")
    _rodar("git", "-C", str(origem), "add", "-A")
    _rodar("git", "-C", str(origem), "commit", "--quiet", "-m", "mensagem da versão nova")
    return str(origem), str(destino)


def test_o_clone_e_mesmo_raso_e_destacado(clone_raso):
    """Se o cenário parar de ser raso, o resto do arquivo não prova nada."""
    _origem, raso = clone_raso
    assert updates._e_raso(raso) is True
    assert updates.is_git_checkout(raso) is True


def test_origin_main_nao_existe_nesse_clone(clone_raso):
    """O fato que causava o 500, preso aqui para não virar suposição."""
    _origem, raso = clone_raso
    _rodar("git", "-C", raso, "fetch", "--quiet", "--tags", "origin", "main")
    saida = subprocess.run(["git", "-C", raso, "rev-parse", "origin/main"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert saida.returncode != 0, "o clone deixou de ser o cenário do defeito"


def test_conferir_atualizacao_nao_estoura_no_clone_raso(clone_raso):
    _origem, raso = clone_raso
    info = updates.check_git("main", raso)
    assert info["update_available"] is True
    assert "mensagem da versão nova" in info["notes"]


def test_a_contagem_nao_inventa_numero_no_clone_raso(clone_raso):
    """Melhor não dizer número do que dizer "213 commits atrás"."""
    _origem, raso = clone_raso
    assert updates.check_git("main", raso)["commits_behind"] == 0


def test_ramo_que_nao_existe_da_erro_com_nome_proprio(clone_raso):
    """Sem ramo nenhum ainda é erro -- mas com código que a tela sabe mostrar."""
    _origem, raso = clone_raso
    with pytest.raises(updates.UpdateError) as capturado:
        updates.check_git("ramo-que-nao-existe", raso)
    assert capturado.value.code in ("branch_missing", "git_failed")


def test_atualizar_de_verdade_chega_no_commit_novo(clone_raso):
    """`apply_git` resetava para `origin/<branch>` -- o mesmo ref inexistente.

    Ali o silêncio seria pior: a tela diria "atualizado" e a caixa continuaria
    na versão velha.
    """
    origem, raso = clone_raso
    esperado = subprocess.run(["git", "-C", origem, "rev-parse", "HEAD"],
                              stdout=subprocess.PIPE, check=True).stdout.decode().strip()
    updates.apply_git({"branch": "main"}, raso)
    agora = subprocess.run(["git", "-C", raso, "rev-parse", "HEAD"],
                           stdout=subprocess.PIPE, check=True).stdout.decode().strip()
    assert agora == esperado
    assert (os.path.join(raso, "a.txt")) and open(os.path.join(raso, "a.txt")).read() == "dois\n"


# --------------------------------------------------------- a tag que diverge


@pytest.fixture
def tag_divergente(clone_raso):
    """Mesma tag apontando para commits diferentes aqui e lá.

    Acontece de verdade quando um release é remarcado lá em cima -- e foi o que
    aconteceu aqui, entre um push recusado e um rebase.
    """
    origem, clone = clone_raso
    _rodar("git", "-C", origem, "tag", "-f", "v1")          # v1 anda na origem
    _rodar("git", "-C", clone, "tag", "-f", "v1", "HEAD")   # v1 fica onde está aqui
    return origem, clone


def test_a_tag_divergente_realmente_derruba_o_fetch_com_tags(tag_divergente):
    """O fato por trás do defeito: `--tags` sai com código 1, não zero."""
    _origem, clone = tag_divergente
    saida = subprocess.run(["git", "-C", clone, "fetch", "--tags", "origin", "main"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert saida.returncode != 0
    assert b"clobber" in saida.stderr, saida.stderr


def test_uma_tag_divergente_nao_derruba_a_conferida(tag_divergente):
    """Enfeite não manda: a pergunta da tela é sobre o ramo."""
    _origem, clone = tag_divergente
    info = updates.check_git("main", clone)
    assert info["update_available"] is True
    assert "mensagem da versão nova" in info["notes"]


def test_e_a_atualizacao_tambem_passa(tag_divergente):
    origem, clone = tag_divergente
    esperado = subprocess.run(["git", "-C", origem, "rev-parse", "HEAD"],
                              stdout=subprocess.PIPE, check=True).stdout.decode().strip()
    updates.apply_git({"branch": "main"}, clone)
    agora = subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"],
                           stdout=subprocess.PIPE, check=True).stdout.decode().strip()
    assert agora == esperado
