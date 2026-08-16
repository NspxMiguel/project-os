"""Baixar áudio baixava o vídeo inteiro, e mais quatro coisas do mesmo print.

* *"e tbm, ele ta baixando o video todo pela demora, baixa so o audio ne"*
* *"n tem botao de cancelar tbm e etc"*
* *"pelo jeito ele n tem fila de downloads tbm"*
* *"quero q mostre a porcentagem do video nao playlist"*
* *"player ta estatico, n se move"*

**O áudio.** ``bestaudio/best`` parece pedir áudio e não pede. Quando o cliente
do YouTube só devolve formatos progressivos -- áudio *dentro* do vídeo --,
``bestaudio`` casa com um deles e o que desce é o filme. E era o caso: dos seis
clientes que o código tentava, cinco eram recusados ("The page needs to be
reloaded.") e o que respondia, ``android``, devolvia cinco formatos e nenhum de
áudio puro. Medido nos três vídeos do teste, incluindo o que ele mandou:

======================  ==================  ================
vídeo                   áudio (android_vr)  hoje (android)
======================  ==================  ================
Zelda (o do print)      68,8 MB             180,1 MB
Me at the zoo           0,3 MB              0,6 MB
Charge                  14,4 MB             33,7 MB
======================  ==================  ================

Num Pi 3B, os 111 MB a mais são baixados, decodificados e jogados fora para
sobrar um mp3. Era boa parte da meia hora.

**O cancelar.** A rota ``DELETE /import/{id}`` existia desde sempre; o botão,
não. E ``cancel_job`` marcava a linha, enquanto ``run_job`` só olhava isso
*entre* itens -- num link só, nunca. Agora o próprio gancho de progresso
pergunta, e o yt-dlp para na hora.

**A fila.** Três links colados viravam três downloads simultâneos dividindo a
banda e três ffmpeg disputando quatro núcleos lentos. Um de cada vez termina o
primeiro antes.

**A porcentagem.** Era a do trabalho inteiro. Com uma música só, isso é 0% até
o fim; com doze, não responde "quanto falta desta".

**O tocador.** A posição só mudava quando o ``/status`` respondia, de 8 em 8
segundos. O número estava certo; a tela é que ficava parada entre uma resposta
e outra.
"""

from __future__ import annotations

import io
import os

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTE = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "sources.py")
APP = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "app.py")
PAINEL = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.js")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")


def _ler(caminho):
    return io.open(caminho, encoding="utf-8").read()


# --------------------------------------------------------------------------- só o áudio


def test_o_seletor_exige_faixa_de_audio_de_verdade():
    from project_os.apps.birdtunes.sources import _base_options

    opts = _base_options("/tmp", "192", True)
    assert opts["format"].startswith("bestaudio[vcodec=none]"), opts["format"]


def test_mas_nao_desiste_de_baixar_quando_nao_existe_audio_puro():
    """Um vídeo sem faixa separada ainda tem que entrar no acervo."""
    from project_os.apps.birdtunes.sources import _base_options

    seletor = _base_options("/tmp", "192", True)["format"]
    assert seletor.count("/") >= 2, seletor
    assert seletor.endswith("best")


def test_a_faixa_escolhida_e_a_mais_perto_da_taxa_que_o_mp3_vai_ter():
    """``bestaudio`` quer dizer a de maior taxa, e isso passa longe do alvo.

    A tabela de verdade de um vídeo de 10 minutos, pelo cliente que oferece
    áudio separado: 48, 50, 129, 130, 195 e 387 kbps -- de 3,6 MB a 28,9 MB.
    Para gerar um mp3 de 192, baixar os 28,9 MB é jogar metade fora na
    conversão. Medido depois desta ordenação: 14,5 MB, a faixa de 195 kbps.
    """
    from project_os.apps.birdtunes.sources import _base_options, _perto_da_taxa

    assert _perto_da_taxa("192") == ["abr~192"]
    assert _perto_da_taxa("320") == ["abr~320"], "quem pede 320 leva a faixa grande"
    assert _perto_da_taxa("") == [] and _perto_da_taxa("melhor") == []
    assert _base_options("/tmp", "192", True)["format_sort"] == ["abr~192"]


def test_o_primeiro_cliente_tentado_e_o_que_oferece_audio_separado():
    from project_os.apps.birdtunes.sources import PLAYER_CLIENTS

    assert PLAYER_CLIENTS[0] == "android_vr", PLAYER_CLIENTS
    assert "android" in PLAYER_CLIENTS, "o que respondia antes continua na lista"


# --------------------------------------------------------------------------- duas passadas


def _fake_de_clientes(tmp_path, quem_tem_audio, seen):
    """Um YouTube de mentira onde só ``quem_tem_audio`` oferece faixa separada.

    Os outros respondem com formatos progressivos, que é o caso do ``android``
    medido de verdade: 4 formatos, nenhum de áudio puro.
    """
    from project_os.apps.birdtunes import sources

    class FakeYDL(object):
        def __init__(self, opts):
            self.opts = opts
            cliente = ((opts.get("extractor_args") or {}).get("youtube") or {}).get("player_client")
            self.client = cliente[0] if cliente else ""
            self.estrito = opts.get("format") == sources.FORMATO_SO_AUDIO
            self.logger = opts.get("logger")
            seen.append((self.client, self.estrito))

        def extract_info(self, url, download=False):
            if self.client == quem_tem_audio:
                escrito = tmp_path / (self.client + ".m4a")
                escrito.write_bytes(b"audio")
                return {"id": "x", "requested_downloads": [{"filepath": str(escrito)}]}
            if self.estrito:
                self.logger.error("ERROR: [youtube] x: Requested format is not available.")
                return None
            escrito = tmp_path / (self.client + ".mp4")  # o filme inteiro
            escrito.write_bytes(b"video" * 1000)
            return {"id": "x", "requested_downloads": [{"filepath": str(escrito)}]}

    return type("m", (), {"YoutubeDL": FakeYDL, "utils": type("u", (), {"DownloadError": Exception})})


def test_ninguem_baixa_video_antes_de_todos_tentarem_o_audio(tmp_path):
    """O primeiro que responde não decide.

    O ``android`` responde rápido e só oferece progressivo. Numa passada só,
    era ele quem ganhava -- 24 MB de mp4 num vídeo de 10 minutos, contra os
    3,6 MB de áudio que outro cliente tinha guardado.
    """
    from project_os.apps.birdtunes import sources

    seen = []
    fake = _fake_de_clientes(tmp_path, "web_safari", seen)
    info, _ydl, erro = sources.download_entry(fake, {"format": "bestaudio/best"}, "https://y/x")

    assert erro == ""
    assert info["requested_downloads"][0]["filepath"].endswith("web_safari.m4a")
    assert all(estrito for _c, estrito in seen), "ninguém chegou a aceitar vídeo"


def test_e_quando_ninguem_tem_audio_o_video_serve():
    """Desistir seria pior: o pedido é ouvir o passarinho, não ganhar a discussão."""
    from project_os.apps.birdtunes import sources
    import tempfile

    pasta = tempfile.mkdtemp()
    seen = []
    fake = _fake_de_clientes(__import__("pathlib").Path(pasta), "ninguem", seen)
    info, _ydl, erro = sources.download_entry(fake, {"format": "bestaudio/best"}, "https://y/x")

    assert erro == "" and info is not None
    assert info["requested_downloads"][0]["filepath"].endswith(".mp4")
    assert seen[0][1] is True, "mas a primeira passada foi a exigente"
    assert any(not estrito for _c, estrito in seen), "e a segunda foi a que aceitou"


def test_a_segunda_passada_nao_repete_quem_recusou(tmp_path):
    """Quem recusou vai recusar de novo; repetir só custa segundos ao vivo."""
    from project_os.apps.birdtunes import sources

    seen = []

    class FakeYDL(object):
        def __init__(self, opts):
            cliente = ((opts.get("extractor_args") or {}).get("youtube") or {}).get("player_client")
            self.client = cliente[0] if cliente else ""
            self.estrito = opts.get("format") == sources.FORMATO_SO_AUDIO
            self.logger = opts.get("logger")
            seen.append((self.client, self.estrito))

        def extract_info(self, url, download=False):
            if self.client == "android":  # responde, mas só com progressivo
                if self.estrito:
                    self.logger.error("ERROR: [youtube] x: Requested format is not available.")
                    return None
                escrito = tmp_path / "android.mp4"
                escrito.write_bytes(b"video")
                return {"id": "x", "requested_downloads": [{"filepath": str(escrito)}]}
            self.logger.error("ERROR: [youtube] x: The page needs to be reloaded.")
            return None

    fake = type("m", (), {"YoutubeDL": FakeYDL, "utils": type("u", (), {"DownloadError": Exception})})
    info, _ydl, erro = sources.download_entry(fake, {"format": "bestaudio/best"}, "https://y/x")

    assert erro == "" and info is not None
    segunda = [cliente for cliente, estrito in seen if not estrito]
    assert segunda == ["android"], segunda


def test_a_tela_ouve_cada_tentativa():
    """Sem isto a barra fica parada, sem número, enquanto os clientes falham."""
    from project_os.apps.birdtunes import sources

    avisos = []

    class FakeYDL(object):
        def __init__(self, opts):
            self.logger = opts.get("logger")

        def extract_info(self, url, download=False):
            self.logger.error("ERROR: [youtube] x: The page needs to be reloaded.")
            return None

    fake = type("m", (), {"YoutubeDL": FakeYDL, "utils": type("u", (), {"DownloadError": Exception})})
    sources.download_entry(fake, {}, "https://y/x", clients=["a", "b"],
                           on_attempt=lambda n, total, audio: avisos.append((n, total, audio)))

    assert avisos[:2] == [(1, 2, True), (2, 2, True)]
    assert (1, 2, False) in avisos, "e a segunda passada também se anuncia"


def test_o_trabalho_conta_isso_na_mensagem():
    fonte = _ler(FONTE)
    assert "Procurando o áudio (%d de %d)…" in fonte
    assert "Tentando outro jeito (%d de %d)…" in fonte
    assert "on_attempt=_avisar" in fonte


# --------------------------------------------------------------------------- o arquivo fantasma


def test_um_download_que_nao_escreveu_arquivo_nao_conta_como_sucesso(tmp_path):
    """``ignoreerrors`` devolve ``info`` mesmo depois de um 403.

    Medido: com o cliente que oferece áudio separado, a URL do formato responde
    403 em alguns vídeos. O yt-dlp registra o erro, devolve ``info``, e o app
    cadastrava no acervo uma faixa apontando para um caminho vazio -- que na
    tela vira "arquivo sumido", e numa importação com playlist vira a playlist
    criada e vazia. É também o que impedia a troca de cliente de acontecer: quem
    *resolve* o vídeo encerrava a busca, mesmo sem baixar um byte.
    """
    from project_os.apps.birdtunes import sources

    tentados = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            cliente = ((opts.get("extractor_args") or {}).get("youtube") or {}).get("player_client")
            self.client = cliente[0] if cliente else ""
            tentados.append(self.client)

        def extract_info(self, url, download=False):
            # O primeiro "consegue" sem escrever nada, como o 403 de verdade.
            if self.client == "android_vr":
                return {"id": "x", "requested_downloads": [{"filepath": str(tmp_path / "nao-existe.webm")}]}
            escrito = tmp_path / "existe.m4a"
            escrito.write_bytes(b"audio")
            return {"id": "x", "requested_downloads": [{"filepath": str(escrito)}]}

    fake = type("m", (), {"YoutubeDL": FakeYDL, "utils": type("u", (), {"DownloadError": Exception})})
    info, _ydl, erro = sources.download_entry(fake, {}, "https://youtu.be/x")

    assert info is not None and erro == ""
    assert tentados[0] == "android_vr", tentados
    assert len(tentados) > 1, "o primeiro não baixou nada e a busca parou nele"


def test_e_quando_nenhum_cliente_escreve_o_erro_diz_isso(tmp_path):
    from project_os.apps.birdtunes import sources

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def extract_info(self, url, download=False):
            return {"id": "x", "requested_downloads": [{"filepath": str(tmp_path / "nada.webm")}]}

    fake = type("m", (), {"YoutubeDL": FakeYDL, "utils": type("u", (), {"DownloadError": Exception})})
    info, _ydl, erro = sources.download_entry(fake, {}, "https://youtu.be/x")
    assert info is None
    assert "arquivo" in erro.lower(), erro


def test_o_acervo_guarda_o_caminho_que_o_yt_dlp_escreveu(db, tmp_path):
    """No Pi tem ffmpeg, e o arquivo final é o mp3 que o pós-processador criou.

    ``FFmpegExtractAudioPP`` reescreve ``filepath`` (ffmpeg.py:524) e o
    ``replace_info_dict`` leva isso até ``requested_downloads``, então o nome
    certo está ali. Adivinhar trocando a extensão do modelo acerta no caso
    comum; quando erra, cadastra uma faixa apontando para o vazio -- que na
    tela é a faixa sumida, e numa playlist é a playlist criada e vazia.
    """
    from project_os.apps.birdtunes import library, sources

    library.register_schema(db)
    final = tmp_path / "Passaros [abc].mp3"
    final.write_bytes(b"mp3")

    class FakeYDL(object):
        def prepare_filename(self, info):
            return str(tmp_path / "Passaros [abc].webm")  # o que o modelo diria

    faixa = sources._store_downloaded(
        db, FakeYDL(),
        {"id": "abc", "title": "Passaros", "duration": 1.0,
         "requested_downloads": [{"filepath": str(final)}]},
        "192",
    )
    assert faixa["path"] == str(final)


def test_e_cai_no_modelo_so_quando_o_yt_dlp_nao_diz_nada(db, tmp_path):
    """Sem essa queda, uma versão do yt-dlp que não preencha o campo pararia tudo."""
    from project_os.apps.birdtunes import library, sources

    library.register_schema(db)
    faixa = sources._store_downloaded(
        db, type("y", (), {"prepare_filename": lambda self, i: str(tmp_path / "x.webm")})(),
        {"id": "def", "title": "Sem campo", "duration": 1.0}, "192",
    )
    assert faixa["path"].startswith(str(tmp_path))


def test_o_caminho_conferido_e_o_que_o_yt_dlp_diz_ter_escrito(tmp_path):
    from project_os.apps.birdtunes.sources import _arquivo_que_saiu

    existe = tmp_path / "a.m4a"
    existe.write_bytes(b"x")
    assert _arquivo_que_saiu({"requested_downloads": [{"filepath": str(existe)}]}) == str(existe)
    assert _arquivo_que_saiu({"filepath": str(existe)}) == str(existe)
    assert _arquivo_que_saiu({"requested_downloads": [{"filepath": str(tmp_path / "b")}]}) == ""
    assert _arquivo_que_saiu({}) == ""
    assert _arquivo_que_saiu(None) == ""


# --------------------------------------------------------------------------- cancelar


class _BancoFalso(object):
    def __init__(self):
        self.escritas = []

    def execute(self, sql, params=None):
        self.escritas.append((sql, tuple(params or ())))


def test_cancelar_para_no_meio_do_download():
    """Entre itens não serve: um link só nunca chega ao 'entre'."""
    import pytest
    from yt_dlp.utils import DownloadCancelled

    from project_os.apps.birdtunes.sources import _Andamento

    a = _Andamento(_BancoFalso(), "job1", None, is_cancelled=lambda: True)
    with pytest.raises(DownloadCancelled):
        a.no_download({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 100})


def test_e_nao_pergunta_ao_banco_a_cada_pacote():
    quantas = []
    from project_os.apps.birdtunes.sources import _Andamento

    a = _Andamento(_BancoFalso(), "job1", None, is_cancelled=lambda: (quantas.append(1), False)[1])
    for feito in range(0, 500):
        a.no_download({"status": "downloading", "downloaded_bytes": feito, "total_bytes": 1000})
    assert len(quantas) <= 3, "%d perguntas ao banco num download só" % len(quantas)


def test_o_trabalho_cancelado_termina_como_cancelado():
    fonte = _ler(FONTE)
    assert "except yt_dlp.utils.DownloadCancelled:" in fonte
    trecho = fonte[fonte.index("except yt_dlp.utils.DownloadCancelled:"):]
    assert 'state="cancelled"' in trecho[:400]


def test_a_tela_tem_o_botao():
    painel = _ler(PAINEL)
    assert "t2('bt.import.cancel')" in painel
    assert "appApi.del('/import/'" in painel
    assert "'bt.import.cancel':" in _ler(PT)


# --------------------------------------------------------------------------- a fila


def test_um_download_por_vez():
    app = _ler(APP)
    assert "self._fila_de_downloads = asyncio.Semaphore(1)" in app
    assert "async with self._fila_de_downloads:" in app


def test_a_fila_nao_engole_o_trabalho_de_verdade():
    """A trava é só a porta; quem baixa continua sendo _importar_agora."""
    app = _ler(APP)
    assert "await self._importar_agora(" in app
    assert "async def _importar_agora(" in app


# --------------------------------------------------------------------------- a porcentagem do vídeo


def test_a_fracao_do_item_sai_do_que_ja_esta_guardado():
    """Sem coluna nova: quem já tem o app instalado não precisa de migração."""
    from project_os.apps.birdtunes.sources import _com_fracao_do_item

    assert _com_fracao_do_item(
        {"progress": 0.625, "total": 4, "completed": 2})["item_progress"] == 0.5
    assert _com_fracao_do_item(
        {"progress": 0.42, "total": 1, "completed": 0})["item_progress"] == 0.42


def test_ela_nunca_sai_de_zero_a_um():
    from project_os.apps.birdtunes.sources import _com_fracao_do_item

    assert _com_fracao_do_item({"progress": 2.0, "total": 1, "completed": 0})["item_progress"] == 1.0
    assert _com_fracao_do_item({"progress": 0.0, "total": 0, "completed": 5})["item_progress"] == 0.0
    assert _com_fracao_do_item(None) is None


def test_a_barra_da_tela_usa_a_do_item_e_o_contador_fica_ao_lado():
    painel = _ler(PAINEL)
    assert "job.item_progress" in painel
    assert "fmtStr('bt.import.of'" in painel, "'3 de 12' responde outra pergunta, e as duas cabem"
    assert "'bt.import.of':" in _ler(PT)


# --------------------------------------------------------------------------- o tocador anda


def test_o_tocador_conta_o_tempo_entre_uma_resposta_e_outra():
    painel = _ler(PAINEL)
    assert "function posicaoAgora()" in painel
    trecho = painel[painel.index("function posicaoAgora()"):]
    trecho = trecho[:trecho.index("\n    function progress()")]
    assert "state.statusEm" in trecho
    assert "status.state !== 'playing'" in trecho, "parado não pode andar sozinho"


def test_e_ressincroniza_quando_o_servidor_responde():
    """Contar sozinho para sempre seria inventar; isto é só o intervalo."""
    painel = _ler(PAINEL)
    assert painel.count("state.statusEm = Date.now();") >= 3, (
        "todo caminho que traz status novo tem que recarimbar a hora"
    )


def test_o_relogio_de_um_segundo_existe_e_para_junto():
    painel = _ler(PAINEL)
    assert "const relogio = setInterval(" in painel
    assert "clearInterval(relogio);" in painel, "sair da tela tem que parar o relógio"


def test_o_relogio_nao_redesenha_a_tela_inteira():
    """Um render() por segundo brigaria com quem estiver digitando na busca."""
    painel = _ler(PAINEL)
    trecho = painel[painel.index("const relogio = setInterval("):]
    trecho = trecho[:trecho.index("}, 1000);")]
    assert "renderPlayer();" in trecho
    assert "\n      render();" not in trecho
