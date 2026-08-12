"""Um slot recém-instalado só é confirmado depois de aparecer na rede.

Chegar ao fim do boot prova que o sistema sobe e que o project-os atende. Não
prova que ele é **alcançável** -- e uma caixa sem tela que não aparece na rede é
indistinguível de uma caixa morta. Se uma atualização quebrasse a rede, o slot
novo se daria por bom, o esquema não voltaria atrás, e a única saída seria o
cartão no PC. É exatamente o que este projeto existe para nunca precisar.

A exigência é estreita de propósito, e a estreiteza é metade do teste: vale só
para slot que ainda não foi confirmado. Cobrar rede de todo boot faria um
roteador fora do ar virar troca de sistema -- três ligadas sem rede e o
initramfs desistiria de um sistema perfeitamente bom, alternando as versões nas
costas do dono.

Os imports moram dentro de cada teste de propósito: o conftest apaga os módulos
de ``project_os`` do ``sys.modules`` entre testes, e a corrotina importa os seus
na hora de rodar. Importar aqui em cima deixaria o patch numa instância que
ninguém mais usa -- o teste passaria sozinho e falharia junto com os outros.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def conf(tmp_path):
    return tmp_path / "project-os-slot.conf"


def escrever(caminho, slot="B", good="A", tries=1):
    caminho.write_text(
        "slot=%s\ngood=%s\ntries=%d\nrecovery=0\n" % (slot, good, tries), encoding="utf-8"
    )


def preparar(monkeypatch, caminho, slot_atual="B"):
    """Aponta o módulo para o arquivo de mentira e devolve os módulos de agora."""
    from project_os import main
    from project_os.core import slots, sysinfo

    monkeypatch.setattr(slots, "state_path", lambda: str(caminho))
    monkeypatch.setattr(slots, "current_slot", lambda: slot_atual)
    return main, slots, sysinfo


def test_slot_recem_instalado_esta_por_provar(monkeypatch, conf):
    escrever(conf, slot="B", good="A")
    _, slots, _ = preparar(monkeypatch, conf)
    assert slots.slot_por_provar() is True


def test_slot_ja_confirmado_nao_precisa_provar_nada(monkeypatch, conf):
    """O boot comum: exigir rede aqui faria a caixa trocar de sistema à toa."""
    escrever(conf, slot="B", good="B", tries=0)
    _, slots, _ = preparar(monkeypatch, conf)
    assert slots.slot_por_provar() is False


def test_sem_slots_nao_ha_o_que_provar(monkeypatch, conf):
    escrever(conf, slot="B", good="A")
    _, slots, _ = preparar(monkeypatch, conf, slot_atual=None)
    assert slots.slot_por_provar() is False


def test_confirma_assim_que_aparece_um_endereco(monkeypatch, conf):
    escrever(conf, slot="B", good="A", tries=1)
    main, slots, sysinfo = preparar(monkeypatch, conf)

    chamadas = {"n": 0}

    def ips():
        chamadas["n"] += 1
        return ["192.168.1.42"] if chamadas["n"] >= 2 else []

    monkeypatch.setattr(sysinfo, "local_ips", ips)
    asyncio.run(main._confirmar_slot_novo(limite=10.0))

    depois = slots.read_state(str(conf))
    assert depois["good"] == "B"
    assert depois["tries"] == 0


def test_sem_rede_o_slot_nao_e_confirmado(monkeypatch, conf):
    """O caso que importa: é o que faz o cartão voltar sozinho."""
    escrever(conf, slot="B", good="A", tries=1)
    main, slots, sysinfo = preparar(monkeypatch, conf)

    monkeypatch.setattr(sysinfo, "local_ips", lambda: [])
    asyncio.run(main._confirmar_slot_novo(limite=0.2))

    depois = slots.read_state(str(conf))
    assert depois["good"] == "A", "confirmou um sistema que não aparece na rede"
    assert depois["tries"] == 1, "zerou o contador que faz o cartão voltar atrás"


def test_um_erro_no_meio_nao_derruba_o_boot(monkeypatch, conf):
    escrever(conf, slot="B", good="A")
    main, _, sysinfo = preparar(monkeypatch, conf)

    def explode():
        raise RuntimeError("psutil sumiu")

    monkeypatch.setattr(sysinfo, "local_ips", explode)
    asyncio.run(main._confirmar_slot_novo(limite=1.0))  # não pode levantar
