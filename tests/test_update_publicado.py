"""A atualização do app, do manifesto publicado até os arquivos trocados.

É o botão que ele mais aperta, e o único caminho de conserto que não exige nem
reiniciar. Os testes de unidade cobrem as peças com um servidor de mentira; este
roda contra o release de verdade, na internet, num diretório descartável.

O que ele prova, que nenhum teste com dados inventados prova: o manifesto
publicado existe, anuncia uma versão, traz sha256, e o arquivo que ele aponta
baixa e bate. Um manifesto com um sha errado -- ou um release publicado sem o
arquivo -- transformaria toda atualização em erro, e a descoberta seria no dia
em que ele precisasse de um conserto.

Pulado sem rede.
"""

from __future__ import annotations

import os

import pytest


def rede_disponivel() -> bool:
    if os.environ.get("PROJECT_OS_SEM_REDE"):
        return False
    import socket

    try:
        socket.create_connection(("github.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not rede_disponivel(), reason="precisa de internet")


@pytest.fixture()
def raiz_falsa(tmp_path):
    """Um /opt/project-os de mentira, com virtualenv e um arquivo do dono."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "project_os").mkdir()
    (tmp_path / "project_os" / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "project-os").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "PEDIDOS.md").write_text("as anotações dele\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def anunciado():
    """O que o manifesto publicado diz agora.

    check_tarball direto, e não check(): rodando de dentro de um checkout git o
    check() escolheria o caminho do git. No Pi, /opt/project-os não é checkout
    -- o stage da imagem copia os arquivos, sem .git -- então o caminho é este.
    """
    from project_os.core import updates

    return updates.check_tarball(updates.DEFAULT_MANIFEST_URL)


def test_o_manifesto_publicado_anuncia_uma_versao_e_um_sha(anunciado):
    assert anunciado.get("latest"), "o manifesto não anunciou versão: %r" % anunciado
    assert anunciado.get("url"), "o manifesto não trouxe url"
    assert len(anunciado.get("sha256") or "") == 64, (
        "sem sha256 no manifesto, toda atualização seria um convite a quem "
        "conseguisse responder pelo servidor"
    )


def test_a_atualizacao_baixa_confere_e_troca_os_arquivos(anunciado, raiz_falsa):
    from project_os.core import updates

    updates.apply_tarball(anunciado, root=str(raiz_falsa), on_line=lambda _l: None)

    texto = (raiz_falsa / "project_os" / "__init__.py").read_text(encoding="utf-8")
    assert anunciado["latest"] in texto, "os arquivos novos não chegaram ao disco"


def test_o_virtualenv_e_as_anotacoes_sobrevivem(anunciado, raiz_falsa):
    """Apagar o venv derrubaria o serviço; apagar o PEDIDOS.md é imperdoável."""
    from project_os.core import updates

    updates.apply_tarball(anunciado, root=str(raiz_falsa), on_line=lambda _l: None)

    assert (raiz_falsa / ".venv" / "bin" / "python3").exists()
    assert (raiz_falsa / "PEDIDOS.md").read_text(encoding="utf-8").strip() == "as anotações dele"


def test_um_tarball_adulterado_e_recusado(anunciado, raiz_falsa):
    from project_os.core import updates

    mentira = dict(anunciado)
    mentira["sha256"] = "0" * 64

    with pytest.raises(Exception) as erro:
        updates.apply_tarball(mentira, root=str(raiz_falsa), on_line=lambda _l: None)

    assert not isinstance(erro.value, KeyError), "recusou por falta de campo, não pelo sha"
    assert "0.0.1" in (raiz_falsa / "project_os" / "__init__.py").read_text(encoding="utf-8"), \
        "escreveu antes de conferir a soma"


def test_o_manifesto_de_sistema_aponta_para_um_arquivo_que_existe():
    """A atualização de sistema é a que evita a viagem até o PC.

    Um manifesto que anuncia um sistema cujo arquivo não existe -- ou cujo
    tamanho não bate -- transforma o botão "Atualizar sistema" em erro. E a
    descoberta seria no dia em que ele precisasse dele.

    Confere sem baixar 880 MB: um HEAD na URL do release e o Content-Length
    contra o tamanho anunciado.
    """
    import urllib.request

    from project_os.core import updates

    manifesto = updates._fetch_json(updates.DEFAULT_MANIFEST_URL)
    sistema = manifesto.get("system") or {}
    if not sistema:
        pytest.skip("o manifesto ainda não anuncia um sistema")

    assert len(sistema.get("sha256") or "") == 64, "o sistema anunciado veio sem sha256"
    assert sistema.get("url"), "o sistema anunciado veio sem url"

    pedido = urllib.request.Request(sistema["url"], method="HEAD")
    with urllib.request.urlopen(pedido, timeout=30) as resposta:
        tamanho = int(resposta.headers.get("Content-Length") or 0)

    esperado = int(sistema.get("size") or 0)
    if esperado:
        assert tamanho == esperado, (
            "o arquivo do sistema tem %d bytes e o manifesto anuncia %d" % (tamanho, esperado)
        )
    else:
        assert tamanho > 100 * 1024 * 1024, "o arquivo do sistema é pequeno demais para ser um rootfs"
