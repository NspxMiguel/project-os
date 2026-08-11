"""O fstab que o slot novo recebe.

Escrever o sistema no slot B e deixar o fstab dizendo que a raiz é a p2 é um
erro silencioso: o sistema sobe, responde, e monta a raiz do outro slot em cima
de si mesmo. Apagar a linha é pior ainda -- o cmdline.txt não tem "rw", então
quem remonta a raiz para escrita é o systemd-remount-fs lendo essa linha, e sem
ela o slot novo fica somente leitura para sempre.

Nada disso aparece na build nem no teste do reparticionamento. Aparece no boot
depois da primeira atualização, que é o boot em que ele não pode ter que pegar o
cartão de novo. Então o programa awk que faz a reescrita é testado aqui -- o
mesmo texto que está dentro do script, extraído dele, não uma cópia.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

AJUDANTE = (
    Path(__file__).resolve().parents[1]
    / "image/stage-project-os/00-project-os/files/usr/share/project-os/fstab-slot.sh"
)

RAIZ = "PARTUUID=5671f673-03  /  ext4  defaults,noatime  0  1"
BOOT = "PARTUUID=5671f673-01  /boot/firmware  vfat  defaults  0  2"


def programa_awk() -> str:
    """O programa awk exatamente como ele é enviado no cartão."""
    texto = AJUDANTE.read_text(encoding="utf-8")
    achado = re.search(
        r"awk -v raiz=\"\$LINHA_RAIZ\" -v boot=\"\$LINHA_BOOT\" '(.*?)'\s*\"\$FSTAB\"",
        texto,
        re.S,
    )
    assert achado, "não achei o programa awk dentro do ajudante"
    return achado.group(1)


def reescrever(fstab: str, tmp_path: Path, raiz: str = RAIZ, boot: str = BOOT) -> list[str]:
    entrada = tmp_path / "fstab"
    entrada.write_text(fstab, encoding="utf-8")
    awk = shutil.which("gawk") or shutil.which("awk")
    if awk is None:
        pytest.skip("sem awk nesta máquina")
    saida = subprocess.run(
        [awk, "-v", f"raiz={raiz}", "-v", f"boot={boot}", programa_awk(), str(entrada)],
        capture_output=True,
        text=True,
        check=True,
    )
    return saida.stdout.splitlines()


FSTAB_DE_FABRICA = """proc            /proc           proc    defaults          0       0
PARTUUID=5671f673-01  /boot/firmware  vfat    defaults          0       2
PARTUUID=5671f673-02  /               ext4    defaults,noatime  0       1
LABEL=pos-data  /var/lib/project-os  ext4  defaults,noatime,nofail  0  2
"""


def test_a_raiz_passa_a_apontar_para_o_slot_novo(tmp_path: Path) -> None:
    linhas = reescrever(FSTAB_DE_FABRICA, tmp_path)
    assert RAIZ in linhas
    assert not any("5671f673-02" in linha for linha in linhas)


def test_a_linha_da_raiz_nao_some(tmp_path: Path) -> None:
    """Sem ela o sistema sobe somente leitura -- liga, responde, não grava."""
    linhas = reescrever(FSTAB_DE_FABRICA, tmp_path)
    raizes = [linha for linha in linhas if not linha.startswith("#") and linha.split()[1:2] == ["/"]]
    assert len(raizes) == 1


def test_a_particao_de_dados_continua(tmp_path: Path) -> None:
    """É onde estão as coisas dele; perder esta linha órfã os dados."""
    linhas = reescrever(FSTAB_DE_FABRICA, tmp_path)
    assert any("pos-data" in linha for linha in linhas)


def test_o_resto_do_fstab_fica_intacto(tmp_path: Path) -> None:
    linhas = reescrever(FSTAB_DE_FABRICA, tmp_path)
    assert any(linha.startswith("proc") for linha in linhas)


def test_o_boot_aponta_para_a_p1_do_cartao_de_agora(tmp_path: Path) -> None:
    """Um tarball vindo de outro cartão traz o PARTUUID de lá."""
    de_outro_cartao = FSTAB_DE_FABRICA.replace("5671f673", "aaaabbbb")
    linhas = reescrever(de_outro_cartao, tmp_path)
    assert BOOT in linhas
    assert not any("aaaabbbb" in linha for linha in linhas)


def test_fstab_sem_raiz_ganha_uma(tmp_path: Path) -> None:
    linhas = reescrever("proc  /proc  proc  defaults  0  0\n", tmp_path)
    assert RAIZ in linhas
    assert BOOT in linhas


def test_comentarios_sao_preservados(tmp_path: Path) -> None:
    com_comentario = "# esse arquivo é lido no boot\n" + FSTAB_DE_FABRICA
    linhas = reescrever(com_comentario, tmp_path)
    assert linhas[0] == "# esse arquivo é lido no boot"


def test_linha_de_raiz_comentada_nao_conta(tmp_path: Path) -> None:
    """Um "#" na frente não é a raiz: a linha de verdade ainda tem que entrar."""
    comentada = "# PARTUUID=xxxx-02  /  ext4  defaults  0  1\nproc  /proc  proc  defaults  0  0\n"
    linhas = reescrever(comentada, tmp_path)
    assert linhas[0].startswith("#")
    assert RAIZ in linhas


def test_sem_partuuid_de_boot_a_linha_de_boot_nao_e_inventada(tmp_path: Path) -> None:
    """Se não deu para descobrir o PARTUUID do boot, melhor não escrever nada."""
    linhas = reescrever(FSTAB_DE_FABRICA, tmp_path, boot="")
    assert not any("/boot/firmware" in linha for linha in linhas)
    assert RAIZ in linhas
