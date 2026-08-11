"""O esquema de dois slots -- a parte que decide para onde o Pi vai reiniciar.

Errar aqui é exatamente o desastre que este esquema existe para evitar: o cartão
de volta no PC. Então os casos ruins têm mais teste que os bons.
"""

from __future__ import annotations

import os

import pytest

from project_os.core import slots


@pytest.fixture()
def conf(tmp_path):
    return str(tmp_path / "project-os-slot.conf")


# --- leitura -----------------------------------------------------------------
def test_um_arquivo_normal_e_lido_como_esta(conf):
    open(conf, "w").write("slot=B\ngood=A\ntries=2\nrecovery=0\n")
    state = slots.read_state(conf)
    assert state == {"slot": "B", "good": "A", "tries": 2, "recovery": 0}


def test_um_arquivo_que_nao_existe_vira_o_padrao(conf):
    """No caminho do boot, "não entendi" tem que virar "sobe o A", não uma exceção."""
    assert slots.read_state(conf) == slots.DEFAULT_STATE


def test_lixo_no_arquivo_nao_derruba_nada(conf):
    open(conf, "w").write("\x00\x00lixo\nslot=Z\ntries=abc\ngood=B\n[secao]\n")
    state = slots.read_state(conf)
    assert state["good"] == "B"      # a linha boa foi aproveitada
    assert state["slot"] == "A"      # "Z" não é slot; fica o padrão
    assert state["tries"] == 0       # "abc" não é número


def test_comentarios_e_espacos_sao_ignorados(conf):
    open(conf, "w").write("# comentário\n\n  slot = B  \n\tgood=B\n")
    state = slots.read_state(conf)
    assert state["slot"] == "B" and state["good"] == "B"


# --- escrita -----------------------------------------------------------------
def test_o_que_e_escrito_e_lido_de_volta_igual(conf):
    slots.write_state({"slot": "B", "good": "B", "tries": 1, "recovery": 1}, conf)
    assert slots.read_state(conf) == {"slot": "B", "good": "B", "tries": 1, "recovery": 1}


def test_a_escrita_nao_deixa_arquivo_temporario_para_tras(conf):
    slots.write_state({"slot": "A", "good": "A", "tries": 0, "recovery": 0}, conf)
    assert os.listdir(os.path.dirname(conf)) == [os.path.basename(conf)]


def test_o_arquivo_gravado_e_legivel_por_um_shell_burro(conf):
    """Quem lê isso no boot é um script de initramfs, não um parser."""
    slots.write_state({"slot": "B", "good": "A", "tries": 2, "recovery": 0}, conf)
    linhas = [l for l in open(conf).read().splitlines() if l and not l.startswith("#")]
    assert linhas == ["slot=B", "good=A", "tries=2", "recovery=0"]


# --- as transições -----------------------------------------------------------
def test_apontar_para_o_outro_slot_zera_as_tentativas_e_preserva_o_good(conf):
    """O caminho de volta é o ``good``; uma atualização não pode encostar nele."""
    slots.write_state({"slot": "A", "good": "A", "tries": 2, "recovery": 0}, conf)
    state = slots.boot_into("B", conf)
    assert state["slot"] == "B"
    assert state["good"] == "A"
    assert state["tries"] == 0


def test_apontar_para_um_slot_inexistente_e_recusado(conf):
    with pytest.raises(ValueError):
        slots.boot_into("C", conf)


def test_confirmar_grava_o_slot_que_realmente_subiu(conf, monkeypatch):
    """O initramfs pode ter desistido do pedido: quem manda é o que está montado."""
    slots.write_state({"slot": "B", "good": "A", "tries": 3, "recovery": 0}, conf)
    monkeypatch.setattr(slots, "current_slot", lambda: "A")
    state = slots.mark_current_good(conf)
    assert state == {"slot": "A", "good": "A", "tries": 0, "recovery": 0}


def test_confirmar_depois_de_uma_atualizacao_bem_sucedida(conf, monkeypatch):
    slots.write_state({"slot": "B", "good": "A", "tries": 1, "recovery": 0}, conf)
    monkeypatch.setattr(slots, "current_slot", lambda: "B")
    assert slots.mark_current_good(conf)["good"] == "B"


def test_confirmar_sem_slots_nao_escreve_nada(conf, monkeypatch):
    """Num laptop, ou num cartão antigo, não existe nada para confirmar."""
    monkeypatch.setattr(slots, "current_slot", lambda: None)
    assert slots.mark_current_good(conf) is None
    assert not os.path.exists(conf)


def test_confirmar_de_novo_nao_reescreve_o_cartao(conf, monkeypatch):
    """Uma escrita em FAT por boot é aceitável; uma por requisição não é."""
    monkeypatch.setattr(slots, "current_slot", lambda: "A")
    slots.mark_current_good(conf)
    antes = os.stat(conf).st_mtime_ns
    slots.mark_current_good(conf)
    assert os.stat(conf).st_mtime_ns == antes


# --- dispositivos ------------------------------------------------------------
@pytest.mark.parametrize("device, esperado", [
    ("/dev/mmcblk0p2", "A"),
    ("/dev/mmcblk0p3", "B"),
    ("/dev/sda2", "A"),
    ("/dev/mmcblk0p1", None),   # a FAT não é slot
    ("/dev/mmcblk0p4", None),   # dados não são slot
    ("", None),
])
def test_o_slot_atual_vem_do_dispositivo_montado(monkeypatch, device, esperado):
    monkeypatch.setattr(slots, "root_device", lambda: device)
    assert slots.current_slot() == esperado


def test_o_outro_slot_e_sempre_o_alvo_da_atualizacao(monkeypatch):
    monkeypatch.setattr(slots, "root_device", lambda: "/dev/mmcblk0p2")
    assert slots.other_slot() == "B"
    monkeypatch.setattr(slots, "root_device", lambda: "/dev/mmcblk0p3")
    assert slots.other_slot() == "A"


def test_sem_slot_nenhum_nao_existe_outro(monkeypatch):
    monkeypatch.setattr(slots, "root_device", lambda: "/dev/sda9")
    assert slots.other_slot() is None


@pytest.mark.parametrize("raiz, slot, esperado", [
    ("/dev/mmcblk0p2", "B", "/dev/mmcblk0p3"),
    ("/dev/mmcblk0p3", "A", "/dev/mmcblk0p2"),
    ("/dev/sda2", "B", "/dev/sda3"),
    ("/dev/nvme0n1p2", "B", "/dev/nvme0n1p3"),
])
def test_o_dispositivo_do_slot_sai_do_disco_onde_estamos(monkeypatch, raiz, slot, esperado):
    """O "p" antes do número existe em mmcblk e nvme, e não existe em sda."""
    monkeypatch.setattr(slots, "root_device", lambda: raiz)
    assert slots.slot_device(slot) == esperado


def test_um_cartao_de_uma_particao_so_diz_que_nao_da(monkeypatch, conf):
    """A tela precisa oferecer "atualizar o sistema" só onde isso existe."""
    monkeypatch.setattr(slots, "root_device", lambda: "/dev/mmcblk0p2")
    monkeypatch.setattr(slots, "state_path", lambda: conf)
    assert slots.available() is False          # sem arquivo de estado
    slots.write_state(dict(slots.DEFAULT_STATE), conf)
    assert slots.available() is True


def test_o_status_mostra_de_onde_subiu_e_para_onde_vai(monkeypatch, conf):
    monkeypatch.setattr(slots, "root_device", lambda: "/dev/mmcblk0p3")
    monkeypatch.setattr(slots, "state_path", lambda: conf)
    slots.write_state({"slot": "B", "good": "A", "tries": 1, "recovery": 0}, conf)
    info = slots.status(conf)
    assert info["current"] == "B"
    assert info["target"] == "A"
    assert info["good"] == "A"
    assert info["devices"]["A"] == "/dev/mmcblk0p2"
