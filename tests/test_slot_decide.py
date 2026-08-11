"""O decisor de slot do initramfs, rodado de verdade com /bin/sh.

Este script decide, antes de existir sistema nenhum, qual dos dois sobe. Se ele
errar, o Pi não sobe -- e a única saída passa a ser o cartão no PC, que é
exatamente o que o esquema existe para acabar. Então cada caminho dele tem um
caso aqui, incluindo os arquivos estragados.
"""

from __future__ import annotations

import os
import subprocess

import pytest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "image", "stage-project-os", "00-project-os", "files",
    "usr", "share", "project-os", "slot-decide.sh",
)


def decidir(tmp_path, texto):
    """Roda o script como o initramfs roda e devolve (slot, tries)."""
    conf = tmp_path / "project-os-slot.conf"
    conf.write_text(texto, encoding="utf-8")
    result = subprocess.run(
        ["/bin/sh", SCRIPT, str(conf)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    saida = result.stdout.decode().strip().split()
    assert len(saida) == 2, saida
    return saida[0], int(saida[1])


def test_o_script_existe_e_e_executavel():
    assert os.path.isfile(SCRIPT)


def test_boot_normal_sobe_o_slot_pedido_e_conta_a_tentativa(tmp_path):
    assert decidir(tmp_path, "slot=A\ngood=A\ntries=0\nrecovery=0\n") == ("A", 1)


def test_a_contagem_sobe_a_cada_tentativa_sem_confirmacao(tmp_path):
    assert decidir(tmp_path, "slot=B\ngood=A\ntries=1\nrecovery=0\n") == ("B", 2)
    assert decidir(tmp_path, "slot=B\ngood=A\ntries=2\nrecovery=0\n") == ("B", 3)


def test_na_terceira_falha_volta_para_o_slot_que_sabidamente_sobe(tmp_path):
    """O caso que paga por todo o esquema: a atualização não subiu."""
    assert decidir(tmp_path, "slot=B\ngood=A\ntries=3\nrecovery=0\n") == ("A", 1)


def test_a_volta_passa_a_ser_o_pedido_e_nao_fica_repetindo_o_quebrado(tmp_path):
    """Depois de voltar, o slot bom é quem é tentado -- com a contagem dele."""
    assert decidir(tmp_path, "slot=A\ngood=A\ntries=1\nrecovery=0\n") == ("A", 2)


def test_quando_ate_o_slot_bom_para_de_subir_sobra_o_recovery(tmp_path):
    """Não existe terceiro slot para tentar; quem resolve isso é o recovery."""
    assert decidir(tmp_path, "slot=A\ngood=A\ntries=3\nrecovery=0\n") == ("RECOVERY", 0)


def test_recovery_pedido_de_proposito_ganha_de_tudo(tmp_path):
    assert decidir(tmp_path, "slot=B\ngood=B\ntries=0\nrecovery=1\n") == ("RECOVERY", 0)


# --- arquivos estragados -----------------------------------------------------
def test_arquivo_vazio_sobe_o_slot_A(tmp_path):
    assert decidir(tmp_path, "") == ("A", 1)


def test_arquivo_sem_o_arquivo_sobe_o_slot_A(tmp_path):
    result = subprocess.run(
        ["/bin/sh", SCRIPT, str(tmp_path / "nao-existe.conf")],
        stdout=subprocess.PIPE, check=False,
    )
    assert result.stdout.decode().strip() == "A 1"


def test_slot_invalido_vira_o_padrao_em_vez_de_travar(tmp_path):
    assert decidir(tmp_path, "slot=Z\ngood=Z\ntries=0\nrecovery=0\n") == ("A", 1)


def test_tries_que_nao_e_numero_vira_zero(tmp_path):
    assert decidir(tmp_path, "slot=B\ngood=A\ntries=muitas\nrecovery=0\n") == ("B", 1)


def test_lixo_binario_no_meio_nao_impede_o_boot(tmp_path):
    assert decidir(tmp_path, "\x00\x01lixo\nslot=B\ngood=A\ntries=0\n") == ("B", 1)


def test_comentarios_e_espacos_nao_atrapalham(tmp_path):
    assert decidir(tmp_path, "# comentário\n  slot = B \n good=A\n tries = 2 \n") == ("B", 3)


def test_a_ultima_linha_de_uma_chave_repetida_e_a_que_vale(tmp_path):
    """Um arquivo escrito pela metade não pode virar uma decisão pela metade."""
    assert decidir(tmp_path, "slot=A\nslot=B\ngood=A\ntries=0\n") == ("B", 1)


@pytest.mark.parametrize("veneno", [
    "slot=$(rm -rf /)\ngood=A\ntries=0\n",
    "slot=`reboot`\ngood=A\ntries=0\n",
    "slot=A; rm -rf /\ngood=A\ntries=0\n",
    "tries=0; echo pwned\nslot=A\ngood=A\n",
])
def test_o_arquivo_nao_e_executado_como_shell(tmp_path, veneno):
    """A FAT é escrita por qualquer PC que receba o cartão.

    Dar `eval` nesse arquivo seria entregar o boot do Pi para quem o editasse.
    O pior que uma linha estragada pode causar é cair no padrão.
    """
    slot, _ = decidir(tmp_path, veneno)
    assert slot in ("A", "B", "RECOVERY")


# --- o script do initramfs que usa o decisor ---------------------------------
INITRAMFS_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "image", "stage-project-os", "00-project-os", "files",
    "etc", "initramfs-tools", "scripts", "local-top", "project-os-slot",
)
HOOK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "image", "stage-project-os", "00-project-os", "files",
    "etc", "initramfs-tools", "hooks", "project-os-slot",
)


def test_o_script_do_initramfs_responde_prereqs():
    """initramfs-tools chama todo script com "prereqs" antes de usá-lo.

    Um script que não responde isso é ignorado em silêncio -- o Pi subiria pelo
    caminho normal e o esquema de slots simplesmente não existiria.
    """
    result = subprocess.run(["/bin/sh", INITRAMFS_SCRIPT, "prereqs"],
                            stdout=subprocess.PIPE, check=False)
    assert result.returncode == 0
    saida = result.stdout.decode().split()
    assert "udev" in saida
    # O reparticionamento tem que rodar antes: no primeiro boot o slot B ainda
    # não existe para ser escolhido.
    assert "project-os-layout" in saida


def test_o_script_do_initramfs_e_o_hook_sao_sh_valido():
    for script in (INITRAMFS_SCRIPT, HOOK):
        result = subprocess.run(["/bin/sh", "-n", script],
                                stderr=subprocess.PIPE, check=False)
        assert result.returncode == 0, "%s: %s" % (script, result.stderr.decode())


LAYOUT_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "image", "stage-project-os", "00-project-os", "files",
    "etc", "initramfs-tools", "scripts", "local-top", "project-os-layout",
)


def test_o_script_de_reparticionamento_responde_prereqs():
    result = subprocess.run(["/bin/sh", LAYOUT_SCRIPT, "prereqs"],
                            stdout=subprocess.PIPE, check=False)
    assert result.returncode == 0
    assert result.stdout.decode().strip() == "udev"


def test_o_reparticionamento_nunca_derruba_o_boot():
    """Um cartão com um sistema só ainda sobe; um cartão que não sobe é o único
    problema que este projeto não conserta pela rede."""
    texto = open(LAYOUT_SCRIPT, encoding="utf-8").read()
    assert '"$LAYOUT" "$DISCO" ||' in texto
    assert texto.rstrip().endswith("exit 0")


def test_o_hook_leva_o_decisor_para_dentro_do_initramfs():
    """Um initramfs só tem o que o hook copiar; sem isso o Pi não sobe."""
    texto = open(HOOK, encoding="utf-8").read()
    assert "slot-decide.sh" in texto
    assert "copy_exec /sbin/blkid" in texto
    assert "vfat" in texto


def test_o_script_grava_a_tentativa_antes_de_entregar_o_boot():
    """Um kernel que trava não volta para contar que travou."""
    texto = open(INITRAMFS_SCRIPT, encoding="utf-8").read()
    posicao_gravacao = texto.index('mv -f "$CONF.novo"')
    posicao_export = texto.index("export ROOT")
    assert posicao_gravacao < posicao_export
