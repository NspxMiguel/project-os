"""Dois sistemas no cartão, um ativo e outro reserva.

Ver ``docs/RECOVERY.md``. O resumo:

    "n quero nunca mais ter q pegar esse sdcard e plugar no meu pc dnv"

Uma atualização de sistema escreve no slot que **não** está rodando, aponta o
boot para ele e reinicia. Se o slot novo não subir, o initramfs volta sozinho
para o que sabidamente sobe. Este módulo é a parte que roda dentro do sistema:
ler e escrever o estado, saber em qual slot estamos, e dizer qual é o outro.

O estado mora na partição FAT porque é o único lugar que o initramfs consegue
montar sem já ter escolhido um slot -- e porque é o único lugar que sobrevive a
qualquer coisa que aconteça com os dois sistemas.

Três decisões que não são óbvias:

* **O slot atual é deduzido do dispositivo montado em ``/``**, não do arquivo de
  estado. O arquivo diz o que *era para* ter subido; o dispositivo diz o que
  subiu. Quando o initramfs desiste de um slot e sobe o outro, os dois discordam,
  e é o dispositivo que está certo.
* **``tries`` é incrementado por quem tenta, e zerado por quem consegue.** Não
  existe "boot bem-sucedido" observável de dentro do initramfs.
* **Escrita atômica sempre.** Corromper este arquivo é o único jeito de precisar
  do cartão de novo, e ele é escrito na hora de reiniciar, que é exatamente
  quando a energia costuma faltar.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

#: Onde o firmware do Pi monta a partição FAT. O caminho antigo (``/boot``) vale
#: como reserva: imagens anteriores ao bookworm usavam ele.
BOOT_DIRS = ("/boot/firmware", "/boot")

STATE_NAME = "project-os-slot.conf"

SLOT_A = "A"
SLOT_B = "B"
SLOTS = (SLOT_A, SLOT_B)

#: Qual partição é qual slot. p1 é a FAT, p4 são os dados.
SLOT_PARTITION = {SLOT_A: 2, SLOT_B: 3}

#: Depois disso o initramfs desiste do slot pedido e sobe o ``good``. Três é o
#: bastante para distinguir "não sobe" de "não subiu daquela vez".
MAX_TRIES = 3

DEFAULT_STATE = {
    "slot": SLOT_A,
    "good": SLOT_A,
    "tries": 0,
    "recovery": 0,
}

_LINE = re.compile(r"^\s*([a-z_]+)\s*=\s*(\S*)\s*$")


# ---------------------------------------------------------------------------
# onde as coisas estão
# ---------------------------------------------------------------------------
def boot_dir() -> str:
    """A partição FAT montada, ou o caminho padrão se nenhuma estiver."""
    for candidate in BOOT_DIRS:
        if os.path.isdir(candidate):
            return candidate
    return BOOT_DIRS[0]


def state_path() -> str:
    return os.path.join(boot_dir(), STATE_NAME)


def root_device() -> str:
    """O dispositivo montado em ``/``, ou "" quando não dá para saber."""
    try:
        result = subprocess.run(
            ["findmnt", "-no", "SOURCE", "/"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or b"").decode("utf-8", "replace").strip()


def _partition_number(device: str) -> Optional[int]:
    """``/dev/mmcblk0p3`` -> 3, ``/dev/sda2`` -> 2."""
    match = re.search(r"(?:p)?(\d+)$", device or "")
    if not match:
        return None
    return int(match.group(1))


def disk_of(device: str) -> str:
    """``/dev/mmcblk0p3`` -> ``/dev/mmcblk0``."""
    if not device:
        return ""
    return re.sub(r"p?\d+$", "", device)


def slot_device(slot: str, disk: str = "") -> str:
    """O dispositivo de um slot, no mesmo disco de onde estamos rodando."""
    where = disk or disk_of(root_device())
    if not where:
        return ""
    number = SLOT_PARTITION[slot]
    # mmcblk e nvme separam o número da partição com "p"; sda não.
    separator = "p" if re.search(r"\d$", where) else ""
    return "%s%s%d" % (where, separator, number)


def current_slot() -> Optional[str]:
    """Em qual slot este sistema está rodando, deduzido do que está montado."""
    number = _partition_number(root_device())
    if number is None:
        return None
    for slot, partition in SLOT_PARTITION.items():
        if partition == number:
            return slot
    return None


def other_slot(slot: Optional[str] = None) -> Optional[str]:
    """O slot que não é este -- o alvo de qualquer atualização de sistema."""
    here = slot or current_slot()
    if here not in SLOTS:
        return None
    return SLOT_B if here == SLOT_A else SLOT_A


def available() -> bool:
    """Se este cartão tem o esquema de dois slots.

    Uma imagem antiga (uma partição só) responde False, e a tela mostra
    "atualização de sistema não disponível neste cartão" em vez de oferecer um
    botão que não teria para onde escrever.
    """
    return current_slot() is not None and os.path.isfile(state_path())


# ---------------------------------------------------------------------------
# o arquivo de estado
# ---------------------------------------------------------------------------
def parse_state(text: str) -> Dict[str, Any]:
    """Lê o formato ``chave=valor``, uma por linha, ignorando o que não conhece.

    Um arquivo ilegível vira o padrão em vez de uma exceção: este arquivo é lido
    no caminho do boot, e "não entendi, então subo o slot A" é infinitamente
    melhor do que não subir nada.
    """
    state = dict(DEFAULT_STATE)
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if key in ("slot", "good"):
            if value.upper() in SLOTS:
                state[key] = value.upper()
        elif key in ("tries", "recovery"):
            try:
                state[key] = int(value)
            except ValueError:
                pass
    return state


def format_state(state: Dict[str, Any]) -> str:
    return (
        "# project-os -- qual sistema sobe. Escrito pelo sistema, lido pelo initramfs.\n"
        "# docs/RECOVERY.md explica. Editar à mão só se algo deu muito errado.\n"
        "slot=%s\n"
        "good=%s\n"
        "tries=%d\n"
        "recovery=%d\n"
    ) % (
        state.get("slot", SLOT_A),
        state.get("good", SLOT_A),
        int(state.get("tries", 0)),
        1 if int(state.get("recovery", 0)) else 0,
    )


def read_state(path: str = "") -> Dict[str, Any]:
    where = path or state_path()
    try:
        with open(where, "r", encoding="utf-8", errors="replace") as handle:
            return parse_state(handle.read())
    except (OSError, IOError):
        return dict(DEFAULT_STATE)


#: O ajudante que grava o estado quando este processo não consegue. Ver
#: ``_write_via_helper``.
STATE_HELPER = "/usr/local/sbin/project-os-slot-state"


def _helper_argv() -> List[str]:
    """``sudo -n`` só quando não somos root -- em teste e no CI somos."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return [STATE_HELPER]
    return ["sudo", "-n", STATE_HELPER]


def _write_via_helper(state: Dict[str, Any]) -> None:
    """Grava pelo ajudante de root.

    A partição FAT é montada com uid=0 e umask 0022 (o padrão do vfat), e este
    serviço roda como usuário comum: ele não consegue criar arquivo nenhum em
    /boot/firmware. Descoberto montando a imagem de verdade e tentando escrever
    como o usuário project-os.

    Isso não seria um detalhe de permissão, seria o fim do esquema: sem escrever
    este arquivo, uma atualização grava o slot B inteiro e nunca consegue
    apontar o boot para ele -- a atualização "dá certo" e nada acontece.
    """
    argv = _helper_argv() + [
        "slot=%s" % state.get("slot", SLOT_A),
        "good=%s" % state.get("good", SLOT_A),
        "tries=%d" % int(state.get("tries", 0)),
        "recovery=%d" % (1 if int(state.get("recovery", 0)) else 0),
    ]
    result = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20.0, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise OSError("o ajudante de estado falhou: %s" % (detail or result.returncode))


def write_state(state: Dict[str, Any], path: str = "") -> None:
    """Escreve e força para o cartão antes de voltar.

    Sem o fsync, "escrevi e mandei reiniciar" é uma corrida contra o cache do
    kernel que o cartão perde de vez em quando -- e perder essa corrida é
    justamente o Pi subir apontando para um slot que ainda não foi gravado.

    Na máquina de verdade a escrita direta não passa (a FAT é do root), e aí
    quem grava é o ajudante. Um caminho explícito -- teste, ou alguém apontando
    para outro cartão -- nunca vai para o ajudante: ele grava no boot desta
    máquina, que não é o que foi pedido.
    """
    where = path or state_path()
    try:
        _write_direct(state, where)
    except OSError:
        if path:
            raise
        log.debug("escrita direta em %s falhou; tentando o ajudante", where)
        _write_via_helper(state)


def _write_direct(state: Dict[str, Any], where: str) -> None:
    temporary = where + ".novo"
    payload = format_state(state)
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, where)
    # FAT não tem journal: sincronizar o diretório é o que garante que o rename
    # chegou ao cartão, e não só à memória.
    try:
        fd = os.open(os.path.dirname(where) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def update_state(path: str = "", **changes: Any) -> Dict[str, Any]:
    state = read_state(path)
    state.update(changes)
    write_state(state, path)
    return state


# ---------------------------------------------------------------------------
# as duas transições que importam
# ---------------------------------------------------------------------------
def boot_into(slot: str, path: str = "") -> Dict[str, Any]:
    """Aponta o próximo boot para ``slot`` e zera o contador de tentativas.

    Não mexe em ``good``: o slot novo ainda não provou nada. É essa distinção
    que dá o caminho de volta.
    """
    if slot not in SLOTS:
        raise ValueError("slot inválido: %r" % slot)
    return update_state(path, slot=slot, tries=0)


def mark_current_good(path: str = "") -> Optional[Dict[str, Any]]:
    """"Este sistema presta": chamado quando o project-os começa a atender.

    Zera ``tries`` e grava ``good`` como o slot que realmente subiu -- que pode
    não ser o que o arquivo pedia, se o initramfs tiver desistido dele.

    Devolve None quando não há slots (cartão antigo, ou rodando num laptop), que
    é o caso em que não existe nada para confirmar.
    """
    here = current_slot()
    if here is None:
        return None
    where = path or state_path()
    state = read_state(where)
    already = (
        os.path.isfile(where)
        and state.get("good") == here
        and state.get("slot") == here
        and int(state.get("tries", 0)) == 0
    )
    if already:
        # Já está assim. Não reescrever poupa uma escrita em FAT por requisição
        # -- e o arquivo *precisa* existir para isso valer: sem ele, "o padrão é
        # igual ao que eu ia gravar" não é motivo para deixar o cartão sem estado.
        return state
    state.update({"slot": here, "good": here, "tries": 0})
    write_state(state, where)
    log.info("slot %s confirmado como bom", here)
    return state


def status(path: str = "") -> Dict[str, Any]:
    """O que a tela mostra sobre o esquema de slots."""
    here = current_slot()
    state = read_state(path)
    return {
        "available": available(),
        "current": here,
        "target": other_slot(here),
        "booted_from": state.get("slot"),
        "good": state.get("good"),
        "tries": int(state.get("tries", 0)),
        "recovery": bool(int(state.get("recovery", 0))),
        "max_tries": MAX_TRIES,
        "devices": {slot: slot_device(slot) for slot in SLOTS} if here else {},
        "state_path": path or state_path(),
    }


__all__ = [
    "MAX_TRIES", "SLOTS", "SLOT_A", "SLOT_B",
    "available", "boot_into", "current_slot", "disk_of", "format_state",
    "mark_current_good", "other_slot", "parse_state", "read_state",
    "slot_device", "state_path", "status", "update_state", "write_state",
]
