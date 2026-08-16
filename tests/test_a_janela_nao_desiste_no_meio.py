"""Se a música cala no meio da janela, a janela tenta de novo.

Achado testando a agenda ao vivo: com uma janela das 15:30 às 16:00 tocando de
verdade numa TV, bastou um ``POST /stop`` para o silêncio durar até as 16:00.
O laço da agenda só reagia à *mudança* -- a janela era olhada no instante em
que abria e nunca mais. Na casa dele isso é a TV desligada no meio, a conexão
que cai, o erro que aparece no minuto dois: para quem marcou o horário, tudo
isso é indistinguível de "não tocou".

Duas coisas que este teste amarra, e a segunda é tão importante quanto a
primeira: apertar **parar** é ordem, não defeito. Uma janela que ressuscita a
música por cima do botão seria pior que o silêncio.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest


def _rodar(corrotina):
    laco = asyncio.new_event_loop()
    try:
        return laco.run_until_complete(corrotina)
    finally:
        laco.close()


def _agenda(inicio="08:00", fim="09:00"):
    return {
        "enabled": True,
        "quiet_hours": {"start": "23:00", "end": "06:00"},
        "windows": [{"id": "j1", "name": "Manhã", "enabled": True, "start": inicio,
                     "end": fim, "days": list(range(7)), "playlist_id": ""}],
    }


# --------------------------------------------------------------------- o laço


def test_a_janela_aberta_e_olhada_a_cada_volta():
    from project_os.apps.birdtunes import scheduler

    abriu, olhou = [], []
    laco = scheduler.SchedulerLoop(
        get_schedule=_agenda,
        on_should_play=lambda w: abriu.append(w["id"]),
        on_still_open=lambda w: olhou.append(w["id"]),
        clock=lambda: dt.datetime(2026, 1, 7, 8, 30),
    )
    _rodar(laco.tick())
    _rodar(laco.tick())
    _rodar(laco.tick())

    assert abriu == ["j1"], "abrir é uma vez só"
    assert olhou == ["j1", "j1"], "as voltas seguintes olham"


def test_e_fora_da_janela_ninguem_e_olhado():
    from project_os.apps.birdtunes import scheduler

    olhou = []
    laco = scheduler.SchedulerLoop(
        get_schedule=_agenda,
        on_still_open=lambda w: olhou.append(w),
        clock=lambda: dt.datetime(2026, 1, 7, 12, 0),
    )
    _rodar(laco.tick())
    assert olhou == []


# --------------------------------------------------------------------- o app


class _AppFalso(object):
    """Só o que ``_on_schedule_still_open`` toca, com a função de verdade."""

    from project_os.apps.birdtunes.app import BirdTunesApp as _real

    RETENTAR_A_CADA_S = _real.RETENTAR_A_CADA_S
    _on_schedule_still_open = _real._on_schedule_still_open
    _quem_esta_na_saida = _real._quem_esta_na_saida

    def __init__(self, estado, pedido=False):
        import logging

        self._estado = estado
        self._silencio_pedido = pedido
        self._ultima_retomada = 0.0
        self.log = logging.getLogger("teste")
        self.tentativas = []
        self._player = None  # saída que não sabe dizer quem está na frente

    def status(self):
        return {"state": self._estado}

    async def _on_schedule_play(self, window):
        self.tentativas.append(window["id"])


def test_calou_no_meio_entao_tenta_de_novo():
    app = _AppFalso("stopped")
    _rodar(app._on_schedule_still_open({"id": "j1"}))
    assert app.tentativas == ["j1"]


@pytest.mark.parametrize("estado", ["playing", "buffering"])
def test_mas_nao_atrapalha_quem_esta_tocando(estado):
    app = _AppFalso(estado)
    _rodar(app._on_schedule_still_open({"id": "j1"}))
    assert app.tentativas == []


def test_e_nao_passa_por_cima_do_botao_parar():
    """Ele apertou parar. Voltar a tocar sozinho seria desobedecer."""
    app = _AppFalso("stopped", pedido=True)
    _rodar(app._on_schedule_still_open({"id": "j1"}))
    assert app.tentativas == []


def test_insistir_tem_intervalo():
    """O laço bate a cada 15s; insistir a cada volta castiga uma TV desligada."""
    app = _AppFalso("stopped")
    for _ in range(5):
        _rodar(app._on_schedule_still_open({"id": "j1"}))
    assert app.tentativas == ["j1"], "cinco voltas seguidas, uma tentativa"
    assert app.RETENTAR_A_CADA_S >= 60


def test_uma_janela_nova_comeca_limpa():
    """Um "parar" às 08:10 não pode calar também o horário das 17:00."""
    import io
    import os

    fonte = io.open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "project_os", "apps", "birdtunes", "app.py"), encoding="utf-8").read()
    corpo = fonte[fonte.index("async def _on_schedule_play"):]
    corpo = corpo[:corpo.index("async def _on_schedule_still_open")]
    assert "self._silencio_pedido = False" in corpo


def test_o_botao_parar_marca_que_foi_pedido():
    import io
    import os

    fonte = io.open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "project_os", "apps", "birdtunes", "app.py"), encoding="utf-8").read()
    assert "instance.stop_playback(a_pedido=True)" in fonte
    assert "def stop_playback(self, a_pedido: bool = False)" in fonte


# ------------------------------------------------- e não rouba a TV de volta


class _AppComTV(_AppFalso):
    """Como o de cima, mas com uma saída que responde quem está na frente."""

    from project_os.apps.birdtunes.app import BirdTunesApp as _real2

    _quem_esta_na_saida = _real2._quem_esta_na_saida

    def __init__(self, estado, app_na_tela):
        _AppFalso.__init__(self, estado)
        self.anotado = []
        self._player = type("P", (), {"_app_na_tela": staticmethod(lambda: app_na_tela)})()

    def _anotar_tentativa(self, window, code, message):
        self.anotado.append((code, message))


def test_se_ele_pegou_a_tv_a_janela_nao_toma_de_volta():
    """Medido no vivo: ele escreveu "agr botei yt" no meio da janela.

    Insistir de 90 em 90 segundos cortaria o vídeo dele. Tomar a saída é o que
    ele pediu ao *marcar* o horário -- no começo da janela. No meio, a saída
    ocupada quer dizer que ele foi lá e mudou.
    """
    app = _AppComTV("stopped", "YouTube")
    _rodar(app._on_schedule_still_open({"id": "j1"}))
    assert app.tentativas == []
    assert app.anotado and app.anotado[0][0] == "device_busy"
    assert "YouTube" in app.anotado[0][1]


def test_mas_a_saida_livre_volta_a_tocar():
    app = _AppComTV("stopped", None)
    _rodar(app._on_schedule_still_open({"id": "j1"}))
    assert app.tentativas == ["j1"]


def test_o_nosso_proprio_receptor_nao_conta_como_ocupacao():
    app = _AppComTV("stopped", "Default Media Receiver")
    _rodar(app._on_schedule_still_open({"id": "j1"}))
    assert app.tentativas == ["j1"]


# ------------------------------------------- a tela para de dizer que toca


def test_o_vigia_desiste_quando_a_sessao_some():
    """Medido: a TV sem aplicativo nenhum e o app dizendo "playing", posição 255s.

    O vigia só reagia a IDLE/FINISHED. Desligar a TV, ou abrir o YouTube nela,
    encerra a sessão sem passar por IDLE -- e o laço girava para sempre.
    """
    import io
    import os

    fonte = io.open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "project_os", "apps", "birdtunes", "players", "chromecast.py"), encoding="utf-8").read()
    corpo = fonte[fonte.index("async def _watch"):]
    corpo = corpo[:corpo.index("def _app_na_tela")]
    assert "viu_tocar" in corpo, "só desiste depois de ter tocado de verdade"
    assert "SUMICOS_ATE_DESISTIR" in corpo, "uma volta só seria cedo demais"
    assert "REASON_ERROR" in corpo
    assert "PlaybackState.STOPPED" in corpo


def test_e_o_motivo_diz_quem_tomou_a_tela():
    from project_os.apps.birdtunes.players.chromecast import _fim_por_fora

    assert "YouTube" in _fim_por_fora("YouTube")
    assert _fim_por_fora(None) and "encerrada" in _fim_por_fora(None)


def test_nao_desiste_no_piscar_entre_uma_faixa_e_outra():
    from project_os.apps.birdtunes.players import chromecast

    assert chromecast.SUMICOS_ATE_DESISTIR >= 2
