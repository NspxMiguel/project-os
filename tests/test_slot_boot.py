"""A escolha do slot, rodada como o boot roda: em processo filho.

Este é o teste mais importante do esquema de dois sistemas, e ele existe por
causa de um detalhe que não aparece lendo o script.

Os scripts de local-top não são carregados dentro do shell do init. O arquivo
ORDER, que o initramfs-tools gera, chama cada um como processo filho e depois
faz source de /conf/param.conf:

    /scripts/local-top/project-os-slot "$@"
    [ -e /conf/param.conf ] && . /conf/param.conf

Ou seja: "export ROOT" no filho não chega em ninguém. O init continuaria
montando a partição que o cmdline.txt manda -- a p2, sempre. E o sintoma é
invisível: o Pi liga, tudo funciona, e a troca de sistema simplesmente nunca
acontece. Toda atualização gravaria o slot B e todo boot subiria o slot A.

Então o teste executa o script como filho, com blkid e mount falsos, e confere o
que sobrou em /conf/param.conf -- que é o que o init vai ler de verdade.

Precisa de Linux, o que no Mac significa docker. Sem docker o teste é pulado --
mas ele roda no CI, que é Linux.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.test_layout import docker_disponivel

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECISOR = os.path.join(
    RAIZ, "image", "stage-project-os", "00-project-os", "files",
    "etc", "initramfs-tools", "scripts", "local-top", "project-os-slot",
)


def test_o_script_e_sh_valido():
    """Vale sem docker: um erro de sintaxe aqui e o Pi não escolhe slot nenhum."""
    result = subprocess.run(["/bin/sh", "-n", DECISOR], stderr=subprocess.PIPE, check=False)
    assert result.returncode == 0, result.stderr.decode()


def test_entrega_a_raiz_pelo_param_conf():
    """Sem docker também: o canal certo tem que estar escrito no script.

    Um "export ROOT" sozinho é o defeito silencioso descrito no topo deste
    arquivo, e ele passa despercebido em qualquer leitura rápida.
    """
    texto = open(DECISOR, encoding="utf-8").read()
    assert 'echo "ROOT=$ROOT" >> /conf/param.conf' in texto


@pytest.mark.skipif(not docker_disponivel(), reason="precisa de docker")
def test_a_escolha_do_slot_chega_no_init():
    """Roda o script como o ORDER roda e confere o ROOT que o init veria.

    Cobre: slot A, slot B depois de uma atualização, a tentativa contada antes
    do boot, a desistência automática depois de três falhas, cartão sem estado
    nenhum, e o param.conf como canal.
    """
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", "%s:/repo" % RAIZ,
         "debian:bookworm-slim", "bash", "/repo/scripts/test-slot-boot-docker.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600, check=False,
    )
    saida = result.stdout.decode("utf-8", "replace")
    assert "TUDO OK" in saida, saida
    assert result.returncode == 0, saida
