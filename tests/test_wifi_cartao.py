"""O arquivo de Wi-Fi do cartão, lido do jeito que o Pi lê.

Numa Raspberry sem tela e sem cabo, não entrar na rede é indistinguível de não
ligar: a caixa está de pé, funcionando, e não tem como contar isso para ninguém.
O conserto seria o cartão de volta no PC -- a viagem que este projeto existe
para nunca precisar. Por isso o que se testa aqui não é o caminho feliz, é o
arquivo salvo no editor errado.

Foi assim que apareceu o defeito do BOM: alguns editores põem três bytes
invisíveis no começo do arquivo, e eles se colam na primeira chave -- "ssid"
vira "\\xef\\xbb\\xbfssid", que não casa com nada. O arquivo inteiro era
ignorado em silêncio e o Pi nunca entrava na rede.

Precisa de docker (o script mexe em /boot/firmware e chama nmcli).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.test_layout import docker_disponivel

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(
    RAIZ, "image", "stage-project-os", "00-project-os", "files",
    "usr", "local", "sbin", "project-os-firstboot",
)


def test_o_script_e_bash_valido():
    result = subprocess.run(["/bin/bash", "-n", SCRIPT], stderr=subprocess.PIPE, check=False)
    assert result.returncode == 0, result.stderr.decode()


def test_tira_o_bom_do_comeco_do_arquivo():
    """Vale sem docker: três bytes invisíveis derrubariam a rede inteira."""
    texto = open(SCRIPT, encoding="utf-8").read()
    assert "key=${key#$'\\xef\\xbb\\xbf'}" in texto


@pytest.mark.skipif(not docker_disponivel(), reason="precisa de docker")
def test_le_o_arquivo_do_cartao_em_todas_as_formas_que_ele_chega():
    """Arquivo simples, salvo no Windows, com BOM, com aspas, senha com "=".

    Confere também o que acontece quando dá errado: sem ssid não tenta nada, e
    quando a rede recusa o arquivo **fica** -- é o único jeito de ele corrigir a
    senha sem regravar o cartão. E quando conecta, o arquivo é apagado: ele
    guarda a senha do Wi-Fi em texto puro numa partição que qualquer computador
    lê.
    """
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", "%s:/repo" % RAIZ,
         "debian:bookworm-slim", "bash", "/repo/scripts/test-wifi-cartao-docker.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600, check=False,
    )
    saida = result.stdout.decode("utf-8", "replace")
    assert "TUDO OK" in saida, saida
    assert result.returncode == 0, saida
