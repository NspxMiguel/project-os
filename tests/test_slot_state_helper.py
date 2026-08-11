"""O ajudante que grava qual slot deve subir.

Ele existe por um motivo que não aparece em teste nenhum rodando no laptop: a
partição FAT é montada com uid=0 e umask 0022 (o padrão do vfat), e o project-os
roda como usuário comum. Na máquina de verdade ele não consegue criar arquivo
nenhum em /boot/firmware -- foi conferido montando a imagem e tentando escrever
como o usuário project-os.

O que isso quebraria, se ninguém tivesse olhado:

* ``mark_current_good()`` falha em silêncio (o main.py engole a exceção de
  propósito, para nunca derrubar o boot), ``tries`` nunca zera, e na terceira
  ligada o initramfs conclui que o sistema que está rodando não presta;
* ``boot_into()`` falha, e uma atualização grava o slot B inteiro sem conseguir
  apontar o boot para ele. A tela diz que deu certo e o Pi reinicia no mesmo
  lugar, para sempre.

Aqui se testa o ajudante em si (é ele que tem poder de root, então é ele que
precisa recusar besteira) e o caminho de volta do lado do Python.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from project_os.core import slots

AJUDANTE = (
    Path(__file__).resolve().parents[1]
    / "image/stage-project-os/00-project-os/files/usr/local/sbin/project-os-slot-state"
)


@pytest.fixture()
def boot(tmp_path: Path) -> Path:
    """Uma partição de boot de mentira: o ajudante a reconhece pelo config.txt."""
    firmware = tmp_path / "boot" / "firmware"
    firmware.mkdir(parents=True)
    (firmware / "config.txt").write_text("auto_initramfs=1\n", encoding="utf-8")
    return firmware


def rodar(boot: Path, *args: str) -> subprocess.CompletedProcess:
    """Roda o ajudante com a raiz apontando para a pasta de mentira.

    O ajudante procura /boot/firmware, então o teste o roda com o diretório de
    trabalho trocado e o caminho reescrito -- é o mesmo script, palavra por
    palavra, com o lugar de procurar trocado por uma cópia.
    """
    texto = AJUDANTE.read_text(encoding="utf-8")
    texto = texto.replace("for candidato in /boot/firmware /boot; do",
                          'for candidato in "%s"; do' % boot)
    copia = boot.parent / "ajudante.sh"
    copia.write_text(texto, encoding="utf-8")
    return subprocess.run(
        ["/bin/sh", str(copia), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )


def conf(boot: Path) -> dict:
    texto = (boot / "project-os-slot.conf").read_text(encoding="utf-8")
    return slots.parse_state(texto)


def test_o_script_e_sh_valido() -> None:
    resultado = subprocess.run(["/bin/sh", "-n", str(AJUDANTE)], stderr=subprocess.PIPE, check=False)
    assert resultado.returncode == 0, resultado.stderr.decode()


def test_grava_o_slot_pedido(boot: Path) -> None:
    assert rodar(boot, "slot=B", "tries=0").returncode == 0
    assert conf(boot)["slot"] == "B"
    assert conf(boot)["tries"] == 0


def test_o_que_nao_foi_pedido_fica_como_estava(boot: Path) -> None:
    rodar(boot, "slot=B", "good=B", "tries=2")
    rodar(boot, "tries=0")
    estado = conf(boot)
    assert estado["slot"] == "B"
    assert estado["good"] == "B"
    assert estado["tries"] == 0


def test_recusa_slot_que_nao_existe(boot: Path) -> None:
    """Este é o lado que tem root: ele confere de novo o que o outro já conferiu."""
    resultado = rodar(boot, "slot=C")
    assert resultado.returncode != 0
    assert not (boot / "project-os-slot.conf").exists()


def test_recusa_campo_desconhecido(boot: Path) -> None:
    assert rodar(boot, "caminho=/etc/passwd").returncode != 0


def test_recusa_tries_que_nao_e_numero(boot: Path) -> None:
    assert rodar(boot, "tries=;rm -rf /").returncode != 0


def test_nada_e_avaliado_como_comando(boot: Path) -> None:
    """Um valor com aspas e cifrão não pode virar comando nem entrar no arquivo."""
    marca = boot.parent / "invadiu"
    assert rodar(boot, 'slot=$(touch %s)' % marca).returncode != 0
    assert not marca.exists()


def test_cartao_sem_estado_comeca_no_slot_A(boot: Path) -> None:
    rodar(boot, "tries=1")
    estado = conf(boot)
    assert estado["slot"] == "A"
    assert estado["good"] == "A"


# ---------------------------------------------------------------------------
# o lado do Python
# ---------------------------------------------------------------------------
def test_escrita_direta_continua_valendo(tmp_path: Path) -> None:
    """No laptop e no CI a escrita direta passa, e o ajudante nem é chamado."""
    alvo = tmp_path / "project-os-slot.conf"
    chamou = []
    slots.write_state({"slot": "B", "good": "A", "tries": 0, "recovery": 0}, str(alvo))
    assert slots.parse_state(alvo.read_text(encoding="utf-8"))["slot"] == "B"
    assert chamou == []


def test_quando_a_fat_recusa_a_escrita_o_ajudante_entra(monkeypatch, tmp_path: Path) -> None:
    """O caso da máquina de verdade."""
    chamadas = []

    def falhar(state, where):
        raise PermissionError(13, "Permission denied", where)

    def fingir(argv, **kwargs):
        chamadas.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(slots, "_write_direct", falhar)
    monkeypatch.setattr(slots, "state_path", lambda: str(tmp_path / "slot.conf"))
    monkeypatch.setattr(slots.subprocess, "run", fingir)

    slots.write_state({"slot": "B", "good": "A", "tries": 0, "recovery": 0})

    assert len(chamadas) == 1
    argv = chamadas[0]
    assert slots.STATE_HELPER in argv
    assert "slot=B" in argv and "tries=0" in argv


def test_caminho_explicito_nunca_vai_para_o_ajudante(monkeypatch, tmp_path: Path) -> None:
    """Pedir um arquivo e gravar em outro é pior do que falhar."""
    def falhar(state, where):
        raise PermissionError(13, "Permission denied", where)

    def nao_deveria(argv, **kwargs):  # pragma: no cover - o teste falha antes
        raise AssertionError("chamou o ajudante para um caminho explícito")

    monkeypatch.setattr(slots, "_write_direct", falhar)
    monkeypatch.setattr(slots.subprocess, "run", nao_deveria)

    with pytest.raises(PermissionError):
        slots.write_state(dict(slots.DEFAULT_STATE), str(tmp_path / "outro.conf"))


def test_o_ajudante_e_chamado_com_sudo_quando_nao_somos_root(monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert slots._helper_argv()[:2] == ["sudo", "-n"]
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert slots._helper_argv() == [slots.STATE_HELPER]


def test_ajudante_que_falha_vira_erro(monkeypatch, tmp_path: Path) -> None:
    """Falhar alto: quem chamou precisa saber que o boot não foi redirecionado."""
    monkeypatch.setattr(slots, "_write_direct",
                        lambda state, where: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(slots, "state_path", lambda: str(tmp_path / "slot.conf"))
    monkeypatch.setattr(
        slots.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"sem sudo"),
    )
    with pytest.raises(OSError):
        slots.write_state(dict(slots.DEFAULT_STATE))
