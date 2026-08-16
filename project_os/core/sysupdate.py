"""Atualizar o **sistema** pela rede, e não só o app.

``core/updates.py`` troca ``/opt/project-os``. Isso resolve bug no app e não
resolve mais nada -- o arquivo do serviço, o ajudante root, o sudoers e o kernel
moram fora dali, e foi exatamente ali que estavam os bugs que custaram duas
gravações de cartão num dia só:

    "acha uma solucão, de fleshar o sistema COMPLETAMENTE. Pra eu NUNCA MAIS ter
     q fleshar manualmente essa porra."

Aqui a unidade é o sistema inteiro. O caminho é o de ``docs/RECOVERY.md``:

1. o manifesto anuncia um tarball de rootfs com sha256;
2. baixa e **confere antes de escrever qualquer coisa** -- o tarball que não bate
   é apagado, não instalado;
3. o ajudante root formata o slot que *não* está rodando e desempacota lá;
4. aponta o boot para ele e reinicia;
5. o sistema novo confirma que presta ao começar a atender.

O que está rodando não é tocado em nenhum passo. Se o sistema novo não subir, o
initramfs volta sozinho para o antigo -- e o cartão não sai do Pi.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from typing import Any, Callable, Dict, Optional

from project_os import __version__
from project_os.core import slots, updates

log = logging.getLogger(__name__)

#: O ajudante que a imagem instala. É ele que tem root; este módulo não tem.
HELPER = "/usr/local/sbin/project-os-system-update"

DOWNLOAD_TIMEOUT = 3600.0
#: Um rootfs de Pi comprimido dá uns 500 MB. O teto existe para um manifesto
#: errado (ou hostil) não encher o cartão antes de alguém perceber.
MAX_BYTES = 3 * 1024 * 1024 * 1024


class SystemUpdateError(Exception):
    def __init__(self, message: str, code: str = "system_update_failed", hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint


# ---------------------------------------------------------------------------
# o que existe lá fora
# ---------------------------------------------------------------------------
def check(manifest_url: str = "") -> Dict[str, Any]:
    """O que o manifesto diz sobre a versão de **sistema**.

    Um manifesto sem a seção ``system`` é o normal por enquanto: só o app tem
    versão nova. Isso não é erro, é "não há atualização de sistema".
    """
    url = manifest_url or updates.DEFAULT_MANIFEST_URL
    manifest = updates._fetch_json(url)
    system = manifest.get("system") or {}
    version = str(system.get("version") or "").strip()
    tarball = str(system.get("url") or "").strip()
    sha256 = str(system.get("sha256") or "").strip().lower()

    if not version or not tarball:
        return {
            "available": False,
            "current": __version__,
            "latest": "",
            "update_available": False,
            "reason": "Este manifesto não anuncia sistema novo, só app.",
        }
    if not sha256:
        raise SystemUpdateError(
            "O manifesto anuncia um sistema sem sha256, então não dá para conferir o download.",
            code="unverifiable",
            hint="Toda versão tem que publicar a soma de verificação; sem ela, não instalo.",
        )
    return {
        "available": True,
        "current": __version__,
        "latest": version,
        "update_available": updates.is_newer(version),
        "url": tarball,
        "sha256": sha256,
        "size": int(system.get("size") or 0),
        # 880 MB e um reinício: aqui é onde mais importa poder ler o que muda
        # antes de decidir. Ver updates._notes_text -- manifesto antigo manda um
        # endereço neste campo, e endereço não responde "o que mudou".
        "notes": updates._notes_text(system.get("notes")),
        "notes_url": updates._notes_link(system),
    }


# ---------------------------------------------------------------------------
# baixar, conferindo
# ---------------------------------------------------------------------------
def download(url: str, sha256: str, destino: str,
             on_progress: Optional[Callable[[int, int], None]] = None) -> str:
    """Baixa e confere. Um arquivo que não bate com o sha256 não sobrevive.

    O hash é calculado enquanto baixa, e não depois: um rootfs não cabe na
    memória de um Pi 3, e ler 600 MB do cartão de novo só para conferir custa
    minutos que não precisam existir.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    esperado = (sha256 or "").strip().lower()
    if not esperado:
        raise SystemUpdateError("Sem sha256 não instalo nada.", code="unverifiable")

    parcial = destino + ".parcial"
    digest = hashlib.sha256()
    baixado = 0
    request = Request(url, headers={"User-Agent": "project-os/%s" % __version__})
    try:
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as resposta:
            total = int(resposta.headers.get("Content-Length") or 0)
            if total and total > MAX_BYTES:
                raise SystemUpdateError(
                    "O sistema anunciado tem %d bytes; o limite é %d." % (total, MAX_BYTES),
                    code="too_large",
                )
            with open(parcial, "wb") as saida:
                while True:
                    pedaco = resposta.read(1024 * 256)
                    if not pedaco:
                        break
                    baixado += len(pedaco)
                    if baixado > MAX_BYTES:
                        raise SystemUpdateError(
                            "O download passou de %d bytes." % MAX_BYTES, code="too_large"
                        )
                    digest.update(pedaco)
                    saida.write(pedaco)
                    if on_progress:
                        on_progress(baixado, total)
                saida.flush()
                os.fsync(saida.fileno())
    except HTTPError as exc:
        _apagar(parcial)
        raise SystemUpdateError("O servidor respondeu %d." % exc.code, code="download_failed")
    except URLError as exc:
        _apagar(parcial)
        raise SystemUpdateError(
            "Não consegui baixar o sistema: %s" % exc.reason, code="offline",
            hint="Esta caixa precisa de internet para atualizar o sistema.",
        )
    except OSError as exc:
        _apagar(parcial)
        raise SystemUpdateError("Não consegui gravar o download: %s" % exc, code="disk_error")

    obtido = digest.hexdigest()
    if obtido != esperado:
        _apagar(parcial)
        raise SystemUpdateError(
            "O download não bate com a soma de verificação do manifesto.",
            code="checksum_mismatch",
            hint="Nada foi instalado. Ou a versão está corrompida, ou não é a que foi anunciada.",
        )
    os.replace(parcial, destino)
    return destino


def _apagar(caminho: str) -> None:
    try:
        os.remove(caminho)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# escrever no outro slot
# ---------------------------------------------------------------------------
def helper_available() -> bool:
    return os.path.isfile(HELPER) and os.access(HELPER, os.X_OK)


def available() -> Dict[str, Any]:
    """Se esta caixa consegue trocar o sistema pela rede, e por que não."""
    if not slots.available():
        return {
            "ok": False,
            "code": "no_slots",
            "reason": "Este cartão tem um sistema só. Grave a imagem nova uma vez e "
                      "depois disso nunca mais.",
        }
    if not helper_available():
        return {
            "ok": False,
            "code": "no_helper",
            "reason": "Falta o ajudante que escreve no outro sistema (%s)." % HELPER,
        }
    return {"ok": True, "code": "", "reason": ""}


def write_slot(tarball: str, slot: str,
               on_line: Optional[Callable[[str], None]] = None) -> None:
    """Chama o ajudante root: formata o slot e desempacota o sistema nele.

    Este processo roda sem privilégio nenhum de propósito. Formatar partição é
    trabalho de root, e o jeito de ter isso sem virar root é um único comando
    root que só sabe fazer essa coisa.
    """
    diga = on_line or (lambda linha: None)
    if slot not in slots.SLOTS:
        raise SystemUpdateError("Slot inválido: %r" % slot, code="bad_slot")
    if slot == slots.current_slot():
        # A mesma trava existe dentro do ajudante. Duas vezes, porque o preço de
        # errar aqui é apagar o sistema que está atendendo esta requisição.
        raise SystemUpdateError(
            "Recusando escrever no sistema que está rodando.", code="refuse_current",
        )

    comando = []
    if os.geteuid() != 0:
        if not shutil.which("sudo"):
            raise SystemUpdateError("Sem sudo e sem root; não dá.", code="needs_root")
        comando = ["sudo", "-n"]
    comando += [HELPER, tarball, slot]

    try:
        processo = subprocess.Popen(
            comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
    except OSError as exc:
        raise SystemUpdateError("Não consegui rodar o ajudante: %s" % exc, code="helper_failed")

    if processo.stdout is not None:
        for linha in processo.stdout:
            diga(linha.rstrip())
    codigo = processo.wait()
    if codigo != 0:
        raise SystemUpdateError(
            "O ajudante falhou ao gravar o slot %s (código %d)." % (slot, codigo),
            code="helper_failed",
        )


# ---------------------------------------------------------------------------
# o caminho inteiro
# ---------------------------------------------------------------------------
def install(manifest_url: str = "", download_dir: str = "",
            on_line: Optional[Callable[[str], None]] = None,
            reboot: bool = True) -> Dict[str, Any]:
    """Baixa, confere, grava no outro slot, aponta o boot e reinicia."""
    diga = on_line or (lambda linha: None)

    pronto = available()
    if not pronto["ok"]:
        raise SystemUpdateError(pronto["reason"], code=pronto["code"])

    info = check(manifest_url)
    if not info.get("available"):
        raise SystemUpdateError(
            "Não há sistema novo anunciado.", code="no_system_update",
        )
    if not info.get("update_available"):
        raise SystemUpdateError(
            "Esta caixa já está no sistema %s." % info["latest"], code="already_current",
        )

    alvo = slots.other_slot()
    if alvo is None:
        raise SystemUpdateError("Não sei qual é o outro slot.", code="no_slots")

    pasta = download_dir or "/var/lib/project-os/downloads"
    os.makedirs(pasta, exist_ok=True)
    destino = os.path.join(pasta, "project-os-rootfs-%s.tar.gz" % info["latest"])

    # O manifesto diz o tamanho, então dá para saber antes se cabe. Sem isto a
    # descoberta vem no fim de meio giga de download, com a partição de dados
    # dele lotada e um arquivo pela metade para limpar. A folga de 20% é o
    # arquivo mais o respiro que qualquer sistema de arquivos quer ter.
    anunciado = int(info.get("size") or 0)
    if anunciado > 0:
        try:
            livre = shutil.disk_usage(pasta).free
        except OSError:
            livre = 0
        if livre and livre < anunciado * 1.2:
            raise SystemUpdateError(
                "O sistema novo tem %d MB e só há %d MB livres em %s."
                % (anunciado // (1024 * 1024), livre // (1024 * 1024), pasta),
                code="no_space",
                hint="Libere espaço (Arquivos, ou apague downloads antigos) e tente de novo.",
            )

    diga("baixando o sistema %s" % info["latest"])
    ultimo = [-1]

    def progresso(baixado, total):
        if not total:
            return
        porcento = int(baixado * 100 / total)
        if porcento != ultimo[0] and porcento % 5 == 0:
            ultimo[0] = porcento
            diga("baixando: %d%%" % porcento)

    download(info["url"], info["sha256"], destino, on_progress=progresso)
    diga("soma de verificação confere")

    diga("gravando no slot %s (o slot %s continua intocado)" % (alvo, slots.current_slot()))
    write_slot(destino, alvo, on_line=diga)

    slots.boot_into(alvo)
    diga("o próximo boot é o slot %s" % alvo)
    _apagar(destino)

    resultado = {
        "ok": True,
        "installed": info["latest"],
        "slot": alvo,
        "previous_slot": slots.current_slot(),
        "rebooting": bool(reboot),
    }
    if reboot:
        diga("reiniciando")
        subprocess.Popen(updates.systemctl_argv() + ["reboot"])
    return resultado


def status(manifest_url: str = "") -> Dict[str, Any]:
    """O que a tela mostra: o esquema de slots mais o que há para instalar."""
    payload = {"slots": slots.status(), "capability": available()}
    try:
        payload["system"] = check(manifest_url)
    except (SystemUpdateError, updates.UpdateError) as exc:
        payload["system"] = {"available": False, "error": str(exc)}
    return payload


__all__ = [
    "HELPER", "SystemUpdateError", "available", "check", "download", "install",
    "status", "write_slot",
]
