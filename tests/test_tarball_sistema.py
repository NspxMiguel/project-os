"""O tarball de sistema, desempacotado num slot de verdade.

Quando ele aperta Atualizar, é o tarball publicado pelo CI que vira o sistema do
slot reserva. Se ele tiver a forma errada -- uma pasta a mais em volta, faltando
/etc, sem o virtualenv -- o slot novo não sobe. O esquema volta atrás sozinho, e
o Pi sobrevive; mas nenhuma atualização pela rede funcionaria nunca, e a saída
voltaria a ser o cartão no PC.

O arquivo tem 840 MB, então este teste não baixa nada: ele roda se o tarball já
estiver na máquina, e é pulado se não estiver. Para rodar:

    gh release download v0.4.5 -R NspxMiguel/project-os \\
        -p 'project-os-rootfs-*.tar.gz' -D /tmp
    PROJECT_OS_ROOTFS=/tmp/project-os-rootfs-0.4.5.tar.gz pytest tests/test_tarball_sistema.py
"""

from __future__ import annotations

import glob
import os
import subprocess

import pytest

from tests.test_layout import docker_disponivel

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def achar_tarball() -> str:
    """O tarball apontado pelo ambiente, ou qualquer um já baixado."""
    escolhido = os.environ.get("PROJECT_OS_ROOTFS", "")
    if escolhido and os.path.isfile(escolhido):
        return escolhido
    for lugar in ("/tmp", os.environ.get("TMPDIR", "/tmp"), RAIZ):
        achados = sorted(glob.glob(os.path.join(lugar, "project-os-rootfs-*.tar.gz")))
        if achados:
            return achados[-1]
    return ""


TARBALL = achar_tarball()


@pytest.mark.skipif(not TARBALL, reason="o tarball de sistema não está baixado nesta máquina")
@pytest.mark.skipif(not docker_disponivel(), reason="precisa de docker para loop devices")
def test_o_tarball_publicado_vira_um_sistema_que_sobe():
    """Desempacota com o ajudante de verdade e confere o que ficou no slot."""
    pasta, nome = os.path.split(os.path.abspath(TARBALL))
    result = subprocess.run(
        ["docker", "run", "--rm", "--privileged",
         "-v", "%s:/repo" % RAIZ, "-v", "%s:/s" % pasta,
         "debian:bookworm-slim", "bash",
         "/repo/scripts/test-tarball-sistema-docker.sh", "/s/%s" % nome],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=2400, check=False,
    )
    saida = result.stdout.decode("utf-8", "replace")
    assert "TUDO OK" in saida, saida
    assert result.returncode == 0, saida
