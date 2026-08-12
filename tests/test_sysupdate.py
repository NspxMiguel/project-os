"""Atualização do sistema inteiro pela rede.

O que precisa ser impossível, e por isso tem teste: instalar um tarball que não
bate com o sha256 anunciado, e escrever no slot que está rodando. O primeiro é
como se entrega código de outra pessoa para um Pi; o segundo é como se apaga o
sistema que está atendendo a requisição.
"""

from __future__ import annotations

import hashlib
import os
import tarfile

import pytest

from project_os.core import slots, sysupdate


@pytest.fixture()
def tarball(tmp_path):
    caminho = tmp_path / "rootfs.tar.gz"
    dentro = tmp_path / "conteudo"
    dentro.mkdir()
    (dentro / "prova.txt").write_text("um sistema", encoding="utf-8")
    with tarfile.open(caminho, "w:gz") as tar:
        tar.add(str(dentro / "prova.txt"), arcname="prova.txt")
    return caminho


def servir(monkeypatch, dados, content_length=True):
    """Faz o urlopen do módulo devolver estes bytes."""
    import io

    class Resposta(io.BytesIO):
        headers = {"Content-Length": str(len(dados))} if content_length else {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Resposta(dados))


# --- o download confere antes de qualquer coisa ------------------------------
def test_o_download_confere_e_grava(tmp_path, monkeypatch):
    dados = b"um sistema inteiro" * 1000
    sha = hashlib.sha256(dados).hexdigest()
    servir(monkeypatch, dados)
    destino = str(tmp_path / "rootfs.tar.gz")
    assert sysupdate.download("http://exemplo/rootfs.tar.gz", sha, destino) == destino
    assert open(destino, "rb").read() == dados


def test_um_download_que_nao_bate_no_sha_e_apagado(tmp_path, monkeypatch):
    """Sem isso, toda atualização é um convite a quem responder pelo servidor."""
    servir(monkeypatch, b"outra coisa")
    destino = str(tmp_path / "rootfs.tar.gz")
    with pytest.raises(sysupdate.SystemUpdateError) as erro:
        sysupdate.download("http://exemplo/rootfs.tar.gz", "0" * 64, destino)
    assert erro.value.code == "checksum_mismatch"
    assert not os.path.exists(destino)
    assert not os.path.exists(destino + ".parcial")


def test_sem_sha_no_manifesto_nao_instala_nada(tmp_path):
    with pytest.raises(sysupdate.SystemUpdateError) as erro:
        sysupdate.download("http://exemplo/x.tar.gz", "", str(tmp_path / "x"))
    assert erro.value.code == "unverifiable"


def test_um_download_gigante_e_cortado(tmp_path, monkeypatch):
    monkeypatch.setattr(sysupdate, "MAX_BYTES", 1024)
    servir(monkeypatch, b"x" * 5000)
    with pytest.raises(sysupdate.SystemUpdateError) as erro:
        sysupdate.download("http://exemplo/x.tar.gz", "0" * 64, str(tmp_path / "x"))
    assert erro.value.code == "too_large"


# --- a trava que protege o sistema que está no ar ----------------------------
def test_recusa_escrever_no_slot_que_esta_rodando(monkeypatch, tarball):
    monkeypatch.setattr(slots, "current_slot", lambda: "A")
    with pytest.raises(sysupdate.SystemUpdateError) as erro:
        sysupdate.write_slot(str(tarball), "A")
    assert erro.value.code == "refuse_current"


def test_recusa_um_slot_que_nao_existe(tarball):
    with pytest.raises(sysupdate.SystemUpdateError) as erro:
        sysupdate.write_slot(str(tarball), "C")
    assert erro.value.code == "bad_slot"


def test_o_ajudante_e_chamado_com_o_slot_certo(monkeypatch, tarball):
    monkeypatch.setattr(slots, "current_slot", lambda: "A")
    monkeypatch.setattr(sysupdate.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(sysupdate.shutil, "which", lambda nome: "/usr/bin/" + nome)
    visto = {}

    class Processo(object):
        stdout = iter(["formatando", "pronto"])

        def wait(self):
            return 0

    def popen(comando, **kwargs):
        visto["comando"] = comando
        return Processo()

    monkeypatch.setattr(sysupdate.subprocess, "Popen", popen)
    sysupdate.write_slot(str(tarball), "B")
    assert visto["comando"] == ["sudo", "-n", sysupdate.HELPER, str(tarball), "B"]


# --- o manifesto -------------------------------------------------------------
def test_um_manifesto_so_de_app_nao_e_erro(monkeypatch):
    """O normal por enquanto: versão nova do app, sistema igual."""
    monkeypatch.setattr(sysupdate.updates, "_fetch_json", lambda url, **k: {"version": "0.4.0"})
    info = sysupdate.check("http://exemplo/latest.json")
    assert info["available"] is False
    assert info["update_available"] is False


def test_um_sistema_anunciado_sem_sha_e_recusado(monkeypatch):
    monkeypatch.setattr(
        sysupdate.updates, "_fetch_json",
        lambda url, **k: {"system": {"version": "0.4.0", "url": "http://x/rootfs.tar.gz"}},
    )
    with pytest.raises(sysupdate.SystemUpdateError) as erro:
        sysupdate.check("http://exemplo/latest.json")
    assert erro.value.code == "unverifiable"


def test_um_sistema_mais_novo_e_oferecido(monkeypatch):
    monkeypatch.setattr(
        sysupdate.updates, "_fetch_json",
        lambda url, **k: {"system": {
            "version": "99.0.0", "url": "http://x/rootfs.tar.gz", "sha256": "ab" * 32,
        }},
    )
    info = sysupdate.check("http://exemplo/latest.json")
    assert info["available"] is True and info["update_available"] is True


# --- o que a tela pergunta antes de oferecer o botão -------------------------
def test_um_cartao_de_um_sistema_so_diz_isso_com_todas_as_letras(monkeypatch):
    monkeypatch.setattr(slots, "available", lambda: False)
    pronto = sysupdate.available()
    assert pronto["ok"] is False and pronto["code"] == "no_slots"
    assert "uma vez" in pronto["reason"]


def test_sem_o_ajudante_root_tambem_nao_da(monkeypatch):
    monkeypatch.setattr(slots, "available", lambda: True)
    monkeypatch.setattr(sysupdate, "helper_available", lambda: False)
    assert sysupdate.available()["code"] == "no_helper"


def test_com_slots_e_ajudante_da(monkeypatch):
    monkeypatch.setattr(slots, "available", lambda: True)
    monkeypatch.setattr(sysupdate, "helper_available", lambda: True)
    assert sysupdate.available()["ok"] is True


# --- o caminho inteiro, sem tocar em disco -----------------------------------
def test_instalar_grava_no_outro_slot_e_aponta_o_boot(monkeypatch, tmp_path):
    conf = str(tmp_path / "slot.conf")
    slots.write_state({"slot": "A", "good": "A", "tries": 0, "recovery": 0}, conf)

    dados = b"rootfs de mentira"
    sha = hashlib.sha256(dados).hexdigest()
    servir(monkeypatch, dados)
    monkeypatch.setattr(slots, "available", lambda: True)
    monkeypatch.setattr(slots, "current_slot", lambda: "A")
    monkeypatch.setattr(slots, "state_path", lambda: conf)
    monkeypatch.setattr(sysupdate, "helper_available", lambda: True)
    monkeypatch.setattr(
        sysupdate.updates, "_fetch_json",
        lambda url, **k: {"system": {
            "version": "99.0.0", "url": "http://x/rootfs.tar.gz", "sha256": sha,
        }},
    )
    gravado = {}
    monkeypatch.setattr(
        sysupdate, "write_slot",
        lambda tarball, slot, on_line=None: gravado.update(slot=slot, tarball=tarball),
    )
    reiniciou = []
    monkeypatch.setattr(sysupdate.subprocess, "Popen", lambda cmd, **k: reiniciou.append(cmd))

    resultado = sysupdate.install(
        manifest_url="http://exemplo/latest.json",
        download_dir=str(tmp_path / "downloads"),
    )

    assert resultado["slot"] == "B"
    assert gravado["slot"] == "B"
    estado = slots.read_state(conf)
    assert estado["slot"] == "B"
    assert estado["good"] == "A", "o caminho de volta não pode ser mexido pela instalação"
    assert estado["tries"] == 0
    assert reiniciou and "reboot" in reiniciou[0]


def test_instalar_a_mesma_versao_nao_faz_nada(monkeypatch, tmp_path):
    monkeypatch.setattr(slots, "available", lambda: True)
    monkeypatch.setattr(sysupdate, "helper_available", lambda: True)
    from project_os import __version__

    monkeypatch.setattr(
        sysupdate.updates, "_fetch_json",
        lambda url, **k: {"system": {
            "version": __version__, "url": "http://x/r.tar.gz", "sha256": "ab" * 32,
        }},
    )
    with pytest.raises(sysupdate.SystemUpdateError) as erro:
        sysupdate.install(manifest_url="http://exemplo/latest.json",
                          download_dir=str(tmp_path))
    assert erro.value.code == "already_current"


def test_num_cartao_antigo_a_instalacao_e_recusada_antes_de_baixar(monkeypatch, tmp_path):
    monkeypatch.setattr(slots, "available", lambda: False)
    baixou = []
    monkeypatch.setattr(sysupdate, "download", lambda *a, **k: baixou.append(a))
    with pytest.raises(sysupdate.SystemUpdateError) as erro:
        sysupdate.install(download_dir=str(tmp_path))
    assert erro.value.code == "no_slots"
    assert not baixou, "não faz sentido baixar 600 MB para descobrir que não dá"


def test_sem_espaco_a_instalacao_e_recusada_antes_de_baixar(monkeypatch, tmp_path):
    """Meio giga só cabe onde cabe, e o manifesto diz o tamanho antes.

    Sem esta conferência a descoberta vinha no fim: disco cheio no meio da
    escrita, com a partição de dados dele lotada de sobra.
    """
    import collections

    conf = str(tmp_path / "slot.conf")
    slots.write_state({"slot": "A", "good": "A", "tries": 0, "recovery": 0}, conf)
    monkeypatch.setattr(slots, "available", lambda: True)
    monkeypatch.setattr(slots, "current_slot", lambda: "A")
    monkeypatch.setattr(slots, "state_path", lambda: conf)
    monkeypatch.setattr(sysupdate, "helper_available", lambda: True)
    monkeypatch.setattr(
        sysupdate.updates, "_fetch_json",
        lambda url, **k: {"system": {
            "version": "99.0.0", "url": "http://x/rootfs.tar.gz",
            "sha256": "a" * 64, "size": 900 * 1024 * 1024,
        }},
    )
    Uso = collections.namedtuple("Uso", "total used free")
    monkeypatch.setattr(sysupdate.shutil, "disk_usage", lambda caminho: Uso(1 << 30, 1 << 30, 100 * 1024 * 1024))

    baixou = []
    monkeypatch.setattr(sysupdate, "download", lambda *a, **k: baixou.append(a))

    with pytest.raises(sysupdate.SystemUpdateError) as erro:
        sysupdate.install(download_dir=str(tmp_path / "downloads"))

    assert erro.value.code == "no_space"
    assert not baixou, "começou a baixar sabendo que não cabia"
    assert "MB" in erro.value.message or "GB" in erro.value.message, erro.value.message


# --- a API -------------------------------------------------------------------
# O conftest recarrega o pacote entre os testes, então existem dois objetos-módulo
# de core.slots vivos: o que este arquivo importou e o que a API guardou. Patch
# aqui é feito pelo caminho da API, que é quem vai chamar.
@pytest.fixture()
def api_slots():
    import project_os.api.updates as api

    return api.slots


@pytest.fixture()
def api_sysupdate():
    import project_os.api.updates as api

    return api.sysupdate

def test_a_tela_recebe_o_estado_dos_dois_sistemas(auth_client, monkeypatch):
    resposta = auth_client.get("/api/updates/system")
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert "slots" in corpo and "capability" in corpo


def test_instalar_sistema_num_cartao_antigo_diz_por_que_nao(auth_client, monkeypatch, api_slots):
    """Sem os dois sistemas não há para onde escrever, e a tela precisa dizer isso."""
    monkeypatch.setattr(api_slots, "available", lambda: False)
    resposta = auth_client.post("/api/updates/system/install", json={})
    assert resposta.status_code == 409, resposta.text
    assert resposta.json()["error"] == "no_slots"


def test_voltar_para_o_outro_sistema_num_cartao_antigo_e_recusado(auth_client, monkeypatch, api_slots):
    monkeypatch.setattr(api_slots, "available", lambda: False)
    resposta = auth_client.post("/api/updates/system/rollback")
    assert resposta.status_code == 409
    assert resposta.json()["error"] == "no_slots"


def test_voltar_aponta_o_boot_para_o_outro_slot(auth_client, monkeypatch, tmp_path, api_slots):
    conf = str(tmp_path / "slot.conf")
    api_slots.write_state({"slot": "B", "good": "A", "tries": 0, "recovery": 0}, conf)
    monkeypatch.setattr(api_slots, "available", lambda: True)
    monkeypatch.setattr(api_slots, "current_slot", lambda: "B")
    monkeypatch.setattr(api_slots, "state_path", lambda: conf)
    import subprocess as sp

    chamados = []

    class Reiniciando(object):
        """Um Popen de mentira que também serve de gerenciador de contexto.

        subprocess.run() usa Popen com `with`, e o findmnt que descobre o slot
        passa por ali -- um duble que só anota a chamada quebra o resto do
        módulo em vez de testar o que interessa.
        """

        def __init__(self, cmd, **kwargs):
            chamados.append(cmd)
            self.args = cmd
            self.returncode = 0
            self.stdout = b""
            self.stderr = b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, *args, **kwargs):
            return (b"", b"")

        def wait(self, *args, **kwargs):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(sp, "Popen", Reiniciando)

    resposta = auth_client.post("/api/updates/system/rollback")
    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["slot"] == "A"
    assert api_slots.read_state(conf)["slot"] == "A"
    assert chamados and "reboot" in chamados[0]
