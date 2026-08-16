"""A agenda chegava na hora, não tocava, e não contava a ninguém.

*"agendamento n funciono"*.

O horário marcado é a única coisa que este app faz sozinho, sem ninguém olhando
-- e era a única que podia dar errado em silêncio. ``_on_schedule_play`` chamava
``play()`` dentro de um ``try`` que engolia ``ApiError`` num ``log.info`` que
ninguém lê. Pior: dois dos três jeitos de não tocar nem exceção levantam --
``play()`` devolve ``{"playing": False, "reason": ...}`` quando não há faixa
escolhível, e ``_start_track`` desiste em silêncio quando o arquivo não serve
para aquela saída. Nos três casos a tela seguia dizendo "Toca às 08:00", a hora
chegava, não saía som, e não havia onde perguntar por quê.

No Pi dele a causa estava à vista desde o começo, no cartão do painel: não havia
caixa de som escolhida. O app sabia; a tela da Agenda, onde ele marcou o horário,
não dizia nada.

Duas metades, e as duas têm teste aqui:

* **antes da hora** -- ``schedule_blocked()`` confere agora o que vai impedir o
  próximo horário (sem caixa de som escolhida, sem faixa que dê para tocar) e a
  tela mostra isso na Agenda, com o botão de escolher a caixa;
* **depois da hora** -- toda tentativa fica registrada com o motivo, e um
  horário que não tocou aparece na tela em vez de sumir num log.
"""

from __future__ import annotations

import asyncio
import io
import os

import pytest

pytestmark = pytest.mark.usefixtures("home")

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAINEL = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.js")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")
SCHEDULER = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "scheduler.py")


# --------------------------------------------------------------------------- antes da hora


def test_sem_caixa_de_som_a_agenda_avisa_que_nao_vai_tocar(auth_client):
    corpo = auth_client.get("/api/apps/birdtunes/schedule").json()
    bloqueio = corpo["blocked"]
    assert bloqueio and bloqueio["code"] == "no_output"
    assert "caixa de som" in bloqueio["message"], bloqueio
    assert "não vai sair som" in bloqueio["message"], (
        "o aviso tem que dizer a consequência, e não só o estado"
    )


def test_o_aviso_sai_junto_com_o_resto_da_agenda(auth_client):
    """Uma chamada só: a tela não precisa saber montar o diagnóstico sozinha."""
    corpo = auth_client.get("/api/apps/birdtunes/schedule").json()
    for chave in ("schedule", "next_change", "active_window", "blocked", "last_attempt"):
        assert chave in corpo, "falta %s na resposta da agenda" % chave


def test_com_caixa_escolhida_o_aviso_passa_a_ser_o_acervo(auth_client):
    """Escolher a caixa não é o fim: sem faixa nenhuma continua não tocando.

    O aviso tem que trocar de motivo em vez de sumir -- senão a tela diria que
    está tudo certo e, na hora, continuaria não saindo som.
    """
    resposta = auth_client.put(
        "/api/apps/birdtunes/config", json={"output.type": "airplay"}
    )
    assert resposta.status_code == 200, resposta.text
    bloqueio = auth_client.get("/api/apps/birdtunes/schedule").json()["blocked"]
    assert bloqueio is not None, "sem faixa nenhuma, a agenda ainda não vai tocar"
    assert bloqueio["code"] != "no_output"
    assert bloqueio["message"], "motivo sem frase não serve para ninguém"


# --------------------------------------------------------------------------- depois da hora


class _AppFalso(object):
    """O bastante de BirdTunesApp para o registro da tentativa rodar de verdade."""

    def __init__(self, resultado=None, erro=None):
        import logging

        from project_os.apps.birdtunes.app import BirdTunesApp

        self.log = logging.getLogger("teste")
        self._resultado = resultado
        self._erro = erro
        self._last_schedule_attempt = None
        self.emitidos = []
        self.ctx = self
        self.config = self
        self._anotar_tentativa = BirdTunesApp._anotar_tentativa.__get__(self)
        self._tocar = BirdTunesApp._on_schedule_play.__get__(self)

    # ctx.config
    def get(self, chave, padrao=None):
        return padrao

    def set(self, chave, valor):
        pass

    def raw_dict(self):
        return {}

    # ctx
    def emit(self, nome, dados):
        self.emitidos.append((nome, dados))

    async def play(self, playlist_id=None, track_id=None):
        if self._erro is not None:
            raise self._erro
        return self._resultado


def _rodar(app, janela):
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(app._tocar(janela))


def test_um_erro_no_horario_marcado_fica_registrado(monkeypatch):
    from project_os.apps.birdtunes import app as modulo
    from project_os.errors import ApiError

    monkeypatch.setattr(modulo, "_now", lambda config=None: __import__("datetime").datetime(2026, 1, 7, 8, 0))
    app = _AppFalso(erro=ApiError(503, "device_unreachable", "A caixa de som não respondeu."))
    _rodar(app, {"id": "manha", "name": "De manhã"})

    tentativa = app._last_schedule_attempt
    assert tentativa["ok"] is False
    assert tentativa["code"] == "device_unreachable"
    assert tentativa["message"] == "A caixa de som não respondeu."
    assert tentativa["window_id"] == "manha"


def test_o_horario_que_nao_achou_faixa_tambem(monkeypatch):
    """Este nem exceção levanta -- era o silêncio mais fundo dos três."""
    from project_os.apps.birdtunes import app as modulo

    monkeypatch.setattr(modulo, "_now", lambda config=None: __import__("datetime").datetime(2026, 1, 7, 8, 0))
    app = _AppFalso(resultado={"playing": False, "reason": "no_tracks", "message": "Sem faixa."})
    _rodar(app, {"id": "manha", "name": "De manhã"})

    assert app._last_schedule_attempt["ok"] is False
    assert app._last_schedule_attempt["code"] == "no_tracks"
    assert app._last_schedule_attempt["message"] == "Sem faixa."


def test_quando_toca_o_registro_diz_que_deu_certo(monkeypatch):
    from project_os.apps.birdtunes import app as modulo

    monkeypatch.setattr(modulo, "_now", lambda config=None: __import__("datetime").datetime(2026, 1, 7, 8, 0))
    app = _AppFalso(resultado={"playing": True, "track": {"id": "x"}})
    _rodar(app, {"id": "manha", "name": "De manhã"})
    assert app._last_schedule_attempt["ok"] is True


def test_a_tela_fica_sabendo_na_hora(monkeypatch):
    """Sem o evento, o aviso só apareceria quando alguém recarregasse a página."""
    from project_os.apps.birdtunes import app as modulo

    monkeypatch.setattr(modulo, "_now", lambda config=None: __import__("datetime").datetime(2026, 1, 7, 8, 0))
    app = _AppFalso(resultado={"playing": False, "reason": "no_tracks"})
    _rodar(app, {"id": "manha", "name": ""})
    assert [nome for nome, _dados in app.emitidos] == ["schedule"]


# --------------------------------------------------------------------------- a tela


def _painel():
    return io.open(PAINEL, encoding="utf-8").read()


def test_a_agenda_mostra_o_aviso():
    fonte = _painel()
    assert "function scheduleWarnings()" in fonte
    assert "scheduleWarnings()," in fonte, "a função existe e ninguém chama"
    assert "info.blocked" in fonte and "info.last_attempt" in fonte


def test_e_oferece_escolher_a_caixa_ali_mesmo():
    """Dizer o que falta sem dar como resolver é meio aviso."""
    fonte = _painel()
    assert "bt.schedule.pick_output" in fonte
    assert "state.outputOpen = true" in fonte


def test_o_portugues_do_aviso_existe():
    pt = io.open(PT, encoding="utf-8").read()
    for chave in ("bt.schedule.blocked", "bt.schedule.pick_output", "bt.schedule.last_failed"):
        assert "'%s':" % chave in pt, "falta o português de %s" % chave


# --------------------------------------------------------------------------- as palavras


def test_a_agenda_fala_portugues():
    """"Toca às 08:00 tomorrow" e "While I'm out" numa tela em português."""
    from project_os.apps.birdtunes import scheduler

    import datetime as dt

    hoje = dt.datetime(2026, 1, 7, 10, 0)
    amanha = dt.datetime(2026, 1, 8, 8, 0)
    assert scheduler._describe_moment(amanha, hoje) == "08:00 de amanhã"

    nomes = [p["name"] for p in scheduler.PRESETS]
    for nome in nomes:
        assert not any(p in nome for p in ("While", "Mornings", "Afternoons", "Weekends")), nome
    assert "De manhã" in nomes


def test_o_dia_da_semana_distante_tambem():
    import datetime as dt

    from project_os.apps.birdtunes import scheduler

    hoje = dt.datetime(2026, 1, 7, 10, 0)          # quarta
    daqui_tres_dias = dt.datetime(2026, 1, 10, 8, 0)  # sábado
    texto = scheduler._describe_moment(daqui_tres_dias, hoje)
    assert texto == "08:00 de sábado", texto
