"""Desde quando a caixa está ligada, num aparelho sem relógio.

Este teste existe por causa de uma medição no Pi dele, no primeiro boot do
cartão novo:

    fake-hwclock da imagem:  2026-08-12 04:23:57
    started_at do serviço:   2026-08-12T04:24:14Z   (17 segundos depois)
    hora de verdade:         2026-08-12T15:49:00Z

Uma Raspberry não tem bateria de relógio. Ela sobe com a hora que estava gravada
no cartão -- numa imagem recém-gravada, a hora em que a **imagem foi
construída** -- e só depois o NTP acerta. Estampar a hora de parede no boot
grava essa mentira para sempre: a tela diria que a caixa está ligada há onze
horas, até o serviço reiniciar. Não quebra nada, mas é o tipo de coisa que faz
ele perguntar se dá para confiar no resto do que a tela diz.

A correção é não estampar: guarda-se um ponto do relógio monotônico, que não
anda para trás nem pula quando o NTP corrige, e o começo é calculado na hora de
responder -- agora menos quanto tempo faz.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Estado:
    """O app.state, do tamanho que este teste precisa."""


def test_marca_o_comeco_das_duas_formas():
    from project_os.core import clock

    e = Estado()
    clock.mark_start(e)

    assert getattr(e, "started_monotonic", None) is not None
    assert getattr(e, "started_at", "").endswith("Z")


def test_o_started_at_e_calculado_para_tras(monkeypatch):
    from project_os.core import clock

    e = Estado()
    clock.mark_start(e)
    # Dez minutos de uptime, sem esperar dez minutos.
    e.started_monotonic = e.started_monotonic - 600

    faz = clock.uptime(e)
    assert 599 <= faz <= 601

    quando = datetime.fromisoformat(clock.started_at(e).replace("Z", "+00:00"))
    esperado = datetime.now(timezone.utc) - timedelta(seconds=600)
    assert abs((quando - esperado).total_seconds()) < 5


def test_um_salto_do_relogio_nao_estraga_a_conta(monkeypatch):
    """O caso do Pi: o NTP corrige onze horas depois do boot.

    A estampa do boot fica onze horas no passado; a conta monotônica não.
    """
    from project_os.core import clock

    e = Estado()
    clock.mark_start(e)
    estampa_do_boot = e.started_at

    # O relógio de parede pula onze horas para a frente, como o NTP faz num
    # cartão recém-gravado. O monotônico não se move junto.
    real = datetime.now(timezone.utc) + timedelta(hours=11)

    class ReloginhoQuePulou(datetime):
        @classmethod
        def now(cls, tz=None):
            return real if tz else real.replace(tzinfo=None)

    monkeypatch.setattr(clock, "datetime", ReloginhoQuePulou)

    calculado = datetime.fromisoformat(clock.started_at(e).replace("Z", "+00:00"))
    da_estampa = datetime.fromisoformat(estampa_do_boot.replace("Z", "+00:00"))

    # O jeito antigo diria que a caixa está de pé há onze horas.
    assert (real - da_estampa).total_seconds() > 39000
    # O jeito novo diz a verdade: subiu agora.
    assert abs((real - calculado).total_seconds()) < 5


def test_sem_marca_nenhuma_cai_para_o_que_existia_antes():
    from project_os.core import clock

    e = Estado()
    assert clock.uptime(e) is None
    assert clock.started_at(e) is None

    e.started_at = "2026-01-01T00:00:00Z"
    assert clock.started_at(e) == "2026-01-01T00:00:00Z"


def test_o_health_responde_o_valor_calculado():
    """A ponta que ele vê: /api/system/health."""
    from project_os import main
    from project_os.core import clock

    e = Estado()
    clock.mark_start(e)
    e.started_monotonic = e.started_monotonic - 120

    quando = datetime.fromisoformat(main._started_at_de(e).replace("Z", "+00:00"))
    esperado = datetime.now(timezone.utc) - timedelta(seconds=120)
    assert abs((quando - esperado).total_seconds()) < 5
