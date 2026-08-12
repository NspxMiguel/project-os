"""A vida inteira do cartão, em sequência, num disco de verdade.

Todo o resto do projeto testa uma peça de cada vez. Este testa a **sequência**:
gravação, primeiro boot, clone para o slot reserva, atualização, boot no slot
novo, e a volta atrás quando ele não confirma. É onde mora o defeito que sobra
depois de todos os outros -- a peça A passa, a peça B passa, e a saída de A não
é bem o que B esperava.

Foi assim que apareceu o defeito do clone: o rsync de um sistema que está
rodando sai com código 24 ("sumiu arquivo durante a cópia") como coisa normal,
e o script tratava qualquer código diferente de zero como falha. Ele dizia "o
slot segue vazio" -- mentira, ficava pela metade -- saía com sucesso, e no boot
seguinte a checagem "tem /usr e /etc?" achava os dois e marcava o slot como
pronto para sempre. A caixa passava a contar com um caminho de volta que não
sobe. Agora quem responde essa pergunta é um carimbo escrito no fim da cópia.

Precisa de Linux com loop devices, o que no Mac significa docker. Sem docker o
teste é pulado -- mas ele roda no CI, que é Linux.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.test_layout import docker_disponivel

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLONE = os.path.join(
    RAIZ, "image", "stage-project-os", "00-project-os", "files",
    "usr", "local", "sbin", "project-os-clone-slot",
)


def test_o_script_de_clone_e_sh_valido():
    result = subprocess.run(["/bin/sh", "-n", CLONE], stderr=subprocess.PIPE, check=False)
    assert result.returncode == 0, result.stderr.decode()


def test_o_clone_aceita_o_codigo_24_do_rsync():
    """Vale sem docker, e é o caso comum: num Pi ligado arquivo some o tempo todo.

    Log rotacionado, temporário de serviço -- o rsync chama isso de 24 e segue
    tendo copiado tudo que importa. Tratar como falha abortaria a cópia de um
    sistema perfeitamente bom, quase toda vez.
    """
    texto = open(CLONE, encoding="utf-8").read()
    assert '[ "$CODIGO" -ne 24 ]' in texto


def test_o_clone_decide_pelo_carimbo_e_nao_por_usr_e_etc():
    """Uma cópia interrompida tem /usr e /etc; um sistema inteiro tem carimbo."""
    texto = open(CLONE, encoding="utf-8").read()
    assert 'if [ -f "$MONTAGEM/$COMPLETO" ]; then' in texto
    assert '[ -d "$MONTAGEM/usr" ]' not in texto


@pytest.mark.skipif(not docker_disponivel(), reason="precisa de docker para loop devices")
def test_o_cartao_vive_o_ciclo_inteiro():
    """Grava, sobe, clona, atualiza, reinicia no slot novo e volta atrás.

    Confere também o que só aparece na sequência: o fstab de cada slot apontando
    para o próprio slot, a identidade da caixa (Wi-Fi, chaves de SSH, a senha
    que ele criou) atravessando a atualização, o slot que está rodando sendo
    recusado para formatação, e um slot pela metade não passando por pronto.
    """
    result = subprocess.run(
        ["docker", "run", "--rm", "--privileged", "-v", "%s:/repo" % RAIZ,
         "debian:bookworm-slim", "bash", "/repo/scripts/test-ciclo-completo-docker.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=2400, check=False,
    )
    saida = result.stdout.decode("utf-8", "replace")
    assert "TUDO OK" in saida, saida
    assert result.returncode == 0, saida
