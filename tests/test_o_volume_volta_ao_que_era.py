"""Uma janela com volume próprio não pode virar o volume de tudo.

*"ta tocando aq, mas subiu pra 30% o volume do nada, sendo q era 15 o padrao
lembra??"*

Ele lembra certo: ``DEFAULT_VOLUME = 0.15`` e o manifesto declara
``output.volume: 0.15``. O que acontecia é que ``_on_schedule_play`` gravava o
volume da janela **na configuração**. Uma janela marcada a 30% não tocava a
30% -- ela *passava a valer* 30% para tudo dali em diante: o botão de tocar, as
outras janelas sem volume próprio, e o número que a tela mostra como padrão.

E numa TV o estrago sai do app: o volume do Chromecast é do aparelho, não da
nossa sessão. Depois de os passarinhos cantarem a 30%, o próximo vídeo que ele
abrir na TV começa a 30%.
"""

from __future__ import annotations

import asyncio


def _rodar(corrotina):
    laco = asyncio.new_event_loop()
    try:
        return laco.run_until_complete(corrotina)
    finally:
        laco.close()


# ------------------------------------------------------------- a configuração


class _ConfigFalso(object):
    def __init__(self, volume):
        self.valores = {"output.volume": volume}

    def get(self, chave, padrao=None):
        return self.valores.get(chave, padrao)

    def set(self, chave, valor):
        self.valores[chave] = valor

    def save(self):
        pass

    def raw_dict(self):
        return {"output": {"max_volume": 0.6, "volume": self.valores["output.volume"]}}


class _AppFalso(object):
    from project_os.apps.birdtunes.app import BirdTunesApp as _real

    _devolver_volume = _real._devolver_volume

    def __init__(self, volume=0.15):
        self.ctx = type("C", (), {"config": _ConfigFalso(volume)})()
        self._volume_de_antes = None

    @property
    def volume(self):
        return self.ctx.config.get("output.volume")


def test_o_padrao_do_app_e_quinze_por_cento():
    from project_os.apps.birdtunes.app import DEFAULT_VOLUME

    assert DEFAULT_VOLUME == 0.15


def test_a_janela_devolve_o_volume_ao_fechar():
    app = _AppFalso(0.15)
    # o que _on_schedule_play faz quando a janela tem volume próprio
    app._volume_de_antes = app.ctx.config.get("output.volume")
    app.ctx.config.set("output.volume", 0.30)
    assert app.volume == 0.30, "durante a janela, vale o volume dela"

    app._devolver_volume()
    assert app.volume == 0.15, "depois, volta a ser o dele"


def test_e_devolver_duas_vezes_nao_estraga():
    app = _AppFalso(0.15)
    app._volume_de_antes = 0.15
    app.ctx.config.set("output.volume", 0.30)
    app._devolver_volume()
    app._devolver_volume()
    assert app.volume == 0.15


def test_sem_janela_nenhuma_nao_ha_o_que_devolver():
    app = _AppFalso(0.42)
    app._devolver_volume()
    assert app.volume == 0.42, "não inventa um volume que ninguém guardou"


def test_o_codigo_guarda_antes_de_escrever():
    import io
    import os

    fonte = io.open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "project_os", "apps", "birdtunes", "app.py"), encoding="utf-8").read()
    corpo = fonte[fonte.index("async def _on_schedule_play"):]
    corpo = corpo[:corpo.index("async def _on_schedule_still_open")]
    assert "if self._volume_de_antes is None:" in corpo
    assert corpo.index("self._volume_de_antes = self.ctx.config.get") < corpo.index(
        'self.ctx.config.set("output.volume"')


def test_mexer_no_volume_com_a_mao_ganha_da_janela():
    """Se ele escolheu 20% durante a janela, o fim da janela não desfaz isso."""
    import io
    import os

    fonte = io.open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "project_os", "apps", "birdtunes", "app.py"), encoding="utf-8").read()
    corpo = fonte[fonte.index("async def set_volume"):]
    corpo = corpo[:corpo.index("# -- session cap")]
    assert "self._volume_de_antes = None" in corpo


# ------------------------------------------------------------------ a TV


class _CastFalso(object):
    def __init__(self, volume):
        self.status = type("S", (), {"volume_level": volume, "display_name": "Default Media Receiver"})()
        self.escritas = []

    def set_volume(self, valor):
        self.escritas.append(valor)
        self.status.volume_level = valor


def _player(volume_da_tv):
    from project_os.apps.birdtunes.players.chromecast import ChromecastPlayer

    p = ChromecastPlayer.__new__(ChromecastPlayer)
    p._cast = _CastFalso(volume_da_tv)
    p._volume_do_aparelho = None
    p.volume = 0.15
    p.device = {"id": "chromecast:x", "name": "TV"}
    return p


def test_a_tv_volta_ao_volume_que_ele_tinha_deixado():
    p = _player(0.15)
    _rodar(p.set_volume(0.30))
    assert p._cast.status.volume_level == 0.30, "durante a música, o volume é o da música"

    _rodar(p._devolver_volume_do_aparelho())
    assert p._cast.status.volume_level == 0.15, "o próximo vídeo dele começa como antes"


def test_e_guarda_o_volume_uma_vez_so():
    """Duas mudanças no meio da mesma sessão não podem reescrever a lembrança."""
    p = _player(0.15)
    _rodar(p.set_volume(0.30))
    _rodar(p.set_volume(0.45))
    _rodar(p._devolver_volume_do_aparelho())
    assert p._cast.status.volume_level == 0.15


def test_parar_devolve_o_volume():
    import io
    import os

    fonte = io.open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "project_os", "apps", "birdtunes", "players", "chromecast.py"), encoding="utf-8").read()
    corpo = fonte[fonte.index("    async def stop(self)"):]
    corpo = corpo[:corpo.index("    async def pause(self)")]
    assert "_devolver_volume_do_aparelho()" in corpo


# ------------------------------------------------------------- e o HomePod


class _AudioFalso(object):
    def __init__(self, volume_0a100):
        self.volume = volume_0a100
        self.escritas = []

    async def set_volume(self, valor):
        self.escritas.append(valor)
        self.volume = valor


def _airplay(volume_da_caixa):
    from project_os.apps.birdtunes.players.airplay import AirPlayPlayer

    p = AirPlayPlayer.__new__(AirPlayPlayer)
    p._atv = type("A", (), {"audio": _AudioFalso(volume_da_caixa)})()
    p._volume_do_aparelho = None
    p.volume = 0.15
    p.device = {"id": "homepod:x", "name": "HomePod"}
    return p


def test_o_homepod_tambem_volta_ao_volume_dele():
    """Uma caixa tem um volume só, e ele fica como a gente deixou.

    O mesmo defeito da TV: depois de os passarinhos cantarem a 30%, a próxima
    música que ele mandar para a caixa começa a 30%.
    """
    p = _airplay(20.0)  # pyatv fala 0-100
    _rodar(p.set_volume(0.30))
    assert p._atv.audio.volume == 30.0

    _rodar(p._devolver_volume_do_aparelho())
    assert p._atv.audio.volume == 20.0


def test_e_tambem_guarda_uma_vez_so():
    p = _airplay(20.0)
    _rodar(p.set_volume(0.30))
    _rodar(p.set_volume(0.45))
    _rodar(p._devolver_volume_do_aparelho())
    assert p._atv.audio.volume == 20.0


def test_parar_no_airplay_devolve_o_volume():
    import io
    import os

    fonte = io.open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "project_os", "apps", "birdtunes", "players", "airplay.py"), encoding="utf-8").read()
    corpo = fonte[fonte.index("    async def stop(self)"):]
    corpo = corpo[:corpo.index("PlaybackState.STOPPED")]
    assert "_devolver_volume_do_aparelho()" in corpo
