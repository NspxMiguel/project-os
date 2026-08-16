"""YouTube import through yt-dlp -- download and cache, never stream live.

*"Musicas do youtube ou mp3 flac ou qualquer merda com arquivo de musica"* --
YouTube is a source, not a special case: once downloaded a track is a local
file like any other and the rest of the app never knows where it came from.

Section 3 of docs/BIRDTUNES.md is normative here: download once, dedupe on
the YouTube video id, keep going when one item in a playlist fails, and never
shell out to ``youtube-dl``/``yt-dlp`` as a subprocess -- ``yt_dlp`` is a pure
Python library, invoked in-process, imported lazily so a box without it still
starts (the import UI just explains why it is disabled).
"""

from __future__ import annotations

import os
import re
import shutil
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from project_os.db import utcnow_iso

from . import library

INSTALL_HINT = "pip install yt-dlp (YouTube import is an optional dependency)"

IMPORT_STATES = ("queued", "running", "done", "error", "cancelled")


class SourceError(Exception):
    """A source-side failure with an actionable hint, same shape as PlayerError."""

    def __init__(self, message: str, code: str = "source_error", hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint

    def as_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "message": self.message, "hint": self.hint}


def _yt_dlp() -> Any:
    try:
        import yt_dlp
        import yt_dlp.utils  # noqa: F401
    except ImportError as exc:
        raise SourceError(
            "yt-dlp is not installed, so YouTube import is unavailable.",
            code="ytdlp_missing",
            hint=INSTALL_HINT,
        ) from exc
    return yt_dlp


def available() -> bool:
    """Whether YouTube import can run at all.

    Already-imported wins over find_spec: a module sitting in ``sys.modules``
    is importable by definition, and find_spec raises ValueError on any module
    whose ``__spec__`` is None -- which is true of anything installed by hand
    into sys.modules, tests included. Asking find_spec first turned a module
    that is right there into "not installed".
    """
    import importlib.util
    import sys

    if "yt_dlp" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


#: Every shape of YouTube link a person actually pastes.
_YOUTUBE_ID = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/))([A-Za-z0-9_-]{11})"
)


def youtube_video_id(url: str) -> str:
    """The video id in ``url``, or "" -- no network, no yt-dlp.

    Used by the "play it on the television without downloading anything" path,
    which must work on a box where yt-dlp was never installed.
    """
    text = str(url or "").strip()
    if re.match(r"^[A-Za-z0-9_-]{11}$", text):
        return text  # already an id
    match = _YOUTUBE_ID.search(text)
    return match.group(1) if match else ""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


#: Qualidade do VBR do lame. 2 é "quase transparente" e ainda cabe num cartão.
MP3_QUALITY = "2"

#: Uma faixa de dez minutos num Pi 3 leva bem menos que isso; o teto existe
#: para um arquivo quebrado não segurar a fila para sempre.
CONVERT_TIMEOUT = 900.0


def convert_to_mp3(source: str, dest_dir: Optional[str] = None) -> str:
    """Converte um arquivo para MP3 ao lado do original e devolve o caminho novo.

    O botão "Converter para MP3" existia na tela desde o começo e o endpoint
    respondia ``{"queued": [...]}`` -- uma lista de ids de volta, sem fila,
    sem ffmpeg, sem arquivo nenhum. Quem tinha um flac e uma Apple TV (que não
    toca flac) clicava, via um toast de sucesso e continuava sem tocar nada.

    O original não é apagado: quem quiser recuperar o flac depois de ouvir o
    mp3 não tem como desfazer uma conversão.
    """
    if not ffmpeg_available():
        raise RuntimeError("O ffmpeg não está instalado nesta máquina.")
    if not os.path.isfile(source):
        raise FileNotFoundError(source)
    pasta = dest_dir or os.path.dirname(source)
    base = os.path.splitext(os.path.basename(source))[0]
    destino = os.path.join(pasta, "%s.mp3" % base)
    if os.path.abspath(destino) == os.path.abspath(source):
        raise RuntimeError("%s já é um mp3." % os.path.basename(source))
    if os.path.exists(destino):
        return destino

    import subprocess

    parcial = destino + ".part.mp3"
    argv = [
        "ffmpeg", "-nostdin", "-y", "-i", source,
        "-vn", "-codec:a", "libmp3lame", "-q:a", MP3_QUALITY,
        parcial,
    ]
    try:
        done = subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=CONVERT_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("O ffmpeg não conseguiu rodar: %s" % exc)
    if done.returncode != 0 or not os.path.exists(parcial):
        detalhe = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(detalhe[-1] if detalhe else "o ffmpeg falhou")
    # Só vira o arquivo final depois de pronto: uma queda de energia no meio
    # deixaria um mp3 truncado na biblioteca, e a biblioteca varre por extensão.
    os.replace(parcial, destino)
    return destino


_ERROR_PREFIX = re.compile(r"^ERROR:\s*(\[[^\]]+\]\s*)?([\w-]{6,}:\s*)?")


def _tidy_error(message: Any) -> str:
    """yt-dlp's line, minus the plumbing.

    "ERROR: [youtube] Qk14auV_OJs: The page needs to be reloaded." is a sentence
    with the useful part in the middle; the screen shows this, so it says what
    happened and not which extractor said it.
    """
    text = str(message or "").strip()
    text = _ERROR_PREFIX.sub("", text)
    marker = ". Use --cookies"
    if marker in text:
        text = text.split(marker, 1)[0] + "."
    return text.strip()


class _ErrorCatcher(object):
    """Keeps yt-dlp's last error line.

    ``ignoreerrors`` is on so one bad video in a playlist does not sink the
    whole import -- but it also means a refused video comes back as ``None``
    with no exception, and the job used to report "Nothing new to import.",
    which reads as "you already had it". A private video, an age gate or a
    region block all end up here, and the user deserves the actual sentence.
    """

    def __init__(self) -> None:
        self.last = ""

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        text = _tidy_error(message)
        if text:
            self.last = text


#: O que o SponsorBlock corta do que é baixado. Escolhidas para música: são as
#: categorias que a comunidade marca como propaganda e enrolação, e não trechos
#: musicais. ``music_offtopic`` existe exatamente para vídeo de música com
#: conversa no meio.
SPONSOR_CATEGORIES = ("sponsor", "selfpromo", "interaction", "music_offtopic")


def sponsorblock_available() -> Tuple[bool, str]:
    """Se dá para cortar patrocínio do que for baixado, e por que não.

    Precisa de duas coisas: o yt-dlp saber consultar o SponsorBlock (que é uma
    base pública de trechos marcados por gente) e o ffmpeg, porque cortar
    pedaço do meio de um arquivo é trabalho dele. Sem ffmpeg o yt-dlp baixa e
    ignora as marcas -- em silêncio, que é o que esta função existe para
    evitar.
    """
    try:
        from yt_dlp.postprocessor import SponsorBlockPP  # noqa: F401
    except Exception:
        return False, "O yt-dlp desta máquina não conhece o SponsorBlock."
    if not ffmpeg_available():
        return False, "Falta o ffmpeg: sem ele não dá para cortar trecho do meio do arquivo."
    return True, ""


def _perto_da_taxa(quality: str) -> List[str]:
    """Ordenar os formatos pela taxa mais perto da que o mp3 vai ter.

    ``abr~192`` é o jeito do yt-dlp de dizer "prefira o mais próximo de 192",
    e não "pelo menos 192" -- na tabela real de um vídeo (48, 50, 129, 130,
    195 e 387 kbps) isso escolhe o de 195. Sem número configurado não há o que
    preferir, e aí vale o padrão do yt-dlp.
    """
    alvo = str(quality or "").strip()
    return ["abr~%d" % int(alvo)] if alvo.isdigit() and int(alvo) > 0 else []


def _base_options(dest_dir: str, quality: str, noplaylist: bool,
                  skip_sponsors: bool = True,
                  categories: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    postprocessors = []
    # Ordem importa: o SponsorBlock marca, o ModifyChapters corta, e só então o
    # áudio é extraído -- extrair antes cortaria depois de já ter jogado fora a
    # informação dos trechos.
    if skip_sponsors and sponsorblock_available()[0]:
        wanted = list(categories or SPONSOR_CATEGORIES)
        postprocessors.append(
            {"key": "SponsorBlock", "categories": wanted, "when": "after_filter"}
        )
        postprocessors.append(
            {"key": "ModifyChapters", "remove_sponsor_segments": wanted}
        )
    if ffmpeg_available():
        postprocessors.append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality}
        )
    return {
        # `bestaudio/best` parece pedir áudio e não pede: quando o cliente do
        # YouTube só oferece formatos progressivos (áudio *dentro* do vídeo),
        # `bestaudio` casa com um deles e o que desce é o filme inteiro. Medido
        # aqui, no vídeo que ele mandou: 180 MB pelo cliente `android` contra
        # 68,8 MB de áudio puro -- baixados, decodificados e jogados fora, num
        # Pi 3B, para sobrar um mp3. `[vcodec=none]` é o que exige áudio de
        # verdade; a cadeia atrás dele existe para não desistir de baixar quando
        # nenhum cliente oferece um.
        "format": "bestaudio[vcodec=none]/bestaudio/best",
        # `bestaudio` quer dizer o de maior taxa, e o YouTube guarda um de 387
        # kbps: 28,9 MB baixados num vídeo de 10 minutos para virar um mp3 de
        # 192 -- metade jogada fora na conversão. Pedindo a faixa mais perto da
        # taxa que se vai gerar, o mesmo vídeo desce com 14,5 MB (195 kbps) sem
        # perder nada que sobreviva ao mp3. Quem escolher 320 continua levando a
        # faixa grande, que é o que ele pediu.
        "format_sort": _perto_da_taxa(quality),
        "outtmpl": os.path.join(dest_dir, "%(title).120B [%(id)s].%(ext)s"),
        "noplaylist": noplaylist,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cachedir": False,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "postprocessors": postprocessors,
    }


# Which YouTube front-end yt-dlp pretends to be. The default one gets refused
# often ("The page needs to be reloaded."), and the same video downloads fine as
# the Android client -- verified on this machine minutes after the web client
# was blocked. Tried in order, first one that works wins.
#: `android_vr` vem primeiro porque é o único, hoje, que oferece faixa de áudio
#: separada: os outros ou são recusados ("The page needs to be reloaded.") ou
#: respondem só com formatos progressivos, e aí não existe escolha a fazer --
#: baixar áudio vira baixar o vídeo inteiro. Medido nos três vídeos do teste.
PLAYER_CLIENTS = ("android_vr", "", "android", "ios", "web_safari", "tv")


def _download_once(yt_dlp: Any, opts: Dict[str, Any], url: str, client: str):
    """One download attempt as one player client. Returns (info, ydl, error)."""
    attempt = dict(opts)
    if client:
        extractor_args = dict(attempt.get("extractor_args") or {})
        extractor_args["youtube"] = {"player_client": [client]}
        attempt["extractor_args"] = extractor_args
    catcher = _ErrorCatcher()
    attempt["logger"] = catcher
    ydl = yt_dlp.YoutubeDL(attempt)
    try:
        info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        return None, ydl, _tidy_error(exc)
    if info is None:
        return None, ydl, catcher.last
    # `ignoreerrors` faz o yt-dlp registrar a falha e devolver `info` assim
    # mesmo. Sem conferir o arquivo, "deu certo" era o que o app ouvia quando o
    # download tinha respondido 403 -- e ele cadastrava no acervo uma faixa
    # apontando para um caminho vazio, que some da tela como "arquivo sumido" e
    # deixa a playlist criada sem música nenhuma dentro. Também é o que faz a
    # troca de cliente valer: sem isto o primeiro cliente que *resolve* o vídeo
    # encerra a busca, mesmo que não consiga baixar um byte.
    if not _arquivo_que_saiu(info):
        return None, ydl, catcher.last or "O download não gerou arquivo nenhum."
    return info, ydl, ""


def _arquivo_que_saiu(info: Dict[str, Any]) -> str:
    """O caminho que o download realmente escreveu, ou "" se não escreveu."""
    for pedido in (info or {}).get("requested_downloads") or []:
        caminho = pedido.get("filepath") or pedido.get("_filename") or ""
        if caminho and os.path.exists(caminho):
            return caminho
    caminho = (info or {}).get("filepath") or (info or {}).get("_filename") or ""
    return caminho if caminho and os.path.exists(caminho) else ""


#: Quanto do tempo de um item é o download. O resto é o pós: extrair o áudio e
#: cortar os trechos marcados -- num Pi 3B isso não é um detalhe, é metade da
#: espera. Uma barra que chega a 100% e fica lá parada mente tanto quanto uma
#: que fica em 0%.
DOWNLOAD_SHARE = 0.7

#: Nem toda atualização vale uma escrita no SQLite de um cartão SD.
PROGRESS_STEP = 0.01
PROGRESS_EVERY_S = 1.0

#: O que aparece na tela em cada fase. Sem isto, meia hora de espera é meia hora
#: sem saber se está baixando, convertendo ou travado.
FASES = {
    "download": "Baixando",
    "FFmpegExtractAudio": "Separando o áudio",
    "SponsorBlock": "Procurando propaganda para cortar",
    "ModifyChapters": "Cortando a propaganda",
    "EmbedThumbnail": "Guardando a capa",
    "FFmpegMetadata": "Escrevendo os dados da música",
}


class _Andamento(object):
    """Traduz os avisos do yt-dlp em progresso e em uma frase.

    O progresso do trabalho era contado por item concluído: um vídeo só quer
    dizer ``total = 1``, e ``(0 + 1) / 1`` só acontece no fim -- a barra ficava
    em 0% do começo ao fim do download e pulava para 100% no último instante.
    Aqui ela anda com os bytes que chegaram, dentro da fatia do item.
    """

    def __init__(self, db, job_id, on_progress=None, is_cancelled=None):
        self.db = db
        self.job_id = job_id
        self.on_progress = on_progress
        self.is_cancelled = is_cancelled
        self.index = 0
        self.total = 1
        self.fracao_do_item = 0.0
        self._ultimo_valor = -1.0
        self._ultimo_instante = 0.0
        self._ultima_pergunta = 0.0

    def _parar_se_pedido(self):
        """Cancelar valia só entre itens: um vídeo só nunca chegava lá.

        ``run_job`` pergunta antes de cada item, o que resolve uma playlist e
        não resolve nada num link só -- que é o caso comum. O yt-dlp para na
        hora se um gancho levantar ``DownloadCancelled``, e é o único ponto de
        dentro do download onde dá para perguntar.
        """
        if self.is_cancelled is None:
            return
        agora = time.time()
        # Uma consulta ao banco por pacote recebido seria pior que o problema.
        if agora - self._ultima_pergunta < 1.0:
            return
        self._ultima_pergunta = agora
        if self.is_cancelled():
            from yt_dlp.utils import DownloadCancelled

            raise DownloadCancelled("cancelado por quem pediu")

    def _publicar(self, fracao, frase, forcar=False):
        fracao = max(0.0, min(1.0, fracao))
        valor = round((self.index + fracao) / float(self.total or 1), 3)
        agora = time.time()
        if not forcar and (
            abs(valor - self._ultimo_valor) < PROGRESS_STEP
            and (agora - self._ultimo_instante) < PROGRESS_EVERY_S
        ):
            return
        self._ultimo_valor = valor
        self._ultimo_instante = agora
        _update_job(self.db, self.job_id, progress=valor, message=frase)
        if self.on_progress is not None:
            self.on_progress({"job_id": self.job_id, "progress": valor,
                              "index": self.index, "message": frase})

    # -- ganchos do yt-dlp ------------------------------------------------
    def no_download(self, d):
        self._parar_se_pedido()
        estado = d.get("status")
        if estado == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            feito = d.get("downloaded_bytes") or 0
            # Sem tamanho anunciado (live, alguns fragmentados) não dá para
            # inventar uma porcentagem: a frase carrega o que se sabe.
            if not total:
                self._publicar(0.0, "%s… %s" % (FASES["download"], _tamanho(feito)))
                return
            parte = (feito / float(total)) * DOWNLOAD_SHARE
            self._publicar(parte, "%s %d%%" % (FASES["download"], round(feito * 100.0 / total)))
        elif estado == "finished":
            self._publicar(DOWNLOAD_SHARE, FASES["FFmpegExtractAudio"], forcar=True)
        elif estado == "error":
            self._publicar(self._ultimo_valor, "Erro no download", forcar=True)

    def no_pos(self, d):
        self._parar_se_pedido()
        if d.get("status") != "started":
            return
        nome = str(d.get("postprocessor") or "")
        frase = FASES.get(nome, nome or "Terminando")
        # O pós ocupa o que sobra da fatia do item; sem saber quanto falta, o
        # meio-termo é honesto: andou, e ainda não acabou.
        self._publicar(DOWNLOAD_SHARE + (1.0 - DOWNLOAD_SHARE) / 2.0, frase, forcar=True)


def _tamanho(bytes_):
    for unidade in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024 or unidade == "GB":
            return "%.0f %s" % (bytes_, unidade)
        bytes_ /= 1024.0
    return "%.0f GB" % bytes_


#: O que o yt-dlp diz quando o cliente respondeu mas não tem faixa de áudio
#: separada para oferecer -- diferente de ter sido recusado, e a diferença
#: decide quem vale a pena tentar de novo na segunda passada.
SEM_FORMATO = "format is not available"

#: Só faixa de áudio de verdade, sem queda nenhuma: é o que a primeira passada
#: exige de *todos* os clientes antes de qualquer um baixar vídeo.
FORMATO_SO_AUDIO = "bestaudio[vcodec=none]"


def download_entry(yt_dlp: Any, opts: Dict[str, Any], url: str,
                   clients: Optional[Iterable[str]] = None,
                   on_attempt: Optional[Any] = None):
    """Download ``url``, falling back through the player clients.

    Duas passadas, e a ordem importa: a primeira exige faixa de áudio de
    verdade em *todos* os clientes; só se nenhum tiver é que a segunda aceita
    o formato progressivo, que é o vídeo inteiro. Numa passada só, o primeiro
    cliente que responde decide -- e se ele só oferece progressivo (o `android`
    oferece), baixa-se o filme mesmo quando o cliente seguinte tinha o áudio
    separado guardado. Medido num vídeo de 10 minutos: 24 MB de mp4 contra
    3,6 MB de áudio.

    Returns ``(info, ydl, error)``; ``info`` is None when every client failed,
    and ``error`` is the first real sentence YouTube gave -- not a guess.
    """
    lista = list(clients if clients is not None else PLAYER_CLIENTS)
    first_error = ""
    responderam = []  # type: List[str]

    for numero, client in enumerate(lista):
        if on_attempt is not None:
            on_attempt(numero + 1, len(lista), True)
        info, ydl, error = _download_once(
            yt_dlp, dict(opts, format=FORMATO_SO_AUDIO), url, client)
        if info is not None:
            return info, ydl, ""
        if error and SEM_FORMATO in error:
            # Respondeu; só não tinha áudio puro. É candidato da segunda passada.
            responderam.append(client)
        elif error and not first_error:
            first_error = error

    # Segunda passada: aceita o progressivo. Quem foi recusado na primeira será
    # recusado de novo, então só se tenta de novo quem respondeu -- e, se
    # ninguém respondeu, a lista inteira, para nunca ficar pior que antes.
    segunda = responderam or lista
    for numero, client in enumerate(segunda):
        if on_attempt is not None:
            on_attempt(numero + 1, len(segunda), False)
        info, ydl, error = _download_once(yt_dlp, opts, url, client)
        if info is not None:
            return info, ydl, ""
        if error and not first_error:
            first_error = error
    return None, None, first_error


def _iter_entries(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries = info.get("entries")
    if entries is None:
        return [info] if info else []
    return [e for e in entries if e]


def preview(url: str) -> Dict[str, Any]:
    """A flat listing of what ``url`` points at, without downloading anything.

    Used by ``GET /import/preview`` so the UI can show "47 tracks found"
    before committing to a long download -- ``extract_flat`` is exactly the
    knob yt-dlp offers for that.
    """
    yt_dlp = _yt_dlp()
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "extract_flat": "in_playlist", "skip_download": True,
    }
    # A mesma troca de player client que o download faz. Sem ela, o "espiar" --
    # que é por onde passa "criar playlist a partir deste link", para dar nome à
    # playlist -- caía em "The page needs to be reloaded." e derrubava a
    # importação inteira antes do primeiro byte, num link que o download baixaria
    # sem reclamar. Medido nesta máquina: o cliente padrão recusou e o `android`
    # leu o mesmo vídeo.
    ultimo = None
    for client in PLAYER_CLIENTS:
        tentativa = dict(opts)
        if client:
            tentativa["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with yt_dlp.YoutubeDL(tentativa) as ydl:
                info = ydl.extract_info(url, download=False)
            if info is not None:
                break
        except yt_dlp.utils.DownloadError as exc:
            ultimo = exc
            info = None
    if info is None:
        raise SourceError(
            "Could not read %s" % url, code="preview_failed",
            hint=_tidy_error(ultimo) if ultimo is not None else "",
        )
    entries = _iter_entries(info or {})
    is_playlist = bool((info or {}).get("entries") is not None)
    items = [
        {
            "id": e.get("id", ""),
            "title": e.get("title", "") or e.get("id", ""),
            "duration": float(e.get("duration") or 0.0),
            "uploader": e.get("uploader", ""),
            "thumbnail": e.get("thumbnail", ""),
        }
        for e in entries
    ]
    return {
        "kind": "playlist" if is_playlist else "video",
        "title": (info or {}).get("title", "") or (items[0]["title"] if items else url),
        "count": len(items),
        "items": items,
    }


def create_job(
    db: Any,
    url: str,
    kind: str = "video",
    playlist_id: Optional[str] = None,
    title: str = "",
) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO app_birdtunes_imports "
        "(id, kind, url, title, state, progress, total, completed, message, playlist_id, created_at) "
        "VALUES (?, ?, ?, ?, 'queued', 0, 0, 0, '', ?, ?)",
        (job_id, kind, url, title, playlist_id, utcnow_iso()),
    )
    return get_job(db, job_id) or {}


def _com_fracao_do_item(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Acrescenta ``item_progress``: quanto falta do que está baixando agora.

    ``progress`` é do trabalho inteiro -- útil quando são doze músicas, inútil
    quando é uma. Quem está olhando quer saber quanto falta *desta*, e a conta
    já está guardada: ``progress`` é ``(item + fração) / total`` e ``completed``
    é quantos acabaram, então a fração do item de agora sai da diferença.
    Derivado, não gravado: uma coluna a mais precisaria de migração no banco de
    quem já tem o app instalado.
    """
    if not job:
        return job
    total = int(job.get("total") or 0) or 1
    feitos = int(job.get("completed") or 0)
    fracao = (float(job.get("progress") or 0.0) * total) - feitos
    job["item_progress"] = round(max(0.0, min(1.0, fracao)), 3)
    return job


def get_job(db: Any, job_id: str) -> Optional[Dict[str, Any]]:
    row = db.one("SELECT * FROM app_birdtunes_imports WHERE id = ?", (job_id,))
    from project_os.db import row_to_dict

    return _com_fracao_do_item(row_to_dict(row))


def job_in_flight(db: Any, url: str) -> Optional[Dict[str, Any]]:
    """Um trabalho ainda rodando para este mesmo endereço, se houver.

    Dois downloads do mesmo vídeo escrevem no mesmo arquivo -- o ``outtmpl`` é
    montado a partir do título e do id. O segundo tenta renomear o que o
    primeiro já renomeou e morre com *"No such file or directory: ... .webm ->
    ..."*, que foi o que apareceu no Pi dele depois de clicar em Trazer três
    vezes no mesmo link. E mesmo dando certo seria trabalho jogado fora: a
    faixa tem id derivado do vídeo, então a segunda cópia sobrescreve a
    primeira.
    """
    from project_os.db import row_to_dict

    row = db.one(
        "SELECT * FROM app_birdtunes_imports WHERE url = ? AND state IN ('queued', 'running') "
        "ORDER BY created_at DESC LIMIT 1",
        (url,),
    )
    return row_to_dict(row)


def list_jobs(db: Any, limit: int = 50) -> List[Dict[str, Any]]:
    from project_os.db import rows_to_dicts

    return [
        _com_fracao_do_item(j)
        for j in rows_to_dicts(
            db.query("SELECT * FROM app_birdtunes_imports ORDER BY created_at DESC LIMIT ?", (limit,))
        )
    ]


def _update_job(db: Any, job_id: str, **fields: Any) -> None:
    sets = []  # type: List[str]
    params = []  # type: List[Any]
    for key, value in fields.items():
        sets.append("%s = ?" % key)
        params.append(value)
    params.append(job_id)
    db.execute("UPDATE app_birdtunes_imports SET %s WHERE id = ?" % ", ".join(sets), params)


def cancel_job(db: Any, job_id: str) -> bool:
    job = get_job(db, job_id)
    if job is None or job["state"] not in ("queued", "running"):
        return False
    _update_job(db, job_id, state="cancelled", finished_at=utcnow_iso())
    return True


def run_job(
    db: Any,
    job_id: str,
    dest_dir: str,
    quality: str = "192",
    is_cancelled: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    clients: Optional[Iterable[str]] = None,
    skip_sponsors: bool = True,
) -> Dict[str, Any]:
    """Do the actual download, one item at a time, honestly reporting failures.

    Runs synchronously and is meant to be called from ``run_in_executor`` --
    both ``extract_info`` and the download it triggers block. ``is_cancelled``
    is polled between items so a long playlist import can be stopped midway;
    partial output from the item in flight is left for the OS temp cleanup
    yt-dlp itself does on a cancelled download.
    """
    job = get_job(db, job_id)
    if job is None:
        raise KeyError(job_id)
    os.makedirs(dest_dir, exist_ok=True)
    yt_dlp = _yt_dlp()
    # Antes do primeiro byte existe o `extract_info`, que resolve o link e, num
    # Pi com a rede ruim, leva seus segundos. Sem esta frase esse trecho é uma
    # barra em 0% sem explicação nenhuma -- que é onde a dúvida "está baixando
    # ou não?" nasce.
    _update_job(db, job_id, state="running", message="Lendo o link…")

    noplaylist = job["kind"] != "playlist"
    opts = _base_options(dest_dir, quality, noplaylist, skip_sponsors=skip_sponsors)
    catcher = _ErrorCatcher()
    opts["logger"] = catcher
    # Os ganchos do próprio yt-dlp. `noprogress` acima cala a barra do terminal
    # e não tem nada a ver com eles.
    andamento = _Andamento(db, job_id, on_progress, is_cancelled)
    opts["progress_hooks"] = [andamento.no_download]
    opts["postprocessor_hooks"] = [andamento.no_pos]
    added_tracks = []  # type: List[Dict[str, Any]]
    # Everything the URL resolved to, downloaded now or already here. Adding a
    # link you once imported to a playlist has to work: "already in the library"
    # is not a reason to leave the playlist empty.
    resolved_ids = []  # type: List[str]
    error_message = ""

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=False)
            entries = _iter_entries(info or {})
            total = len(entries) or 1
            _update_job(db, job_id, total=total, message=FASES["download"])
            andamento.total = total
            for index, entry in enumerate(entries or [info or {}]):
                andamento.index = index
                if is_cancelled is not None and is_cancelled():
                    _update_job(db, job_id, state="cancelled", finished_at=utcnow_iso())
                    return get_job(db, job_id) or {}
                video_id = entry.get("id", "")
                existing = db.one(
                    "SELECT id FROM app_birdtunes_tracks WHERE source = 'youtube' AND source_id = ?",
                    (video_id,),
                )
                if existing is not None:
                    resolved_ids.append(existing["id"])
                if existing is None:
                    # Entre pedir o vídeo e o primeiro pacote chegar podem
                    # passar dezenas de segundos: cada cliente recusado custa
                    # ~1s e um que responde e depois dá 403 custa ~8s. Medido
                    # neste vídeo: 16s de barra parada sem uma palavra, que é
                    # exatamente "não dá pra saber se está baixando".
                    def _avisar(numero, total, procurando_audio, _id=job_id):
                        _update_job(db, _id, message=(
                            "Procurando o áudio (%d de %d)…" if procurando_audio
                            else "Tentando outro jeito (%d de %d)…") % (numero, total))

                    downloaded, used_ydl, failure = download_entry(
                        yt_dlp, opts, entry.get("webpage_url") or job["url"], clients,
                        on_attempt=_avisar)
                    if downloaded is None and not error_message:
                        error_message = failure or (
                            "YouTube refused %s and gave no reason." % (entry.get("title") or video_id))
                    if downloaded is not None:
                        track = _store_downloaded(db, used_ydl, downloaded, quality)
                        if track is not None:
                            added_tracks.append(track)
                            resolved_ids.append(track["id"])
                progress = round((index + 1) / float(total), 3)
                _update_job(db, job_id, progress=progress, completed=index + 1, message="")
                if on_progress is not None:
                    on_progress({"job_id": job_id, "progress": progress, "index": index})
    except yt_dlp.utils.DownloadCancelled:
        # Veio de dentro do download, pelo gancho: a linha já está marcada como
        # cancelada por quem pediu; aqui só se fecha a hora.
        _update_job(db, job_id, state="cancelled", message="", finished_at=utcnow_iso())
        return get_job(db, job_id) or {}
    except yt_dlp.utils.DownloadError as exc:
        _update_job(db, job_id, state="error", message=str(exc), finished_at=utcnow_iso())
        return get_job(db, job_id) or {}
    except Exception as exc:  # pragma: no cover - yt-dlp internals are not ours to predict
        _update_job(db, job_id, state="error", message=str(exc), finished_at=utcnow_iso())
        return get_job(db, job_id) or {}

    added_to_playlist = 0
    if job.get("playlist_id") and resolved_ids:
        added_to_playlist = library.add_tracks_to_playlist(db, job["playlist_id"], resolved_ids)

    # "done" has to mean something arrived. yt-dlp runs with ignoreerrors, so a
    # video that YouTube refused leaves an empty result and no exception -- and
    # the job used to report success with "Nothing new to import.", which reads
    # like "you already had it" and is exactly the wrong thing when the download
    # was blocked. Nothing new *and* an error is an error.
    if not added_tracks and error_message:
        _update_job(db, job_id, state="error", message=error_message, finished_at=utcnow_iso())
        return get_job(db, job_id) or {}

    if added_tracks:
        message = ""
    elif added_to_playlist:
        message = "Already in the library -- added to the playlist."
    elif resolved_ids:
        message = "Already in the library."
    else:
        message = "Nothing new to import."
    _update_job(db, job_id, state="done", message=message, finished_at=utcnow_iso())
    return get_job(db, job_id) or {}


def _store_downloaded(db: Any, ydl: Any, info: Dict[str, Any], quality: str) -> Optional[Dict[str, Any]]:
    video_id = info.get("id", "")
    if not video_id:
        return None
    # O caminho que o yt-dlp diz ter escrito vale mais que o que dá para
    # adivinhar do modelo: depois dos pós-processadores ele já aponta para o
    # arquivo final. Adivinhar a extensão acertava no caso comum e cadastrava
    # uma faixa apontando para o vazio quando errava.
    filename = _arquivo_que_saiu(info)
    if not filename:
        filename = ydl.prepare_filename(info)
        if ffmpeg_available():
            # FFmpegExtractAudio rewrites the extension after the postprocessor runs.
            filename = os.path.splitext(filename)[0] + ".mp3"
    container, codec = library.container_codec_for(filename)
    track_id = uuid.uuid5(uuid.NAMESPACE_URL, "birdtunes:youtube:%s" % video_id).hex
    duration = float(info.get("duration") or 0.0)
    title = info.get("title", "") or video_id
    artist = info.get("uploader", "") or ""
    db.execute(
        "INSERT INTO app_birdtunes_tracks "
        "(id, path, source, source_id, source_url, title, artist, duration, container, codec, "
        " thumbnail, added_at, missing) "
        "VALUES (?, ?, 'youtube', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(path) DO NOTHING",
        (
            track_id, filename, video_id, info.get("webpage_url", ""), title, artist,
            duration, container, codec, info.get("thumbnail", ""), utcnow_iso(),
        ),
    )
    db.execute("INSERT OR IGNORE INTO app_birdtunes_feedback (track_id) VALUES (?)", (track_id,))
    return library.get_track(db, track_id)


__all__ = [
    "IMPORT_STATES",
    "INSTALL_HINT",
    "SourceError",
    "available",
    "cancel_job",
    "create_job",
    "convert_to_mp3",
    "ffmpeg_available",
    "get_job",
    "list_jobs",
    "preview",
    "run_job",
    "youtube_video_id",
]
