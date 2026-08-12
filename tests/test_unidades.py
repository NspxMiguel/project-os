"""O sudoers e os arquivos de serviço, conferidos pelas ferramentas do sistema.

Os dois quebram de um jeito que não aparece em teste de Python nem na build: o
arquivo é escrito, a imagem sai, e o defeito só existe quando o Pi liga.

Um sudoers inválido não desabilita uma linha -- derruba o **sudo inteiro**. Numa
caixa em que o modo Advanced é "um linux normal", e em que a senha do SSH e a
troca de slot passam por ajudantes com sudo, isso é a caixa perder a capacidade
de se consertar. E basta uma vírgula fora do lugar.

Um arquivo de serviço com diretiva errada faz o systemd recusar a unidade, e sem
tela isso é indistinguível de não ter ligado.

O bloco do sudoers é lido do próprio 01-run.sh, não de uma cópia: uma cópia
envelheceria em silêncio, que é justamente o modo de falhar que isto impede.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.test_layout import docker_disponivel

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.skipif(not docker_disponivel(), reason="precisa de docker (visudo e systemd-analyze)")
def test_o_sudoers_e_as_unidades_sao_validos():
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", "%s:/repo" % RAIZ,
         "debian:bookworm-slim", "bash", "/repo/scripts/test-unidades-docker.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900, check=False,
    )
    saida = result.stdout.decode("utf-8", "replace")
    assert "TUDO OK" in saida, saida
    assert result.returncode == 0, saida
