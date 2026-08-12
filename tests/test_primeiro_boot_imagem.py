"""O software do cartão, ligado de verdade, criando a primeira conta.

Todo o resto da suíte roda o código deste repositório. Este roda o que está
**dentro do .img**: a raiz é extraída da imagem, virada em contêiner armv7 (por
emulação) e o serviço sobe pelo mesmo ``/opt/project-os/bin/project-os`` que a
unidade do systemd chama, com o mesmo usuário sem privilégio.

O que só isso prova: que o virtualenv gravado na imagem importa tudo o que o app
precisa naquela arquitetura, e que a tela de criar conta -- a primeira coisa que
ele faz com a caixa -- responde 201 e devolve sessão. Se isso falhasse na caixa
dele não haveria conserto pela rede: o SSH sai trancado justamente até essa
conta existir, e a única saída seria o cartão no PC de novo.

Precisa da imagem em mãos e de docker com binfmt para armv7, então só roda
quando alguém aponta o caminho:

    PROJECT_OS_IMAGEM=~/Downloads/project-os-0.4.8.img.xz pytest tests/test_primeiro_boot_imagem.py
"""

from __future__ import annotations

import os
import subprocess

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGEM = os.environ.get("PROJECT_OS_IMAGEM", "")


def docker_disponivel() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not IMAGEM, reason="aponte PROJECT_OS_IMAGEM para um .img.xz")
@pytest.mark.skipif(not os.path.isfile(IMAGEM), reason="a imagem apontada não existe")
@pytest.mark.skipif(not docker_disponivel(), reason="precisa de docker")
def test_a_imagem_sobe_e_a_primeira_tela_funciona():
    resultado = subprocess.run(
        ["bash", os.path.join(RAIZ, "scripts", "test-primeiro-boot-docker.sh"), IMAGEM],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=3600, check=False,
    )
    saida = resultado.stdout.decode("utf-8", "replace")
    assert "tudo certo" in saida, saida
