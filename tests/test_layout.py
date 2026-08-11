"""O reparticionamento do cartão, rodado de verdade contra um cartão de mentira.

Este é o único script do projeto que reescreve tabela de partição. Um teste que
só lê o texto dele não prova nada: o que precisa ser provado é que a partição de
boot continua intacta e que os dados da raiz sobrevivem ao resize. Então o teste
constrói um disco igual ao que o pi-gen gera, com 64 MB de dados aleatórios
dentro, roda o script e confere o sha256 depois.

Precisa de Linux com loop devices, o que no Mac significa docker. Sem docker o
teste é pulado -- mas ele roda no CI, que é Linux.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(RAIZ, "scripts", "test-layout-docker.sh")
LAYOUT = os.path.join(
    RAIZ, "image", "stage-project-os", "00-project-os", "files",
    "usr", "share", "project-os", "layout.sh",
)


def docker_disponivel():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(
            ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, check=False,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def test_o_script_e_sh_valido():
    """Vale sem docker: um erro de sintaxe aqui trava o primeiro boot."""
    result = subprocess.run(["/bin/sh", "-n", LAYOUT], stderr=subprocess.PIPE, check=False)
    assert result.returncode == 0, result.stderr.decode()


def test_pede_o_disco_e_recusa_o_que_nao_e_disco(tmp_path):
    result = subprocess.run(["/bin/sh", LAYOUT], stderr=subprocess.PIPE, check=False)
    assert result.returncode == 2
    result = subprocess.run(
        ["/bin/sh", LAYOUT, str(tmp_path / "nada")], stderr=subprocess.PIPE, check=False
    )
    assert result.returncode == 2


@pytest.mark.skipif(not docker_disponivel(), reason="precisa de docker para loop devices")
def test_reparticiona_um_cartao_de_verdade_sem_perder_nada():
    """O teste que autoriza este script a chegar perto do cartão dele.

    Confere, num disco de 62 GB montado igual ao que sai do pi-gen:
    a partição de boot intacta, o cmdline.txt no lugar, 64 MB de dados da raiz
    idênticos depois do resize, os dois slots e a partição de dados formatados,
    alinhamento de 4 MiB, idempotência, e recusa em cartão pequeno ou em disco
    que não é nosso.
    """
    result = subprocess.run(
        ["docker", "run", "--rm", "--privileged", "-v", "%s:/repo" % RAIZ,
         "debian:bookworm-slim", "bash", "/repo/scripts/test-layout-docker.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900, check=False,
    )
    saida = result.stdout.decode("utf-8", "replace")
    assert "TUDO OK" in saida, saida
    assert result.returncode == 0, saida
