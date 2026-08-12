"""Atualizar o app quando quem manda na pasta de cima é o root.

Na imagem, o código mora em ``/opt/project-os`` e o serviço roda como usuário
``project-os``. A pasta do código é dele -- o stage faz ``chown -R`` --, mas
``/opt`` continua sendo ``root:root 755``, do jeito que vem no Debian.

E a troca de versão não acontece dentro da pasta do código: ela acontece **em
volta** dela. O jeito de trocar sem deixar meio sistema no lugar é:

    /opt/.project_os-update-xxxx/tree   <- extrai aqui
    /opt/project-os -> /opt/project-os.previous-0.4.6
    /opt/.../tree   -> /opt/project-os

Três operações em ``/opt``, nenhuma em ``/opt/project-os``. Sem permissão de
escrita na pasta de cima, todas falham -- e a primeira falhava depois de baixar
o pacote inteiro, com um ``PermissionError`` cru aparecendo na tela dele:

    [Errno 13] Permission denied: '/opt/.project_os-update-tjsd_svi'

Isto foi encontrado lendo o código enquanto o Pi dele estava ligado com a 0.4.6
esperando ele criar a conta. Ninguém tinha apertado o botão ainda.

Duas metades de conserto, e as duas têm teste aqui:

* o código recusa antes de baixar, com um motivo que dá para agir -- nesta caixa
  a atualização é do sistema inteiro, que passa por um ajudante root que existe;
* a imagem passa a deixar o grupo ``project-os`` escrever em ``/opt``, para que
  um conserto de app não exija baixar um rootfs de meio giga.

O ``chmod 555`` daqui é como se testa isso sem ser root: o efeito é o mesmo que
``/opt`` ser do root -- não dá para criar nem renomear nada dentro.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture()
def arvore_com_pai_fechado(tmp_path):
    """Uma /opt/project-os de mentira dentro de uma /opt onde não se escreve."""
    pai = tmp_path / "opt"
    onde = pai / "project-os"
    (onde / "project_os").mkdir(parents=True)
    (onde / "project_os" / "__init__.py").write_text('__version__ = "0.4.6"\n', encoding="utf-8")
    (onde / "PEDIDOS.md").write_text("as anotações dele\n", encoding="utf-8")
    os.chmod(str(pai), 0o555)
    yield onde
    os.chmod(str(pai), 0o755)


INFO = {
    "method": "tarball",
    "url": "https://example.invalid/project-os.tar.gz",
    "sha256": "0" * 64,
    "current": "0.4.6",
    "latest": "0.4.7",
}


def test_recusa_com_motivo_em_vez_de_permission_error(arvore_com_pai_fechado):
    from project_os.core import updates

    with pytest.raises(updates.UpdateError) as caiu:
        updates.apply_tarball(dict(INFO), root=str(arvore_com_pai_fechado))

    assert caiu.value.code == "root_not_writable"
    # O motivo tem que dizer o que fazer, não só que deu errado.
    assert "sistema" in (caiu.value.hint or "").lower()


def test_recusa_antes_de_baixar_o_pacote(arvore_com_pai_fechado, monkeypatch):
    """Meio giga de download para descobrir que não dá é pior que não tentar."""
    from project_os.core import updates

    def nao_pode_baixar(*a, **k):
        raise AssertionError("baixou antes de conferir se dava para instalar")

    monkeypatch.setattr(updates, "_download", nao_pode_baixar)
    with pytest.raises(updates.UpdateError):
        updates.apply_tarball(dict(INFO), root=str(arvore_com_pai_fechado))


def test_nada_foi_mexido_na_pasta_do_codigo(arvore_com_pai_fechado):
    from project_os.core import updates

    with pytest.raises(updates.UpdateError):
        updates.apply_tarball(dict(INFO), root=str(arvore_com_pai_fechado))

    assert (arvore_com_pai_fechado / "project_os" / "__init__.py").exists()
    assert (arvore_com_pai_fechado / "PEDIDOS.md").read_text(encoding="utf-8").strip() == (
        "as anotações dele"
    )
    assert os.listdir(str(arvore_com_pai_fechado.parent)) == ["project-os"]


def test_pode_instalar_diz_nao_com_o_motivo(arvore_com_pai_fechado):
    """A tela precisa saber disso antes de oferecer o botão."""
    from project_os.core import updates

    pode, motivo = updates.can_apply(str(arvore_com_pai_fechado))
    assert pode is False
    assert "opt" in motivo


def test_pode_instalar_diz_sim_no_caso_normal(tmp_path):
    from project_os.core import updates

    onde = tmp_path / "project-os"
    (onde / "project_os").mkdir(parents=True)
    pode, motivo = updates.can_apply(str(onde))
    assert pode is True
    assert motivo == ""


def test_um_checkout_git_nao_e_barrado(arvore_com_pai_fechado):
    """Onde a atualização é ``git reset --hard``, a pasta de cima não importa.

    Barrar aqui seria trocar um botão que falha por um botão que nem aparece --
    e o install.sh instala exatamente assim, num /opt que também é do root.
    """
    from project_os.core import updates

    (arvore_com_pai_fechado / ".git").mkdir()
    pode, motivo = updates.can_apply(str(arvore_com_pai_fechado))
    assert pode is True, motivo


def test_a_checagem_leva_o_aviso_junto(monkeypatch, tmp_path):
    """``check()`` é o que a tela lê; o aviso viaja com ele."""
    from project_os.core import updates

    monkeypatch.setattr(
        updates, "check_tarball",
        lambda url: {"method": "tarball", "current": "0.4.6", "latest": "0.4.7",
                     "update_available": True, "url": "x", "sha256": "y"},
    )
    monkeypatch.setattr(updates, "method", lambda root=None: updates.METHOD_TARBALL)

    pai = tmp_path / "opt"
    onde = pai / "project-os"
    onde.mkdir(parents=True)
    os.chmod(str(pai), 0o555)
    try:
        resultado = updates.check(root=str(onde))
    finally:
        os.chmod(str(pai), 0o755)

    assert resultado["can_install"] is False
    assert resultado["install_blocked"]


def test_o_aviso_chega_na_tela_pelo_http(auth_client, monkeypatch):
    """Do núcleo até o JSON que a tela lê, sem ninguém filtrar no caminho."""
    from project_os.core import updates as live

    monkeypatch.setattr(
        live, "check",
        lambda *a, **k: {"method": "tarball", "current": "0.4.6", "latest": "0.4.7",
                         "update_available": True, "can_install": False,
                         "install_blocked": "Não posso escrever em /opt",
                         "install_hint": live.SYSTEM_UPDATE_HINT},
    )
    resposta = auth_client.post("/api/updates/check")
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["can_install"] is False
    assert "Atualizar sistema" in corpo["install_hint"]


def test_a_imagem_deixa_o_grupo_escrever_no_opt():
    """A outra metade: sem isto, a caixa só se atualiza por rootfs inteiro.

    Não é abrir mão de nada -- o mesmo sudoers já dá ``apt-get`` sem senha a
    este usuário, e apt-get sem senha é root com passos extras. O que se ganha é
    um conserto de app chegar sem meio giga de download.
    """
    caminho = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "image", "stage-project-os", "00-project-os", "01-run.sh",
    )
    with open(caminho, "r", encoding="utf-8") as arquivo:
        texto = arquivo.read()

    linhas = [
        linha.strip() for linha in texto.splitlines()
        if linha.strip() and not linha.strip().startswith("#")
    ]
    grupo = [l for l in linhas if l.startswith("chgrp") and l.endswith("/opt")]
    modo = [l for l in linhas if l.startswith("chmod") and l.endswith("/opt")]
    assert grupo, "o /opt da imagem não muda de grupo"
    assert "project-os" in grupo[0]
    assert modo and "775" in modo[0], modo
