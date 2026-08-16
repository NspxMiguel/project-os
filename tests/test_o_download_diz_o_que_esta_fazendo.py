"""A barra do download ficava em 0% do começo ao fim.

*"download bem bugado, feedback visual n funciona, demora meia hora, cria
playlist mais nao musica. vc n sabe se ta baixando ou nao"*

Três coisas separadas, e o print mostrava as três ao mesmo tempo:

* **o progresso era contado por item concluído.** ``progress = (index + 1) /
  total``, escrito *depois* de baixar. Com um vídeo só, ``total`` é 1: a conta
  só dá qualquer coisa diferente de zero no instante em que acaba. Meia hora de
  download inteira acontecia com a barra em 0%;

* **a tela não recarregava.** O servidor publica ``app.birdtunes.import`` desde
  sempre e o painel nunca ouviu esse evento; o poll de 8s só busca ``/status``.
  Quem ficasse na aba Adicionar via o valor de quando entrou, para sempre;

* **o "espiar" não trocava de player client.** Criar playlist a partir de um
  link passa por ``preview()`` para dar nome à playlist, e o preview usava o
  cliente padrão do yt-dlp -- o mesmo que responde "The page needs to be
  reloaded." e que o download já contornava trocando de cliente. Medido nesta
  máquina: o mesmo link falhava no preview e baixava no download.

E uma quarta, que é o motivo de "demora meia hora" ser insuportável em vez de
só chato: baixar é parte do trabalho. Separar o áudio e cortar a propaganda
vêm depois, num Pi 3B levam tanto quanto o download, e nada dizia em qual das
fases a caixa estava.
"""

from __future__ import annotations

import io
import os

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAINEL = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.js")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")


class _BancoFalso(object):
    """Só o suficiente de db para ver o que o andamento grava."""

    def __init__(self):
        self.escritas = []

    def execute(self, sql, params=None):
        self.escritas.append((sql, tuple(params or ())))


def _andamento(**kw):
    from project_os.apps.birdtunes.sources import _Andamento

    db = _BancoFalso()
    recado = []
    a = _Andamento(db, "job1", lambda p: recado.append(p))
    for chave, valor in kw.items():
        setattr(a, chave, valor)
    return a, db, recado


# --------------------------------------------------------------------------- a barra anda


def test_a_barra_anda_durante_o_download_de_um_video_so():
    """O caso do print dele: um link, um item, meia hora em 0%."""
    a, _db, recado = _andamento()
    a.no_download({"status": "downloading", "downloaded_bytes": 500, "total_bytes": 1000})
    assert recado, "nada foi publicado no meio do download"
    valor = recado[-1]["progress"]
    assert 0.0 < valor < 1.0, valor
    assert "Baixando" in recado[-1]["message"]
    assert "50%" in recado[-1]["message"]


def test_a_barra_nao_chega_a_100_por_cento_quando_o_download_acaba():
    """Ainda falta separar o áudio e cortar a propaganda; 100% ali seria mentira."""
    from project_os.apps.birdtunes.sources import DOWNLOAD_SHARE

    a, _db, recado = _andamento()
    a.no_download({"status": "finished"})
    assert recado[-1]["progress"] == DOWNLOAD_SHARE
    assert DOWNLOAD_SHARE < 1.0


def test_o_pos_processamento_tem_nome_na_tela():
    """"Cortando a propaganda" é uma resposta; uma barra parada não é."""
    a, _db, recado = _andamento()
    a.no_pos({"status": "started", "postprocessor": "ModifyChapters"})
    assert "Cortando" in recado[-1]["message"]
    a.no_pos({"status": "started", "postprocessor": "FFmpegExtractAudio"})
    assert "áudio" in recado[-1]["message"]


def test_um_video_sem_tamanho_anunciado_nao_inventa_porcentagem():
    a, _db, recado = _andamento()
    a.no_download({"status": "downloading", "downloaded_bytes": 3 * 1024 * 1024})
    assert "%" not in recado[-1]["message"].replace("Baixando", "")
    assert "MB" in recado[-1]["message"]


def test_numa_playlist_cada_item_ocupa_sua_fatia():
    a, _db, recado = _andamento(index=2, total=4)
    a.no_download({"status": "downloading", "downloaded_bytes": 1000, "total_bytes": 1000})
    valor = recado[-1]["progress"]
    assert 0.5 < valor < 0.75, ("o terceiro de quatro itens, quase pronto: %r" % valor)


def test_nem_toda_atualizacao_vira_escrita_no_cartao():
    """Um download rápido dispara centenas de avisos; o SD não merece isso."""
    a, db, _recado = _andamento()
    for feito in range(0, 1000, 2):  # 500 avisos, 0.2% de diferença cada
        a.no_download({"status": "downloading", "downloaded_bytes": feito, "total_bytes": 1000})
    assert len(db.escritas) < 120, "%d escritas para um download só" % len(db.escritas)
    assert db.escritas, "e nem tão pouco que a barra pare de andar"


def test_a_espera_antes_do_primeiro_byte_tambem_fala():
    """extract_info resolve o link antes de existir download para medir."""
    fonte = io.open(
        os.path.join(RAIZ, "project_os", "apps", "birdtunes", "sources.py"), encoding="utf-8"
    ).read()
    trecho = fonte[fonte.index("def run_job("):]
    assert 'message="Lendo o link…"' in trecho[:3000]


# --------------------------------------------------------------------------- a tela escuta


def _painel():
    return io.open(PAINEL, encoding="utf-8").read()


def test_a_tela_ouve_o_andamento():
    fonte = _painel()
    assert "ctx.ws.on('app.birdtunes.import'" in fonte, (
        "o servidor publicava isso desde sempre e ninguém escutava"
    )


def test_e_tem_rede_de_seguranca_sem_websocket():
    """Se o socket cair no meio de meia hora, a barra congelaria de novo."""
    fonte = _painel()
    trecho = fonte[fonte.index("const poll = setInterval("):]
    trecho = trecho[:trecho.index("\n    // O app publica")]
    assert "state.view === 'add'" in trecho
    assert "loadImports()" in trecho


def test_a_frase_da_fase_aparece_junto_da_barra():
    fonte = _painel()
    assert "job.message || t2(job.state === 'queued'" in fonte


def test_o_trabalho_na_fila_tambem_aparece():
    """"queued" ficava sem barra nenhuma -- clicou e não acontece nada."""
    assert "job.state === 'running' || job.state === 'queued'" in _painel()


def test_o_portugues_das_frases_novas():
    pt = io.open(PT, encoding="utf-8").read()
    for chave in ("bt.import.working", "bt.import.queued.wait"):
        assert "'%s':" % chave in pt, "falta o português de %s" % chave


# --------------------------------------------------------------------------- espiar


def test_espiar_troca_de_player_client_como_o_download():
    """Era por aqui que "criar playlist deste link" morria antes do primeiro byte."""
    fonte = io.open(
        os.path.join(RAIZ, "project_os", "apps", "birdtunes", "sources.py"), encoding="utf-8"
    ).read()
    trecho = fonte[fonte.index("def preview("):]
    trecho = trecho[:trecho.index("\ndef ", 10)]
    assert "for client in PLAYER_CLIENTS" in trecho
    assert "player_client" in trecho


def test_e_o_motivo_do_erro_continua_chegando_limpo():
    fonte = io.open(
        os.path.join(RAIZ, "project_os", "apps", "birdtunes", "sources.py"), encoding="utf-8"
    ).read()
    trecho = fonte[fonte.index("def preview("):]
    trecho = trecho[:trecho.index("\ndef ", 10)]
    assert "_tidy_error(ultimo)" in trecho
