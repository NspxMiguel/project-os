"""Atualizar o app quando quem manda na pasta de cima é o root.

Na imagem, o código mora em ``/opt/project-os`` e o serviço roda como usuário
``project-os``. A pasta do código é dele -- o stage faz ``chown -R`` --, mas num
cartão gravado antes da 0.4.8 o ``/opt`` continua ``root:root 755``, do jeito
que vem no Debian.

E a troca de versão acontecia só de um jeito: **em volta** da pasta do código.

    /opt/.project_os-update-xxxx/tree   <- extrai aqui
    /opt/project-os -> /opt/project-os.previous-0.4.6
    /opt/.../tree   -> /opt/project-os

Três operações em ``/opt``, nenhuma em ``/opt/project-os``. Sem escrita na pasta
de cima todas falham, e a primeira falhava depois de baixar o pacote inteiro:

    [Errno 13] Permission denied: '/opt/.project_os-update-tjsd_svi'

A primeira resposta foi recusar antes de baixar, com um motivo que dava para
agir: nesta caixa, atualize o sistema inteiro. Só que isso são 880 MB para
entregar uma correção de 700 KB -- e o cartão dele estava parado na 0.4.6
justamente por causa disso, apertando "Atualizar" e continuando na mesma versão.

Então a mesma troca passou a caber um andar abaixo, dentro da pasta que o
serviço já é dono: o que está lá desce para ``.previous-<versão>``, o que veio
no pacote sobe no lugar. O ``.venv`` não se mexe. Não é uma renomeação só, então
existe uma janela de milissegundos com a árvore pela metade -- e é um preço
barato perto de "esta caixa não se atualiza".

O ``chmod 555`` daqui é como se testa isso sem ser root: o efeito é o mesmo que
a pasta ser do root -- não dá para criar nem renomear nada dentro.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile

import pytest


# --------------------------------------------------------------------------- o cenário


def _arvore(caminho, versao, com_venv=True, extra=True):
    """Uma instalação do project-os de mentira, com o mínimo que o updater exige."""
    os.makedirs(os.path.join(caminho, "project_os"))
    with io.open(os.path.join(caminho, "project_os", "__init__.py"), "w", encoding="utf-8") as f:
        f.write('__version__ = "%s"\n' % versao)
    if extra:
        os.makedirs(os.path.join(caminho, "web"))
        with io.open(os.path.join(caminho, "web", "index.html"), "w", encoding="utf-8") as f:
            f.write("<!-- %s -->\n" % versao)
    if com_venv:
        os.makedirs(os.path.join(caminho, ".venv", "bin"))
        with io.open(os.path.join(caminho, ".venv", "bin", "python3"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
    with io.open(os.path.join(caminho, "PEDIDOS.md"), "w", encoding="utf-8") as f:
        f.write("as anotações dele\n")
    return caminho


def _pacote(tmp_path, versao):
    """Um release de verdade, empacotado como o CI empacota. Servido por ``file://``."""
    dentro = str(tmp_path / "empacotar" / ("project-os-%s" % versao))
    _arvore(dentro, versao, com_venv=False)
    caminho = str(tmp_path / ("project-os-%s.tar.gz" % versao))
    with tarfile.open(caminho, "w:gz") as tar:
        tar.add(dentro, arcname="project-os-%s" % versao)
    soma = hashlib.sha256(io.open(caminho, "rb").read()).hexdigest()
    return {
        "method": "tarball",
        "url": "file://" + caminho,
        "sha256": soma,
        "current": "0.4.6",
        "latest": versao,
    }


@pytest.fixture()
def arvore_com_pai_fechado(tmp_path):
    """Uma /opt/project-os de mentira dentro de uma /opt onde não se escreve."""
    pai = tmp_path / "opt"
    onde = pai / "project-os"
    _arvore(str(onde), "0.4.6")
    os.chmod(str(pai), 0o555)
    yield onde
    os.chmod(str(pai), 0o755)


def _versao_instalada(onde):
    caminho = os.path.join(str(onde), "project_os", "__init__.py")
    return io.open(caminho, encoding="utf-8").read().strip()


# --------------------------------------------------------------------------- por onde passa


def test_a_estrategia_diz_por_onde_a_troca_passa(tmp_path, arvore_com_pai_fechado):
    from project_os.core import updates

    solta = tmp_path / "normal" / "project-os"
    (solta / "project_os").mkdir(parents=True)
    assert updates.swap_strategy(str(solta)) == (updates.STRATEGY_PARENT, "")

    estrategia, motivo = updates.swap_strategy(str(arvore_com_pai_fechado))
    assert estrategia == updates.STRATEGY_IN_PLACE, motivo


def test_o_botao_aparece_no_cartao_dele(monkeypatch, arvore_com_pai_fechado):
    """O que estava travando a caixa dele: a tela não oferecia a atualização.

    ``check()`` é o que a tela lê. Com ``can_install`` falso ela mostra o aviso
    do sistema inteiro no lugar do botão -- e ele ficou na 0.4.6 apertando o que
    não ia funcionar.
    """
    from project_os.core import updates

    monkeypatch.setattr(
        updates, "check_tarball",
        lambda url: {"method": "tarball", "current": "0.4.6", "latest": "0.4.14",
                     "update_available": True, "url": "x", "sha256": "y"},
    )
    monkeypatch.setattr(updates, "method", lambda root=None: updates.METHOD_TARBALL)

    resultado = updates.check(root=str(arvore_com_pai_fechado))
    assert resultado["can_install"] is True
    assert resultado["install_blocked"] == ""
    assert "install_hint" not in resultado


def test_um_checkout_git_nao_e_barrado(arvore_com_pai_fechado):
    """Onde a atualização é ``git reset --hard``, a pasta de cima não importa.

    Barrar aqui seria trocar um botão que falha por um botão que nem aparece --
    e o install.sh instala exatamente assim, num /opt que também é do root.
    """
    from project_os.core import updates

    (arvore_com_pai_fechado / ".git").mkdir()
    estrategia, motivo = updates.swap_strategy(str(arvore_com_pai_fechado))
    assert estrategia == updates.STRATEGY_GIT, motivo


# --------------------------------------------------------------------------- a troca


def test_com_o_opt_fechado_a_troca_acontece_por_dentro(tmp_path, arvore_com_pai_fechado):
    """O caminho inteiro, com download e tudo. É o que o cartão dele vai fazer."""
    from project_os.core import updates

    onde = str(arvore_com_pai_fechado)
    resultado = updates.apply_tarball(
        _pacote(tmp_path, "0.4.14"), root=onde, on_line=lambda _l: None
    )

    assert resultado["strategy"] == updates.STRATEGY_IN_PLACE
    assert '"0.4.14"' in _versao_instalada(onde), "o código novo não entrou"
    assert "0.4.14" in io.open(
        os.path.join(onde, "web", "index.html"), encoding="utf-8"
    ).read(), "trocou o pacote python e esqueceu o resto da árvore"
    assert os.listdir(str(arvore_com_pai_fechado.parent)) == ["project-os"], \
        "escreveu na pasta de cima, que é justamente a que não deixa"


def test_o_venv_nao_sai_do_lugar(tmp_path, arvore_com_pai_fechado):
    """Mover o venv aqui seria movê-lo para dentro da pasta de trabalho -- que é apagada.

    E ele não precisa se mexer: os caminhos absolutos gravados dentro dele
    continuam valendo porque a pasta do código não muda de nome.
    """
    from project_os.core import updates

    onde = str(arvore_com_pai_fechado)
    marca = os.path.join(onde, ".venv", "bin", "marca")
    with io.open(marca, "w", encoding="utf-8") as f:
        f.write("este interpretador é o que faz o serviço subir\n")

    resultado = updates.apply_tarball(
        _pacote(tmp_path, "0.4.14"), root=onde, on_line=lambda _l: None
    )

    assert os.path.isfile(marca), "a caixa ficou sem interpretador"
    assert not os.path.exists(os.path.join(resultado["previous"], ".venv"))


def test_as_anotacoes_dele_ficam(tmp_path, arvore_com_pai_fechado):
    from project_os.core import updates

    onde = str(arvore_com_pai_fechado)
    updates.apply_tarball(_pacote(tmp_path, "0.4.14"), root=onde, on_line=lambda _l: None)
    assert io.open(os.path.join(onde, "PEDIDOS.md"), encoding="utf-8").read().strip() == \
        "as anotações dele"


def test_a_anterior_fica_guardada_dentro_da_arvore(tmp_path, arvore_com_pai_fechado):
    from project_os.core import updates

    onde = str(arvore_com_pai_fechado)
    resultado = updates.apply_tarball(
        _pacote(tmp_path, "0.4.14"), root=onde, on_line=lambda _l: None
    )

    assert resultado["previous"] == os.path.join(onde, ".previous-0.4.6")
    assert '"0.4.6"' in io.open(
        os.path.join(resultado["previous"], "project_os", "__init__.py"), encoding="utf-8"
    ).read()

    achadas = updates.previous_versions(root=onde)
    assert [a["version"] for a in achadas] == ["0.4.6"], \
        "guardada por dentro e não encontrada é o mesmo que não guardada"
    assert achadas[0]["path"] == resultado["previous"]


def test_voltar_devolve_o_codigo_antigo_e_o_venv(tmp_path, arvore_com_pai_fechado):
    """O botão de socorro tem que funcionar pelo mesmo caminho que a ida."""
    from project_os.core import updates

    onde = str(arvore_com_pai_fechado)
    resultado = updates.apply_tarball(
        _pacote(tmp_path, "0.4.14"), root=onde, on_line=lambda _l: None
    )
    updates.rollback(resultado["previous"], root=onde)

    assert '"0.4.6"' in _versao_instalada(onde)
    assert "0.4.6" in io.open(os.path.join(onde, "web", "index.html"), encoding="utf-8").read()
    assert os.path.isfile(os.path.join(onde, ".venv", "bin", "python3"))
    assert io.open(os.path.join(onde, "PEDIDOS.md"), encoding="utf-8").read().strip() == \
        "as anotações dele"
    assert not os.path.exists(resultado["previous"]), "a pasta guardada virou a instalação"
    assert os.listdir(str(arvore_com_pai_fechado.parent)) == ["project-os"]


def test_duas_atualizacoes_seguidas_guardam_as_duas(tmp_path, arvore_com_pai_fechado):
    from project_os.core import updates

    onde = str(arvore_com_pai_fechado)
    updates.apply_tarball(_pacote(tmp_path, "0.4.13"), root=onde, on_line=lambda _l: None)
    segundo = dict(_pacote(tmp_path, "0.4.14"), current="0.4.13")
    updates.apply_tarball(segundo, root=onde, on_line=lambda _l: None)

    assert '"0.4.14"' in _versao_instalada(onde)
    versoes = sorted(a["version"] for a in updates.previous_versions(root=onde))
    assert versoes == ["0.4.13", "0.4.6"], \
        "a segunda atualização atropelou a árvore guardada pela primeira"


def test_a_pasta_de_trabalho_nao_e_confundida_com_codigo(tmp_path):
    """Mover a pasta de trabalho no meio da troca é a atualização puxando o próprio tapete."""
    from project_os.core import updates

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.4.6")
    os.mkdir(os.path.join(onde, updates.WORK_PREFIX + "abc"))
    os.mkdir(os.path.join(onde, updates.PREVIOUS_PREFIX + "0.4.5"))
    os.mkdir(os.path.join(onde, updates.FAILED_PREFIX + "xyz"))

    assert updates._own_files(onde) == ["project_os", "web"], (
        "só o código viaja: nem o venv, nem as anotações dele, nem as pastas da "
        "própria atualização"
    )


# --------------------------------------------------------------------------- quando dá errado


def test_se_a_arvore_nova_nao_entra_a_antiga_volta(tmp_path, monkeypatch):
    """Meia troca é o pior estado possível numa caixa que ninguém pode abrir."""
    from project_os.core import updates

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.4.6")
    nova = str(tmp_path / "nova")
    _arvore(nova, "0.4.14", com_venv=False)

    real = os.rename
    contador = {"n": 0}

    def falha_na_terceira(origem, destino):
        contador["n"] += 1
        if contador["n"] == 3:
            raise OSError(28, "No space left on device")
        return real(origem, destino)

    monkeypatch.setattr(os, "rename", falha_na_terceira)
    with pytest.raises(OSError):
        updates._swap_in_place(onde, nova, "0.4.6", lambda _l: None)
    monkeypatch.undo()

    assert '"0.4.6"' in _versao_instalada(onde), "ficou meio nova, meio velha"
    assert "0.4.6" in io.open(os.path.join(onde, "web", "index.html"), encoding="utf-8").read()
    assert os.path.isfile(os.path.join(onde, ".venv", "bin", "python3"))
    assert updates.previous_versions(root=onde) == [], \
        "sobrou uma versão anterior de uma troca que não aconteceu"


def test_pacote_que_nao_e_project_os_nao_troca_nada(tmp_path, arvore_com_pai_fechado):
    from project_os.core import updates

    onde = str(arvore_com_pai_fechado)
    vazio = str(tmp_path / "vazio")
    os.makedirs(os.path.join(vazio, "outra-coisa"))
    caminho = str(tmp_path / "nada.tar.gz")
    with tarfile.open(caminho, "w:gz") as tar:
        tar.add(vazio, arcname="nada")
    soma = hashlib.sha256(io.open(caminho, "rb").read()).hexdigest()

    with pytest.raises(updates.UpdateError) as caiu:
        updates.apply_tarball(
            {"url": "file://" + caminho, "sha256": soma, "current": "0.4.6", "latest": "0.4.14"},
            root=onde, on_line=lambda _l: None,
        )

    assert caiu.value.code == "bad_archive"
    assert '"0.4.6"' in _versao_instalada(onde)
    assert updates.previous_versions(root=onde) == []


@pytest.fixture()
def arvore_toda_fechada(tmp_path):
    """Nem a pasta de cima, nem a do código. Aqui não há troca possível."""
    pai = tmp_path / "opt"
    onde = pai / "project-os"
    _arvore(str(onde), "0.4.6")
    os.chmod(str(onde), 0o555)
    os.chmod(str(pai), 0o555)
    yield onde
    os.chmod(str(pai), 0o755)
    os.chmod(str(onde), 0o755)


def test_sem_escrita_em_lugar_nenhum_recusa_com_o_caminho_do_sistema(arvore_toda_fechada):
    from project_os.core import updates

    with pytest.raises(updates.UpdateError) as caiu:
        updates.apply_tarball(
            {"url": "https://example.invalid/x.tar.gz", "sha256": "0" * 64,
             "current": "0.4.6", "latest": "0.4.14"},
            root=str(arvore_toda_fechada),
        )

    assert caiu.value.code == "root_not_writable"
    # O motivo tem que dizer o que fazer, não só que deu errado.
    assert "sistema" in (caiu.value.hint or "").lower()


def test_recusa_antes_de_baixar_o_pacote(arvore_toda_fechada, monkeypatch):
    """Meio giga de download para descobrir que não dá é pior que não tentar."""
    from project_os.core import updates

    def nao_pode_baixar(*a, **k):
        raise AssertionError("baixou antes de conferir se dava para instalar")

    monkeypatch.setattr(updates, "_download", nao_pode_baixar)
    with pytest.raises(updates.UpdateError):
        updates.apply_tarball(
            {"url": "https://example.invalid/x.tar.gz", "sha256": "0" * 64,
             "current": "0.4.6", "latest": "0.4.14"},
            root=str(arvore_toda_fechada),
        )


def test_pode_instalar_diz_nao_com_o_motivo(arvore_toda_fechada):
    """A tela precisa saber disso antes de oferecer o botão."""
    from project_os.core import updates

    pode, motivo = updates.can_apply(str(arvore_toda_fechada))
    assert pode is False
    assert "opt" in motivo


def test_pode_instalar_diz_sim_no_caso_normal(tmp_path):
    from project_os.core import updates

    onde = tmp_path / "project-os"
    (onde / "project_os").mkdir(parents=True)
    pode, motivo = updates.can_apply(str(onde))
    assert pode is True
    assert motivo == ""


# --------------------------------------------------------------------------- da tela para trás


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
    # O nome exato da tela, porque uma dica que aponta para um lugar que não
    # existe é pior do que nenhuma dica.
    assert "Atualizações" in corpo["install_hint"]
    assert "Sistema do cartão" in corpo["install_hint"]


def test_o_status_conta_por_onde_a_troca_vai_passar(auth_client, monkeypatch, tmp_path):
    """Para responder "por que atualizou agora?" sem abrir o Pi."""
    from project_os.core import updates as live

    onde = str(tmp_path / "project-os")
    _arvore(onde, "0.4.6")
    monkeypatch.setattr(live, "root_dir", lambda: onde)
    assert auth_client.get("/api/updates").json()["strategy"] == live.STRATEGY_PARENT


def test_voltar_pelo_botao_da_tela_acha_a_guardada_por_dentro(
    auth_client, monkeypatch, tmp_path, arvore_com_pai_fechado
):
    """O socorro é um POST, e ele procura no disco -- nos dois lugares.

    Guardar por dentro e continuar procurando só ao lado deixaria o botão de
    voltar invisível justamente nas caixas que mais precisam dele.
    """
    from project_os.core import updates as live

    onde = str(arvore_com_pai_fechado)
    monkeypatch.setattr(live, "root_dir", lambda: onde)
    live.apply_tarball(_pacote(tmp_path, "0.4.14"), root=onde, on_line=lambda _l: None)

    corpo = auth_client.get("/api/updates").json()
    assert corpo["previous"]["version"] == "0.4.6"
    assert corpo["strategy"] == live.STRATEGY_IN_PLACE

    resposta = auth_client.post("/api/updates/rollback", json={"restart": False})
    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["version"] == "0.4.6"
    assert '"0.4.6"' in _versao_instalada(onde)
    assert os.path.isfile(os.path.join(onde, ".venv", "bin", "python3"))


def test_a_imagem_deixa_o_grupo_escrever_no_opt():
    """A troca por fora continua sendo a boa, e nas imagens novas é a que roda.

    Não é abrir mão de nada -- o mesmo sudoers já dá ``apt-get`` sem senha a
    este usuário, e apt-get sem senha é root com passos extras. O que se ganha é
    a troca numa renomeação só, sem janela de árvore pela metade.
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
