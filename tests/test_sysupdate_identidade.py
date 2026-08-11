"""O que é desta caixa tem que atravessar a atualização junto.

O tarball de uma atualização é um sistema de fábrica. Algumas coisas em /etc não
são da versão, são **desta caixa** -- e como /etc mora dentro do slot, e não na
partição de dados, elas não atravessam sozinhas:

* a senha do Wi-Fi. Ela existe só em /etc/NetworkManager/system-connections: o
  project-os-firstboot apaga o project-os-wifi.txt da FAT assim que conecta, de
  propósito, porque é senha em texto puro. Num Pi ligado por Wi-Fi, o slot novo
  subiria sem rede, nunca responderia o /api/system/health, nunca seria
  confirmado, e três boots depois o initramfs voltaria sozinho. Toda atualização
  por Wi-Fi terminaria em volta atrás -- e a saída seria o cartão no PC de novo,
  que é a única coisa que este projeto promete nunca mais;
* a senha que ele criou na tela de primeiro uso, em /etc/shadow. O tarball traz
  a conta trancada, como sai de fábrica;
* as chaves de host do SSH. Trocadas, o cliente recusa a conexão dizendo
  "REMOTE HOST IDENTIFICATION HAS CHANGED", que numa caixa sem tela parece
  invasão e não atualização.

Este teste é uma trava contra alguém apagar essas linhas sem saber o que elas
seguram. O defeito que elas evitam só aparece na casa de quem usa Wi-Fi, e só
depois da primeira atualização.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

AJUDANTE = (
    Path(__file__).resolve().parents[1]
    / "image/stage-project-os/00-project-os/files/usr/local/sbin/project-os-system-update"
)

TEXTO = AJUDANTE.read_text(encoding="utf-8")


def test_o_script_e_sh_valido() -> None:
    resultado = subprocess.run(["/bin/sh", "-n", str(AJUDANTE)], stderr=subprocess.PIPE, check=False)
    assert resultado.returncode == 0, resultado.stderr.decode()


@pytest.mark.parametrize(
    "caminho",
    [
        "etc/NetworkManager/system-connections",
        "etc/wpa_supplicant/wpa_supplicant.conf",
        "etc/ssh/ssh_host_rsa_key",
        "etc/ssh/ssh_host_ecdsa_key",
        "etc/ssh/ssh_host_ed25519_key",
    ],
)
def test_leva_a_identidade_da_caixa(caminho: str) -> None:
    assert caminho in TEXTO, "o slot novo subiria sem %s" % caminho


def test_leva_a_senha_do_dono_sem_levar_o_shadow_inteiro() -> None:
    """O /etc/shadow do tarball tem as contas de sistema que a versão nova criou.

    Copiar o arquivo inteiro por cima levaria a senha e deixaria para trás
    contas que os pacotes novos esperam existir.
    """
    assert "^project-os:" in TEXTO
    assert 'cp -a "/etc/shadow"' not in TEXTO


def test_o_fstab_do_slot_novo_e_ajustado() -> None:
    """Sem isto o slot novo diz que a raiz é a partição do vizinho."""
    assert "fstab-slot.sh" in TEXTO


def test_nada_e_copiado_de_um_caminho_que_veio_de_fora() -> None:
    """A origem é sempre a raiz que está rodando, nunca um argumento.

    É o mesmo princípio da trava que impede formatar o slot em uso: este é o
    lado que tem root, então ele não confia em quem chamou.
    """
    assert 'preservar "$item"' in TEXTO
    for linha in TEXTO.splitlines():
        if linha.strip().startswith("cp -a"):
            assert '"/$1"' in linha, linha
