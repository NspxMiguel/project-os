"""O corta-propaganda, e onde ele honestamente não alcança.

*"deve da pra bota um adblockzinho de cria na opcao tocar direto do youtube da
tv"* -- na opção de tocar direto na TV, não dá, e o teste que importa aqui é o
que garante que a tela **diz isso**. Quem busca o vídeo é a televisão, falando
com o YouTube por conta própria; o Pi manda um id de onze caracteres e sai do
caminho. Não há tráfego passando por aqui para filtrar, e bloquear por DNS não
resolve porque o YouTube serve propaganda dos mesmos domínios do vídeo.

Onde dá é no caminho que este projeto controla: o que é **baixado**. Aí entra o
SponsorBlock -- a base pública de trechos marcados por gente -- e o corte
acontece no arquivo, aqui, antes de virar faixa da biblioteca. Precisa do
ffmpeg, porque cortar pedaço do meio de um arquivo é trabalho dele; sem ffmpeg
o interruptor aparece desligado com o motivo, em vez de ligado sem efeito.

Cobre também o clique repetido que quebrou o import no Pi dele: três "Trazer" no
mesmo link, três downloads no mesmo arquivo, e o segundo morrendo com
``No such file or directory: ... .webm -> ...``.
"""

from __future__ import annotations

import io
import os

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAINEL = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.js")


# --------------------------------------------------------------------------- o corte


def test_o_corte_entra_nos_pos_processadores_na_ordem_certa(monkeypatch):
    from project_os.apps.birdtunes import sources

    # As duas metades do portão, e não só o ffmpeg: numa máquina sem yt-dlp --
    # o CI é uma delas -- forçar só o ffmpeg deixa o corte desligado pela outra
    # metade, e o teste reprova por um motivo que não é o dele.
    monkeypatch.setattr(sources, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(sources, "sponsorblock_available", lambda: (True, ""))
    opts = sources._base_options("/tmp/x", "192", True, skip_sponsors=True)
    chaves = [p["key"] for p in opts["postprocessors"]]
    assert chaves == ["SponsorBlock", "ModifyChapters", "FFmpegExtractAudio"], (
        "marcar, cortar e só então extrair o áudio -- extrair antes joga fora as marcas"
    )
    marcador = opts["postprocessors"][0]
    corte = opts["postprocessors"][1]
    assert marcador["categories"] == corte["remove_sponsor_segments"], (
        "cortar categoria que não foi marcada não corta nada"
    )


def test_desligado_nao_sobra_nada_do_sponsorblock(monkeypatch):
    from project_os.apps.birdtunes import sources

    monkeypatch.setattr(sources, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(sources, "sponsorblock_available", lambda: (True, ""))
    opts = sources._base_options("/tmp/x", "192", True, skip_sponsors=False)
    assert [p["key"] for p in opts["postprocessors"]] == ["FFmpegExtractAudio"]


def test_sem_ffmpeg_o_corte_nao_e_prometido(monkeypatch):
    from project_os.apps.birdtunes import sources

    pytest.importorskip("yt_dlp.postprocessor")
    monkeypatch.setattr(sources, "ffmpeg_available", lambda: False)
    pode, motivo = sources.sponsorblock_available()
    assert pode is False
    assert "ffmpeg" in motivo


def test_com_o_portao_fechado_nao_sobra_pos_processador_de_corte(monkeypatch):
    """Interruptor ligado sem efeito é pior que interruptor nenhum.

    Vale por qualquer das duas metades do portão -- sem ffmpeg ou sem yt-dlp --,
    e é por isso que o que se força aqui é a resposta do portão inteiro.
    """
    from project_os.apps.birdtunes import sources

    monkeypatch.setattr(sources, "sponsorblock_available", lambda: (False, "sem ffmpeg"))
    opts = sources._base_options("/tmp/x", "192", True, skip_sponsors=True)
    chaves = [p["key"] for p in opts["postprocessors"]]
    assert "SponsorBlock" not in chaves and "ModifyChapters" not in chaves
    # A extração de áudio é assunto do ffmpeg desta máquina, e não do corte.
    assert chaves in ([], ["FFmpegExtractAudio"])


def test_as_categorias_sao_de_propaganda_e_nao_de_musica():
    from project_os.apps.birdtunes import sources

    assert "sponsor" in sources.SPONSOR_CATEGORIES
    assert "selfpromo" in sources.SPONSOR_CATEGORIES
    # 'intro' e 'outro' cortariam a abertura da própria música.
    assert "intro" not in sources.SPONSOR_CATEGORIES
    assert "outro" not in sources.SPONSOR_CATEGORIES


def test_as_categorias_existem_no_yt_dlp():
    """Nome inventado aqui viraria erro de configuração no meio do download."""
    yt_dlp = pytest.importorskip("yt_dlp")
    from yt_dlp.postprocessor import SponsorBlockPP

    from project_os.apps.birdtunes import sources

    validas = set(SponsorBlockPP.CATEGORIES)
    assert set(sources.SPONSOR_CATEGORIES) <= validas


def test_o_estado_sai_no_compat(auth_client):
    corpo = auth_client.get("/api/apps/birdtunes/compat").json()
    estado = corpo["sponsorblock"]
    assert set(["enabled", "available", "reason", "categories"]) <= set(estado)
    assert estado["enabled"] is True, "vem ligado: quem trouxe uma música não quer a propaganda"


def test_o_interruptor_e_do_app_e_grava(auth_client):
    base = "/api/apps/birdtunes"
    resposta = auth_client.put(base + "/config", json={"import.youtube.skip_sponsors": False})
    assert resposta.status_code == 200, resposta.text
    assert auth_client.get(base + "/compat").json()["sponsorblock"]["enabled"] is False


def test_o_manifesto_declara_a_chave():
    import json

    caminho = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "manifest.json")
    schema = json.load(io.open(caminho, encoding="utf-8"))["config_schema"]
    campo = [c for c in schema if c["key"] == "import.youtube.skip_sponsors"]
    assert campo and campo[0]["type"] == "boolean" and campo[0]["default"] is True


# --------------------------------------------------------------------------- a tela


def _painel():
    return io.open(PAINEL, encoding="utf-8").read()


def test_a_tela_mostra_o_interruptor_e_o_motivo():
    fonte = _painel()
    assert "function adblockCard()" in fonte
    assert "import.youtube.skip_sponsors" in fonte
    assert "estado.available" in fonte and "estado.reason" in fonte, (
        "interruptor ligado que não faz nada é pior que não ter interruptor"
    )


def test_a_tela_diz_que_na_TV_nao_da():
    """A parte honesta do pedido: dizer onde o adblock não alcança."""
    fonte = _painel()
    assert "bt.adblock.tv" in fonte
    pt = io.open(os.path.join(RAIZ, "web", "lib", "strings-pt.js"), encoding="utf-8").read()
    frase = [l for l in pt.splitlines() if "'bt.adblock.tv'" in l][0]
    assert "televisão" in frase and "não" in frase
    # E o texto do "tocar na TV" não promete o que não existe.
    dica = [l for l in pt.splitlines() if "'bt.import.cast.hint'" in l][0]
    assert "não daqui" in dica or "não vem daqui" in dica


def test_o_portugues_das_frases_novas_existe():
    pt = io.open(os.path.join(RAIZ, "web", "lib", "strings-pt.js"), encoding="utf-8").read()
    for chave in ("bt.adblock.title", "bt.adblock.on", "bt.adblock.what",
                  "bt.adblock.tv", "bt.adblock.saved"):
        assert "'%s'" % chave in pt, "falta o português de %s" % chave


# --------------------------------------------------------------------------- o clique repetido


def test_o_mesmo_link_duas_vezes_nao_vira_dois_downloads(auth_client, fake_ytdl, monkeypatch):
    """Foi o que quebrou no Pi: três Trazer no mesmo link, três downloads no mesmo arquivo.

    O segundo tenta renomear o que o primeiro já renomeou e morre com "No such
    file or directory: ... .webm -> ...". Mesmo dando certo seria desperdício: a
    faixa tem id derivado do vídeo, então a segunda cópia sobrescreve a primeira.
    """
    from project_os.apps.birdtunes import sources

    # O trabalho fica "rodando": é o estado em que o segundo clique acontece.
    monkeypatch.setattr(sources, "run_job", lambda *a, **k: {"state": "running"})

    url = "https://www.youtube.com/watch?v=EPhfbtjqWM8"
    primeiro = auth_client.post("/api/apps/birdtunes/import/youtube", json={"url": url})
    assert primeiro.status_code == 200, primeiro.text
    job = primeiro.json()["job_id"]

    db = auth_client.app.state.db
    db.execute("UPDATE app_birdtunes_imports SET state = 'running' WHERE id = ?", (job,))

    segundo = auth_client.post("/api/apps/birdtunes/import/youtube", json={"url": url})
    assert segundo.status_code == 200, segundo.text
    assert segundo.json()["job_id"] == job, "o segundo clique tem que cair no trabalho que já existe"
    assert segundo.json().get("already_running") is True

    jobs = auth_client.get("/api/apps/birdtunes/import").json()["jobs"]
    assert len([j for j in jobs if j["url"] == url]) == 1


def test_um_clique_repetido_nao_deixa_playlist_vazia_para_tras(auth_client, fake_ytdl, monkeypatch):
    """A conferência vem antes de criar a playlist, não depois."""
    from project_os.apps.birdtunes import sources

    monkeypatch.setattr(sources, "run_job", lambda *a, **k: {"state": "running"})
    url = "https://www.youtube.com/playlist?list=PLzinha"

    primeiro = auth_client.post(
        "/api/apps/birdtunes/import/youtube", json={"url": url, "as_playlist": True}
    )
    assert primeiro.status_code == 200, primeiro.text
    db = auth_client.app.state.db
    db.execute("UPDATE app_birdtunes_imports SET state = 'running' WHERE id = ?",
               (primeiro.json()["job_id"],))
    antes = len(auth_client.get("/api/apps/birdtunes/playlists").json()["playlists"])

    auth_client.post("/api/apps/birdtunes/import/youtube", json={"url": url, "as_playlist": True})
    depois = len(auth_client.get("/api/apps/birdtunes/playlists").json()["playlists"])
    assert depois == antes, "o segundo clique criou uma playlist vazia"


def test_depois_de_terminar_o_mesmo_link_pode_ser_trazido_de_novo(auth_client, fake_ytdl, monkeypatch):
    """Rebaixar não é proibido -- o que não pode é baixar duas vezes ao mesmo tempo."""
    from project_os.apps.birdtunes import sources

    monkeypatch.setattr(sources, "run_job", lambda *a, **k: {"state": "done"})
    url = "https://www.youtube.com/watch?v=EE_kM6fgW9I"

    primeiro = auth_client.post("/api/apps/birdtunes/import/youtube", json={"url": url}).json()
    db = auth_client.app.state.db
    db.execute("UPDATE app_birdtunes_imports SET state = 'done' WHERE id = ?", (primeiro["job_id"],))

    segundo = auth_client.post("/api/apps/birdtunes/import/youtube", json={"url": url}).json()
    assert segundo["job_id"] != primeiro["job_id"]
