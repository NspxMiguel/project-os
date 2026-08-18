"""Updating project-os from project-os.

    "faz um sistema tbm, de atualizar o sistema direto pelo sistema. ou seja,
     n precisa fica tirando o pendrive, puxa pela rede, tlvz até um dominio
     nosso"

No SD card leaving the Pi, no reflashing, no laptop. The box fetches its own new
version over the network and restarts into it.

Two ways in, picked automatically, because the two ways project-os gets onto a
machine are different:

* **git** -- what ``install.sh`` does (a clone in ``/opt/project-os``). Updating is
  a fetch and a hard reset onto the tracked branch or a tag. Cheap, and it keeps
  working if the release host is down.
* **tarball** -- a JSON manifest at a URL says what the latest version is and
  where its tarball lives, with a sha256. That is the "domínio nosso" path: the
  manifest can be a file on any web server, GitHub Releases included.

Whichever it is, the shape is the same and the rules do not change:

1. **Nothing is applied that was not verified.** A tarball whose sha256 does not
   match the manifest is deleted, not installed. Skipping this would make every
   update an invitation to whoever can answer for the update host.
2. **The old tree is kept.** The swap is: extract beside, move current aside,
   move new in. If the new version does not boot, the previous one is still on
   disk, and :func:`rollback` puts it back.
3. **State is never touched.** ``PROJECT_OS_HOME`` (database, config, media) lives
   outside the code tree on purpose; an update replaces code and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from project_os import __version__

log = logging.getLogger(__name__)

#: Where the manifest lives when nobody configured anything. A raw file in the
#: public repo: no release infrastructure needed to ship the first update, and it
#: is a plain URL, so pointing this at a domain later changes one setting.
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/NspxMiguel/project-os/main/release/latest.json"
)

NETWORK_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 600.0
GIT_TIMEOUT = 120.0

METHOD_GIT = "git"
METHOD_TARBALL = "tarball"

#: The systemd unit the image installs. Spelled once, here, because the last
#: time it was spelled inline it kept the pre-rename underscore and every
#: update silently failed to restart anything.
UNIT_NAME = "project-os.service"

#: Never inside the code tree, and never removed by an update.
KEEP_IN_PLACE = (".venv", "PEDIDOS.md")

#: Como a troca acontece nesta caixa. ``parent`` é a de sempre -- pasta nova ao
#: lado do código e duas renomeações, atômica do ponto de vista de quem olha.
#: ``in-place`` é a mesma ideia um andar abaixo, dentro da própria pasta do
#: código, para quando a de cima é do root. ``git`` é ``git reset --hard``.
STRATEGY_PARENT = "parent"
STRATEGY_IN_PLACE = "in-place"
STRATEGY_GIT = "git"

#: A árvore antiga guardada por uma atualização. Ao lado do código na troca por
#: fora (``project-os.previous-0.4.6``), dentro dele na troca por dentro
#: (``project-os/.previous-0.4.6``) -- o mesmo sufixo nos dois, porque quem
#: procura versão anterior procura pelas duas.
PREVIOUS_PREFIX = ".previous-"

#: Prefixos das pastas de trabalho da própria atualização, que nunca são parte
#: do código e nunca viajam numa troca.
WORK_PREFIX = ".project_os-update-"
FAILED_PREFIX = ".project_os-failed-"

#: Said whenever the code tree cannot be swapped in place. There is a second,
#: root-privileged way to update on an image install, and it is the one that
#: works there: a whole rootfs written to the spare slot. See docs/RECOVERY.md.
SYSTEM_UPDATE_HINT = (
    "Nesta caixa a atualização é do sistema inteiro: Atualizações > Sistema do "
    "cartão. Ela escreve o sistema novo no slot livre e reinicia nele."
)


class UpdateError(Exception):
    def __init__(self, message: str, code: str = "update_failed", hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint


# ---------------------------------------------------------------------------
# where the code is
# ---------------------------------------------------------------------------
def root_dir() -> str:
    """The directory holding the ``project_os`` package -- the thing we replace."""
    import project_os

    package = os.path.dirname(os.path.abspath(project_os.__file__))
    return os.path.dirname(package)


def is_git_checkout(root: Optional[str] = None) -> bool:
    return os.path.isdir(os.path.join(root or root_dir(), ".git"))


def _git(args: List[str], root: Optional[str] = None, timeout: float = GIT_TIMEOUT) -> str:
    where = root or root_dir()
    try:
        result = subprocess.run(
            ["git", "-C", where] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError("git failed: %s" % exc, code="git_failed")
    if result.returncode != 0:
        raise UpdateError(
            result.stderr.decode("utf-8", "replace").strip() or "git exited %d" % result.returncode,
            code="git_failed",
        )
    return result.stdout.decode("utf-8", "replace").strip()


def method(root: Optional[str] = None) -> str:
    """How this install updates itself."""
    where = root or root_dir()
    if is_git_checkout(where):
        try:
            if _git(["remote"], where):
                return METHOD_GIT
        except UpdateError:
            pass
    return METHOD_TARBALL


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------
def _version_tuple(text: str) -> Tuple[int, ...]:
    parts = []  # type: List[int]
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def is_newer(candidate: str, current: str = __version__) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


# ---------------------------------------------------------------------------
# checking
# ---------------------------------------------------------------------------
def _fetch_json(url: str, timeout: float = NETWORK_TIMEOUT) -> Dict[str, Any]:
    # urllib rather than httpx: checking for updates must work on a box where
    # the optional extras were never installed.
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    request = Request(url, headers={"User-Agent": "project-os/%s" % __version__})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        raise UpdateError(
            "O servidor de atualização respondeu %d." % exc.code, code="manifest_http_error",
            hint="Confira o updates.manifest_url nas Configurações.",
        )
    except URLError as exc:
        raise UpdateError(
            "Não consegui alcançar o servidor de atualização: %s" % exc.reason, code="offline",
            hint="Esta caixa precisa de internet para procurar atualização.",
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UpdateError("O manifesto de atualização não é um JSON válido.", code="bad_manifest") from exc


#: Teto do que vai para a tela. O texto vem de uma mensagem de commit; uma
#: mensagem enorme não pode transformar o cartão numa página de rolagem.
NOTES_MAX = 4000


def _notes_text(bruto: Any) -> str:
    """O que mudou, em texto. Um manifesto antigo manda um endereço aqui.

    Versões até a 0.4.21 gravavam a URL do release no lugar do texto, e uma
    caixa nova conversa com esses manifestos por anos. Endereço não é
    changelog: some daqui e reaparece como link, que é onde ele serve.
    """
    texto = str(bruto or "").strip()
    if not texto:
        return ""
    if texto.startswith(("http://", "https://")) and "\n" not in texto:
        return ""
    return texto[:NOTES_MAX]


def _notes_link(manifest: Dict[str, Any]) -> str:
    endereco = str(manifest.get("notes_url") or "").strip()
    if not endereco:
        antigo = str(manifest.get("notes") or "").strip()
        if antigo.startswith(("http://", "https://")) and "\n" not in antigo:
            endereco = antigo
    return endereco if endereco.startswith(("http://", "https://")) else ""


def check_tarball(manifest_url: str) -> Dict[str, Any]:
    manifest = _fetch_json(manifest_url)
    version = str(manifest.get("version") or "").strip()
    url = str(manifest.get("url") or "").strip()
    sha256 = str(manifest.get("sha256") or "").strip().lower()
    if not version or not url:
        raise UpdateError(
            "Falta \"version\" ou \"url\" no manifesto.", code="bad_manifest",
        )
    if not sha256:
        raise UpdateError(
            "O manifesto não tem sha256, então não dá para conferir o download.",
            code="unverifiable",
            hint="Toda versão tem que publicar a soma de verificação; sem ela, não instalo.",
        )
    return {
        "method": METHOD_TARBALL,
        "current": __version__,
        "latest": version,
        "update_available": is_newer(version),
        "url": url,
        "sha256": sha256,
        # O que mudou, para quem decide se atualiza agora ou não. Já foi só um
        # endereço: a tela mostrava um link embaixo de "O que mudou", e link
        # nenhum responde a pergunta que a tela está fazendo. O texto vem do
        # manifesto; `notes_url` é o lugar de ler por inteiro, à parte.
        "notes": _notes_text(manifest.get("notes")),
        "notes_url": _notes_link(manifest),
        "published_at": str(manifest.get("published_at") or ""),
        "checked_at": time.time(),
    }


def _ref_remota(where: str, branch: str) -> str:
    """Onde está a ponta do ramo remoto, neste clone.

    ``origin/<branch>`` é o nome normal, mas ele **não existe** num clone feito
    com ``--branch <tag>`` ou ``--depth 1``: o refspec desse clone só traz a
    tag, e o ``fetch`` seguinte deposita o resultado em ``FETCH_HEAD`` sem criar
    o ramo. Sem esta segunda tentativa o ``rev-parse`` falha e a tela de
    atualizações responde 500 em vez de dizer se há versão nova -- num clone
    raso, que é o que sai de qualquer ``git clone --depth 1``.
    """
    for ref in ("origin/%s" % branch, "FETCH_HEAD"):
        try:
            achado = _git(["rev-parse", "--verify", "--quiet", "%s^{commit}" % ref], where)
        except UpdateError:
            continue
        if achado:
            return ref
    raise UpdateError(
        "Não deu para achar o ramo %s no repositório de origem." % branch,
        code="branch_missing",
    )


def _e_raso(where: str) -> bool:
    try:
        return _git(["rev-parse", "--is-shallow-repository"], where) == "true"
    except UpdateError:
        return False


def _buscar(where: str, branch: str) -> None:
    """Traz o ramo remoto. As tags vêm junto, mas não mandam.

    ``git fetch --tags`` sai com código **1** quando uma tag local aponta para
    outro commit que a remota ("would clobber existing tag"). Como a conferida
    era um ``--tags`` só, uma tag divergente -- coisa que acontece quando um
    release é remarcado lá em cima -- derrubava a tela inteira com 500, sem
    dizer nem que havia versão nova.

    A resposta que a tela precisa depende do ramo, não das tags. Então as tags
    vêm primeiro e podem falhar à vontade; o ramo vem depois e é ele que manda
    (e é ele que deixa o ``FETCH_HEAD`` certo para o clone raso).
    """
    try:
        _git(["fetch", "--quiet", "--tags", "origin", branch], where)
    except UpdateError as exc:
        log.info("tags não vieram (%s); a conferida segue pelo ramo", exc)
    _git(["fetch", "--quiet", "origin", branch], where)


def check_git(branch: str = "main", root: Optional[str] = None) -> Dict[str, Any]:
    where = root or root_dir()
    _buscar(where, branch)
    local = _git(["rev-parse", "HEAD"], where)
    ref = _ref_remota(where, branch)
    remote = _git(["rev-parse", ref], where)
    behind = "0"
    # Num clone raso o histórico não chega até a base comum, então a contagem
    # sai como "tudo": 213 commits atrás de uma versão publicada hoje. Melhor
    # não dizer número nenhum do que dizer um número errado.
    if local != remote and not _e_raso(where):
        behind = _git(["rev-list", "--count", "HEAD..%s" % ref], where) or "0"
    # A mensagem inteira, não só o assunto: numa instalação por git é ela que
    # responde "o que muda se eu atualizar agora".
    corpo = _git(["log", "-1", "--pretty=%B", ref], where)
    return {
        "method": METHOD_GIT,
        "current": __version__,
        "latest": remote[:12],
        "update_available": local != remote,
        "commits_behind": int(behind or 0),
        "branch": branch,
        "notes": corpo.strip()[:NOTES_MAX],
        "notes_url": "",
        "checked_at": time.time(),
    }


def swap_strategy(root: Optional[str] = None) -> Tuple[str, str]:
    """Por onde a troca de versão consegue passar nesta caixa, e por que não passa.

    O jeito bom é *em volta* da pasta do código: pasta de trabalho ao lado,
    renomeia a atual para ``.previous-<versão>``, renomeia a nova para o lugar.
    Quem olha nunca vê meia árvore, porque a troca é uma renomeação só. Isso
    precisa de escrita na pasta **de cima**.

    Num cartão gravado antes da 0.4.8 essa pasta de cima é o ``/opt`` padrão do
    Debian -- ``root:root 755`` -- e o serviço roda como ``project-os``. Ali as
    três operações são recusadas, e por muito tempo a resposta foi só recusar
    com um motivo: quem estava na 0.4.6 tinha que baixar um sistema inteiro de
    880 MB para receber uma correção de 700 KB, ou levar o cartão até o PC.

    Só que a pasta do código é dele -- o stage faz ``chown -R project-os`` --,
    e a mesma troca cabe um andar abaixo: as coisas velhas vão para
    ``<código>/.previous-<versão>``, as novas sobem no lugar delas. Não é uma
    renomeação só, então existe uma janela de alguns milissegundos com a árvore
    pela metade; em troca, a caixa se conserta sozinha pela rede. Vale a pena
    exatamente onde a alternativa é não atualizar.

    Um checkout git não usa nenhum dos dois: ``git reset --hard`` reescreve
    arquivos dentro da árvore e nunca toca na pasta de cima.
    """
    where = os.path.abspath(root or root_dir())
    if is_git_checkout(where):
        return STRATEGY_GIT, ""
    parent = os.path.dirname(where)
    if os.access(parent, os.W_OK | os.X_OK):
        return STRATEGY_PARENT, ""
    if os.access(where, os.W_OK | os.X_OK):
        return STRATEGY_IN_PLACE, ""
    return "", (
        "Não posso escrever nem em %s nem em %s, e a troca de versão precisa de "
        "uma das duas." % (parent, where)
    )


def can_apply(root: Optional[str] = None) -> Tuple[bool, str]:
    """Whether the code tree can be swapped from here, and why not.

    Answered here rather than at the failure site so the update screen can say
    so before offering a button that cannot work -- e um ``[Errno 13]`` depois
    do download inteiro é a pior hora possível para descobrir.
    """
    estrategia, motivo = swap_strategy(root)
    return bool(estrategia), motivo


def check(manifest_url: str = DEFAULT_MANIFEST_URL, branch: str = "main",
          root: Optional[str] = None) -> Dict[str, Any]:
    if method(root) == METHOD_GIT:
        result = check_git(branch, root)
    else:
        result = check_tarball(manifest_url)
    pode, motivo = can_apply(root)
    result["can_install"] = pode
    result["install_blocked"] = motivo
    if not pode:
        result["install_hint"] = SYSTEM_UPDATE_HINT
    return result


# ---------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------
def _download(url: str, dest: str, expected_sha256: str,
              on_line: Optional[Any] = None, timeout: float = DOWNLOAD_TIMEOUT) -> str:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    request = Request(url, headers={"User-Agent": "project-os/%s" % __version__})
    digest = hashlib.sha256()
    total = 0
    try:
        with urlopen(request, timeout=timeout) as response, open(dest, "wb") as handle:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
    except (HTTPError, URLError, OSError) as exc:
        raise UpdateError("Download failed: %s" % exc, code="download_failed")

    actual = digest.hexdigest()
    if actual != expected_sha256.lower():
        os.remove(dest)
        raise UpdateError(
            "O download não bate com a soma de verificação do manifesto.",
            code="checksum_mismatch",
            hint="Nada foi instalado. Ou a versão está corrompida, ou não é a que foi anunciada.",
        )
    if on_line:
        on_line("downloaded %.1f MB, sha256 ok" % (total / 1048576.0))
    return actual


def _safe_extract(archive: str, into: str) -> str:
    """Extract, refusing any member that would land outside ``into``.

    A tarball is a list of paths chosen by whoever built it. ``../../etc/cron.d``
    is a valid path; refusing it here is the difference between an update and a
    remote write anywhere on the disk.
    """
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        base = os.path.realpath(into)
        for member in members:
            target = os.path.realpath(os.path.join(into, member.name))
            if target != base and not target.startswith(base + os.sep):
                raise UpdateError(
                    "O pacote tenta escrever fora da pasta de instalação (%s)." % member.name,
                    code="unsafe_archive",
                )
            if member.issym() or member.islnk():
                link = os.path.realpath(os.path.join(os.path.dirname(target), member.linkname))
                if link != base and not link.startswith(base + os.sep):
                    raise UpdateError(
                        "O pacote tem um atalho apontando para fora (%s)." % member.name,
                        code="unsafe_archive",
                    )
        tar.extractall(into)

    entries = [name for name in os.listdir(into) if not name.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(into, entries[0])):
        # GitHub-style tarballs wrap everything in project-os-1.2.3/.
        return os.path.join(into, entries[0])
    return into


def _looks_like_project_os(path: str) -> bool:
    return os.path.isdir(os.path.join(path, "project_os")) and os.path.isfile(
        os.path.join(path, "project_os", "__init__.py")
    )


def _own_files(directory: str) -> List[str]:
    """Os nomes do primeiro nível que são *o código*, e não a atualização.

    Fora ficam o que sobrevive a qualquer troca (``.venv``, as anotações dele) e
    as pastas de trabalho da própria atualização -- mover uma delas no meio da
    troca seria a atualização puxando o próprio tapete.
    """
    guardados = []  # type: List[str]
    for name in sorted(os.listdir(directory)):
        if name in KEEP_IN_PLACE:
            continue
        if name.startswith((PREVIOUS_PREFIX, WORK_PREFIX, FAILED_PREFIX)):
            continue
        guardados.append(name)
    return guardados


def _move_names(origem: str, destino: str, nomes: List[str]) -> None:
    """Renomeia cada nome de uma pasta para a outra, desfazendo se parar no meio.

    Renomear dentro do mesmo sistema de arquivos é instantâneo, então a lista
    inteira leva microssegundos -- mas "instantâneo" não é "atômico", e uma
    árvore metade nova metade velha é o pior estado possível para uma caixa que
    ninguém pode abrir. Se a segunda falha, a primeira volta.
    """
    movidos = []  # type: List[str]
    try:
        for name in nomes:
            os.rename(os.path.join(origem, name), os.path.join(destino, name))
            movidos.append(name)
    except OSError:
        for name in reversed(movidos):
            try:
                os.rename(os.path.join(destino, name), os.path.join(origem, name))
            except OSError:  # pragma: no cover - o desfazer também falhou
                pass
        raise


def _swap_in_place(where: str, extracted: str, current: str,
                   say: Any) -> str:
    """Troca o conteúdo da pasta do código sem tocar na pasta de cima.

    O velho desce para ``<código>/.previous-<versão>``, o novo sobe no lugar. O
    ``.venv`` não se mexe -- ele já está onde precisa estar, e os caminhos
    absolutos gravados dentro dele continuam válidos justamente porque a pasta
    do código não mudou de nome.
    """
    previous = os.path.join(where, PREVIOUS_PREFIX + current)
    shutil.rmtree(previous, ignore_errors=True)
    os.mkdir(previous)

    antigos = _own_files(where)
    novos = _own_files(extracted)
    say("trocando por dentro: %d itens saem, %d entram" % (len(antigos), len(novos)))
    _move_names(where, previous, antigos)
    try:
        _move_names(extracted, where, novos)
    except OSError:
        # A árvore nova não entrou: devolve a antiga antes de desistir, senão a
        # caixa fica sem código nenhum.
        _move_names(previous, where, antigos)
        shutil.rmtree(previous, ignore_errors=True)
        raise
    return previous


def apply_tarball(info: Dict[str, Any], root: Optional[str] = None,
                  on_line: Optional[Any] = None) -> Dict[str, Any]:
    """Download, verify, and swap the code tree. Returns where the old one went."""
    where = os.path.abspath(root or root_dir())
    say = on_line or (lambda line: None)

    # Antes de baixar: se não dá para trocar, nada disto vai acontecer, e
    # descobrir depois do download é meio giga jogado fora para acabar mostrando
    # "[Errno 13] Permission denied" na tela dele.
    estrategia, motivo = swap_strategy(where)
    if not estrategia:
        raise UpdateError(motivo, code="root_not_writable", hint=SYSTEM_UPDATE_HINT)

    por_dentro = estrategia == STRATEGY_IN_PLACE
    parent = os.path.dirname(where)
    workdir = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=where if por_dentro else parent)
    try:
        archive = os.path.join(workdir, "release.tar.gz")
        say("fetching %s" % info["url"])
        _download(info["url"], archive, info["sha256"], on_line=say)

        say("extracting")
        extracted = _safe_extract(archive, os.path.join(workdir, "tree"))
        if not _looks_like_project_os(extracted):
            raise UpdateError(
                "O pacote não contém uma árvore do project-os.", code="bad_archive",
            )

        atual = info.get("current") or __version__
        if por_dentro:
            # Aqui o ``.venv`` não é carregado para lugar nenhum: ele já está no
            # lugar certo e é a árvore em volta dele que muda. Carregá-lo seria
            # movê-lo para dentro da pasta de trabalho -- que é apagada no fim.
            previous = _swap_in_place(where, extracted, atual, say)
            say("pronto -- versão anterior guardada em %s" % previous)
            return {"previous": previous, "root": where, "strategy": estrategia}

        # Anything that must survive the swap is carried across rather than
        # restored afterwards: a half-applied update is worse than none.
        for name in KEEP_IN_PLACE:
            source = os.path.join(where, name)
            if os.path.exists(source) and not os.path.exists(os.path.join(extracted, name)):
                say("keeping %s" % name)
                if os.path.isdir(source):
                    shutil.move(source, os.path.join(extracted, name))
                else:
                    shutil.copy2(source, os.path.join(extracted, name))

        previous = "%s%s%s" % (where, PREVIOUS_PREFIX, atual)
        if os.path.exists(previous):
            shutil.rmtree(previous, ignore_errors=True)
        say("swapping in %s" % info.get("latest", "the new version"))
        os.rename(where, previous)
        try:
            os.rename(extracted, where)
        except OSError:
            os.rename(previous, where)  # put it back before giving up
            raise
        say("done -- previous version kept at %s" % previous)
        return {"previous": previous, "root": where, "strategy": estrategia}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def apply_git(info: Dict[str, Any], root: Optional[str] = None,
              on_line: Optional[Any] = None) -> Dict[str, Any]:
    where = root or root_dir()
    say = on_line or (lambda line: None)
    branch = info.get("branch") or "main"
    before = _git(["rev-parse", "HEAD"], where)
    say("fetching origin/%s" % branch)
    _buscar(where, branch)
    # Mesma história do check_git: num clone raso não existe origin/<branch>,
    # e resetar para um ref que não existe deixaria a caixa na versão velha
    # dizendo que atualizou.
    ref = _ref_remota(where, branch)
    say("resetting to %s" % ref)
    _git(["reset", "--quiet", "--hard", ref], where)
    after = _git(["rev-parse", "HEAD"], where)
    say("now at %s" % after[:12])
    return {"previous": before, "root": where}


def previous_versions(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """As árvores que atualizações anteriores guardaram, da mais nova para a mais velha.

    O caminho da anterior era lembrado só na memória do processo -- e a
    atualização reinicia o serviço, então o botão de voltar sumia exatamente
    depois da única ação que o torna útil. A pasta continua no disco ao lado da
    instalação; basta olhar.

    Olha nos dois lugares: ao lado da árvore, que é onde a troca por fora
    guarda, e dentro dela, que é onde a troca por dentro guarda. Uma caixa pode
    ter as duas coisas -- ela pode ter sido atualizada por dentro e depois ter
    ganhado permissão na pasta de cima.
    """
    where = os.path.abspath(root or root_dir())
    found = []  # type: List[Dict[str, Any]]
    lugares = [
        (os.path.dirname(where), os.path.basename(where) + PREVIOUS_PREFIX),
        (where, PREVIOUS_PREFIX),
    ]
    for pasta, prefix in lugares:
        try:
            names = os.listdir(pasta)
        except OSError:
            continue
        for name in names:
            if not name.startswith(prefix):
                continue
            path = os.path.join(pasta, name)
            if not os.path.isdir(path):
                continue
            found.append({
                "path": path,
                "version": name[len(prefix):],
                "at": os.path.getmtime(path),
            })
    found.sort(key=lambda item: item["at"], reverse=True)
    return found


def _rollback_in_place(previous: str, where: str) -> None:
    """Volta uma troca feita por dentro: o que está no lugar desce, o guardado sobe.

    Espelho exato do ``_swap_in_place``, e com o mesmo motivo para não carregar
    o ``.venv``: ele nunca saiu do lugar.
    """
    broken = tempfile.mkdtemp(prefix=FAILED_PREFIX, dir=where)
    try:
        atuais = _own_files(where)
        _move_names(where, broken, atuais)
        try:
            _move_names(previous, where, _own_files(previous))
        except OSError:
            _move_names(broken, where, atuais)
            raise
    finally:
        shutil.rmtree(broken, ignore_errors=True)
    shutil.rmtree(previous, ignore_errors=True)


def rollback(previous: str, root: Optional[str] = None) -> None:
    """Put a tarball update back the way it was."""
    where = os.path.abspath(root or root_dir())
    if not os.path.isdir(previous):
        raise UpdateError("Não existe versão anterior em %s." % previous, code="no_previous")
    if os.path.dirname(os.path.abspath(previous)) == where:
        return _rollback_in_place(os.path.abspath(previous), where)
    broken = "%s.failed" % where
    shutil.rmtree(broken, ignore_errors=True)
    if os.path.exists(where):
        os.rename(where, broken)
        # O virtualenv e as anotações foram *movidos* para a árvore nova pela
        # atualização, então a anterior está sem eles. Restaurar sem trazê-los
        # de volta devolve uma instalação sem interpretador: o launcher cai no
        # python do sistema, que na imagem não tem uvicorn, e o serviço não
        # sobe. Ou seja, o botão que existe para salvar uma atualização ruim
        # deixaria a caixa pior do que ela estava.
        for name in KEEP_IN_PLACE:
            source = os.path.join(broken, name)
            target = os.path.join(previous, name)
            if os.path.exists(source) and not os.path.exists(target):
                if os.path.isdir(source):
                    shutil.move(source, target)
                else:
                    shutil.copy2(source, target)
    os.rename(previous, where)
    shutil.rmtree(broken, ignore_errors=True)


# ---------------------------------------------------------------------------
# dependencies and restart
# ---------------------------------------------------------------------------
def venv_python(root: Optional[str] = None) -> Optional[str]:
    candidate = os.path.join(root or root_dir(), ".venv", "bin", "python3")
    return candidate if os.path.isfile(candidate) else None


def install_requirements(root: Optional[str] = None, on_line: Optional[Any] = None) -> int:
    """Bring the venv in line with the new requirements.txt. Best effort.

    A failure here is reported but does not fail the update: the new code is
    already in place, and a missing optional dependency degrades honestly
    everywhere in project-os by design.
    """
    where = root or root_dir()
    say = on_line or (lambda line: None)
    requirements = os.path.join(where, "requirements.txt")
    python = venv_python(where) or sys.executable
    if not os.path.isfile(requirements):
        say("no requirements.txt; skipping")
        return 0
    say("installing dependencies")
    process = subprocess.Popen(
        [python, "-m", "pip", "install", "--quiet", "--upgrade", "-r", requirements],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, universal_newlines=True,
    )
    if process.stdout is not None:
        for line in process.stdout:
            say(line.rstrip())
    return process.wait()


#: O que a imagem instala fora do requirements.txt, no 01-run.sh do build.
#:
#: O requirements.txt é atualizado a cada versão pelo install_requirements
#: acima, e tudo que está lá vem junto. Estes dois não estão: entram uma vez, no
#: dia em que a imagem é construída, e ficam parados para sempre.
#:
#: Para o casttube isso quase não importa. Para o yt-dlp importa muito: o
#: trabalho dele é perseguir mudança do YouTube, que acontece toda semana, e por
#: isso ele lança versão quase toda semana também. Uma caixa de seis meses tenta
#: baixar com um baixador de seis meses e falha com "Unable to extract player
#: response" -- uma frase que não diz para ninguém que a causa é a idade.
EXTRAS_DA_IMAGEM = ("yt-dlp", "casttube")


def _pacotes_instalados(python: str) -> List[str]:
    """Os nomes que o pip deste venv conhece, em minúsculas."""
    try:
        saida = subprocess.check_output(
            [python, "-m", "pip", "list", "--format=freeze", "--disable-pip-version-check"],
            stderr=subprocess.DEVNULL, universal_newlines=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    nomes = []
    for linha in saida.splitlines():
        nome = linha.split("==")[0].split(" @ ")[0].strip().lower()
        if nome:
            nomes.append(nome)
    return nomes


def refresh_extras(root: Optional[str] = None, on_line: Optional[Any] = None) -> int:
    """Atualiza os extras da imagem que já estão instalados. Melhor esforço.

    Só atualiza o que já existe, e nunca instala o que não existe: numa caixa
    onde o yt-dlp nunca entrou, o app funciona e diz o que falta, e puxar uma
    dependência opcional pela primeira vez numa atualização de rotina seria
    decidir por quem instalou.
    """
    where = root or root_dir()
    say = on_line or (lambda line: None)
    python = venv_python(where) or sys.executable
    instalados = _pacotes_instalados(python)
    presentes = [nome for nome in EXTRAS_DA_IMAGEM if nome.lower() in instalados]
    if not presentes:
        say("no image extras installed; nothing to refresh")
        return 0
    say("refreshing %s" % ", ".join(presentes))
    process = subprocess.Popen(
        [python, "-m", "pip", "install", "--quiet", "--upgrade"] + presentes,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, universal_newlines=True,
    )
    if process.stdout is not None:
        for line in process.stdout:
            say(line.rstrip())
    return process.wait()


def under_systemd() -> bool:
    return bool(os.environ.get("INVOCATION_ID")) or os.path.exists("/run/systemd/system")


def systemctl_argv(binary: str = "systemctl") -> List[str]:
    """``systemctl`` prefixed with sudo unless this process is already root.

    The service runs as an unprivileged user on purpose, so a bare ``systemctl
    restart`` is refused -- which meant the update swapped the code, said it had
    finished, and left the old version serving until the next power cut. The
    image's sudoers grants exactly this command with no password.

    Public, and imported by :mod:`project_os.api.system` and
    :mod:`project_os.core.sysupdate`, because the same trap caught the Services
    screen months later: it ran a bare ``systemctl restart`` and every button on
    it failed on a real box. One copy of this decision, not four.

    ``binary`` existe porque o ``journalctl`` tem exatamente o mesmo problema:
    como usuário comum o journal do sistema vem vazio, e a tela de Registros de
    um serviço ficaria em branco sem erro nenhum.
    """
    getuid = getattr(os, "geteuid", None)
    if getuid is not None and getuid() == 0:
        return [binary]
    if shutil.which("sudo"):
        return ["sudo", "-n", binary]
    return [binary]


def restart(on_line: Optional[Any] = None) -> str:
    """Restart into the new code, whichever way this process is supervised.

    Under systemd the unit is restarted and this process is expected to die. Run
    by hand, it re-executes itself -- which is the same thing minus the
    supervisor, and it means the update works during development too.
    """
    say = on_line or (lambda line: None)
    if under_systemd() and shutil.which("systemctl"):
        # The unit is ``project-os.service``. It was ``project_os`` before the
        # rename, and restarting a unit that does not exist fails quietly: the
        # update reported success and the box went on serving the old code until
        # somebody happened to reboot it.
        say("reiniciando o serviço project-os")
        subprocess.Popen(systemctl_argv() + ["restart", UNIT_NAME])
        return "systemd"
    argv = restart_argv()
    # The swap renamed the directory this process is sitting in, and a working
    # directory follows the inode, not the name: without this chdir the restart
    # comes back up inside `<root>.previous-<version>` -- old code, new
    # everything else, and a version number that never changes no matter how
    # many times you update. Chdir by path re-resolves onto the new tree.
    where = root_dir()
    try:
        os.chdir(where)
        say("working directory: %s" % where)
    except OSError as exc:  # pragma: no cover - the tree we just installed
        say("could not chdir to %s: %s" % (where, exc))
    say("re-executing: %s" % " ".join(argv))
    os.execv(argv[0], argv)
    return "exec"  # pragma: no cover - execv does not return


def restart_argv(argv: Optional[List[str]] = None, executable: Optional[str] = None) -> List[str]:
    """The command line to come back up with.

    ``python -m project_os`` leaves ``sys.argv[0]`` as the path to
    ``project_os/__main__.py``. Re-executing *that* path is not the same command:
    Python then puts ``project_os/`` itself on sys.path instead of its parent, so
    ``import project_os`` fails unless the package also happens to be installed in
    the venv. Restarting has to be the one thing that always works, so ``-m`` is
    reconstructed.
    """
    original = list(argv if argv is not None else _original_argv())
    python = executable or sys.executable
    first = original[0] if original else ""
    if os.path.basename(first) == "__main__.py":
        package = os.path.basename(os.path.dirname(os.path.abspath(first))) or "project_os"
        return [python, "-m", package] + original[1:]
    return [python] + original


_argv_snapshot = None  # type: Optional[List[str]]


def remember_argv(argv: Optional[List[str]] = None) -> None:
    """Called once at boot, before anything has a chance to mutate sys.argv."""
    global _argv_snapshot
    _argv_snapshot = list(argv if argv is not None else sys.argv)


def _original_argv() -> List[str]:
    return list(_argv_snapshot if _argv_snapshot is not None else sys.argv)


__all__ = [
    "DEFAULT_MANIFEST_URL",
    "METHOD_GIT",
    "METHOD_TARBALL",
    "UpdateError",
    "apply_git",
    "apply_tarball",
    "check",
    "check_git",
    "check_tarball",
    "install_requirements",
    "refresh_extras",
    "is_git_checkout",
    "is_newer",
    "method",
    "previous_versions",
    "remember_argv",
    "restart",
    "restart_argv",
    "rollback",
    "root_dir",
    "swap_strategy",
    "under_systemd",
]
