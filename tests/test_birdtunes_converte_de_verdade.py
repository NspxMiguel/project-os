"""O botão "Converter para MP3" do BirdTunes não convertia nada.

A tela mostra, quando a saída escolhida não toca o formato da faixa, um aviso e
um botão: *"Converter para MP3"*. O endpoint por trás dele era:

    return {"queued": list(body.get("track_ids") or [])}

Devolvia os ids que recebeu. Sem fila, sem ffmpeg, sem arquivo. Quem tem um
flac e uma Apple TV (que não toca flac) clicava, via o toast de sucesso, e
continuava sem tocar nada -- e o mesmo aviso voltava no recarregar seguinte,
porque a faixa continuava sendo flac.
"""

from __future__ import annotations

import os

import pytest


def _ffmpeg_de_mentira(monkeypatch, sucesso=True, escreve=True):
    """Um ffmpeg que produz um arquivo, sem precisar do ffmpeg de verdade."""
    from project_os.apps.birdtunes import sources

    chamadas = []

    class Resposta(object):
        returncode = 0 if sucesso else 1
        stderr = b"" if sucesso else b"Invalid data found when processing input"

    def falso_run(argv, **kwargs):
        chamadas.append(list(argv))
        if escreve and sucesso:
            destino = argv[-1]
            with open(destino, "wb") as arquivo:
                arquivo.write(b"ID3mp3 de mentira")
        return Resposta()

    import subprocess

    monkeypatch.setattr(sources, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", falso_run)
    return chamadas


def test_converte_um_flac_e_deixa_o_original(tmp_path, monkeypatch):
    from project_os.apps.birdtunes import sources

    chamadas = _ffmpeg_de_mentira(monkeypatch)
    origem = tmp_path / "alvorada.flac"
    origem.write_bytes(b"fLaC de mentira")

    destino = sources.convert_to_mp3(str(origem))

    assert destino == str(tmp_path / "alvorada.mp3")
    assert os.path.isfile(destino)
    assert origem.exists(), "o original não pode sumir: não existe desfazer"
    # O ffmpeg foi chamado com libmp3lame e sem vídeo.
    argv = chamadas[0]
    assert "libmp3lame" in argv and "-vn" in argv
    # E escreveu num arquivo temporário antes de virar o final.
    assert argv[-1].endswith(".part.mp3")


def test_o_ffmpeg_falhando_vira_mensagem_e_nao_arquivo_pela_metade(tmp_path, monkeypatch):
    from project_os.apps.birdtunes import sources

    _ffmpeg_de_mentira(monkeypatch, sucesso=False)
    origem = tmp_path / "quebrada.flac"
    origem.write_bytes(b"nao e audio")

    with pytest.raises(RuntimeError) as caiu:
        sources.convert_to_mp3(str(origem))
    assert "Invalid data" in str(caiu.value)
    assert not (tmp_path / "quebrada.mp3").exists()


def test_sem_ffmpeg_recusa_antes_de_tentar(tmp_path, monkeypatch):
    from project_os.apps.birdtunes import sources

    monkeypatch.setattr(sources, "ffmpeg_available", lambda: False)
    origem = tmp_path / "x.flac"
    origem.write_bytes(b"a")
    with pytest.raises(RuntimeError):
        sources.convert_to_mp3(str(origem))


def test_converter_um_mp3_nao_sobrescreve_ele_mesmo(tmp_path, monkeypatch):
    from project_os.apps.birdtunes import sources

    _ffmpeg_de_mentira(monkeypatch)
    origem = tmp_path / "ja.mp3"
    origem.write_bytes(b"ID3")
    with pytest.raises(RuntimeError):
        sources.convert_to_mp3(str(origem))
    assert origem.read_bytes() == b"ID3"


def test_pelo_http_a_faixa_convertida_entra_na_biblioteca(auth_client, media_dir, monkeypatch):
    """O caminho inteiro: POST /convert -> arquivo novo -> biblioteca."""
    _ffmpeg_de_mentira(monkeypatch)
    origem = media_dir / "passarinho.flac"
    origem.write_bytes(b"fLaC de mentira")

    varredura = auth_client.post("/api/apps/birdtunes/library/scan")
    assert varredura.status_code == 200, varredura.text
    faixas = auth_client.get("/api/apps/birdtunes/library").json()["tracks"]
    alvo = [t for t in faixas if t["path"].endswith("passarinho.flac")]
    assert alvo, "o flac não entrou na biblioteca"

    resposta = auth_client.post(
        "/api/apps/birdtunes/convert", json={"track_ids": [alvo[0]["id"]]}
    )
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["count"] == 1
    assert corpo["failed"] == []
    assert (media_dir / "passarinho.mp3").exists()

    # E a faixa nova aparece na biblioteca sem ninguém mandar varrer de novo.
    depois = auth_client.get("/api/apps/birdtunes/library").json()["tracks"]
    assert any(t["path"].endswith("passarinho.mp3") for t in depois)


def test_converter_sem_dizer_o_que_e_recusado(auth_client, monkeypatch):
    from project_os.apps.birdtunes import sources

    monkeypatch.setattr(sources, "ffmpeg_available", lambda: True)
    resposta = auth_client.post("/api/apps/birdtunes/convert", json={"track_ids": []})
    assert resposta.status_code == 400


def test_faixa_inexistente_vira_falha_nomeada_e_nao_500(auth_client, monkeypatch):
    _ffmpeg_de_mentira(monkeypatch)
    resposta = auth_client.post(
        "/api/apps/birdtunes/convert", json={"track_ids": ["nao-existe"]}
    )
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["count"] == 0
    assert corpo["failed"][0]["id"] == "nao-existe"
