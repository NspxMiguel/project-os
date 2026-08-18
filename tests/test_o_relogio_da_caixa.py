"""A caixa marcava o fuso certo e contava as horas em UTC do mesmo jeito.

*"tlvz a hora dele ta errada tbm"* -- e estava, de um jeito que nenhuma tela
mostrava.

No Pi dele, medido pelo navegador: ``system.timezone`` = ``America/Sao_Paulo``
na configuração, e o BirdTunes respondendo "Horário de silêncio: nada toca até
ele acabar" às **18:41** da noite dele, com silêncio marcado para 20:00. Só
bate se a caixa achar que são 21:41 -- ou seja, UTC.

A causa: ``zoneinfo`` lê a base de fusos do sistema, a imagem instala com
``--no-install-recommends`` e ninguém pediu ``tzdata``. Sem a base,
``ZoneInfo("America/Sao_Paulo")`` levanta; o ``_now`` do BirdTunes tinha um
``except Exception`` que escrevia ``log.warning`` e devolvia o relógio do
sistema. Resultado: escolher o fuso na tela de Ajustes não fazia nada, e nada
dizia isso.

O estrago some no meio do que parece certo -- a caixa mostra uma hora, a agenda
tem horários, tudo responde 200. Mas o silêncio começava às 17:00 dele e a
rotina do meio-dia (pedido 90) tocava 09:30.

Duas metades aqui: a conta num lugar só, que sabe dizer quando não deu, e o
``tzdata`` declarado como dependência para a base existir sempre.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from project_os.core import clock

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigFalsa(object):
    def __init__(self, fuso=""):
        self.fuso = fuso

    def get(self, chave, padrao=None):
        if chave == "system.timezone":
            return self.fuso
        return padrao


# ------------------------------------------------------------------ a conta


def test_sem_fuso_escolhido_a_caixa_diz_que_esta_sem_fuso():
    estado = clock.zona(ConfigFalsa(""))
    assert estado["resolved"] is False
    assert estado["problem"] == "unset"


def test_um_fuso_de_verdade_resolve():
    estado = clock.zona(ConfigFalsa("America/Sao_Paulo"))
    assert estado == {"name": "America/Sao_Paulo", "effective": "America/Sao_Paulo",
                      "resolved": True, "problem": "", "detail": ""}


def test_um_nome_inventado_e_nome_inventado_nao_falta_de_base():
    """Dois defeitos diferentes davam a mesma frase na primeira volta."""
    estado = clock.zona(ConfigFalsa("Marte/Olympus"))
    assert estado["resolved"] is False
    assert estado["problem"] == "tz_unknown"


def test_sem_a_base_de_fusos_o_diagnostico_muda(monkeypatch):
    """O caso do Pi: o nome está certo, a base é que não está instalada."""
    import zoneinfo

    def sempre_falha(nome, *a, **k):
        raise zoneinfo.ZoneInfoNotFoundError("No time zone found with key %s" % nome)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", sempre_falha)
    estado = clock.zona(ConfigFalsa("America/Sao_Paulo"))
    assert estado["resolved"] is False
    assert estado["problem"] == "tz_unavailable"
    assert "America/Sao_Paulo" in estado["detail"] or "Sao_Paulo" in estado["detail"]


def test_a_hora_sai_no_fuso_pedido():
    agora = clock.now(ConfigFalsa("America/Sao_Paulo"))
    utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    diferenca = round((agora - utc).total_seconds() / 3600.0)
    assert diferenca == -3, "São Paulo é UTC-3; saiu %d" % diferenca


def test_a_hora_nao_traz_fuso_pendurado():
    """O resto do código compara com ``datetime`` sem fuso; um aware aqui
    levantaria ``can't compare offset-naive and offset-aware``."""
    assert clock.now(ConfigFalsa("America/Sao_Paulo")).tzinfo is None


# --------------------------------------------------------------- o estado


def test_o_estado_diz_que_esta_tudo_certo_quando_esta():
    estado = clock.state(ConfigFalsa("America/Sao_Paulo"))
    assert estado["ok"] is True
    assert estado["resolved"] is True
    assert estado["effective"] == "America/Sao_Paulo"


def test_um_fuso_que_nao_aplica_nunca_sai_como_ok(monkeypatch):
    """A trava principal deste arquivo.

    Ficar `ok` com o fuso escolhido e não aplicado é exatamente o que a caixa
    dele fazia: a tela de Ajustes mostrava America/Sao_Paulo, e a agenda rodava
    em UTC.
    """
    import zoneinfo

    monkeypatch.setattr(zoneinfo, "ZoneInfo", lambda *a, **k: (_ for _ in ()).throw(
        zoneinfo.ZoneInfoNotFoundError("sem base")))
    estado = clock.state(ConfigFalsa("America/Sao_Paulo"))
    assert estado["ok"] is False
    assert estado["effective"] == "UTC"
    assert "UTC" in estado["message"]
    assert estado["message"].strip(), "sem frase a tela não tem o que mostrar"


def test_o_estado_mede_a_diferenca_para_quem_esta_olhando():
    agora = dt.datetime.now(dt.timezone.utc).timestamp()
    estado = clock.state(ConfigFalsa("America/Sao_Paulo"), browser_epoch=agora + 3600)
    assert estado["clock_disagrees"] is True
    assert 3500 < estado["drift_seconds"] < 3700


def test_um_segundo_de_diferenca_nao_e_briga():
    agora = dt.datetime.now(dt.timezone.utc).timestamp()
    estado = clock.state(ConfigFalsa("America/Sao_Paulo"), browser_epoch=agora + 1)
    assert estado["clock_disagrees"] is False
    assert estado["ok"] is True


def test_sem_navegador_nao_inventa_diferenca():
    assert clock.state(ConfigFalsa("America/Sao_Paulo"))["drift_seconds"] is None


def test_o_estado_nunca_levanta():
    """Roda dentro de laço de agenda e de rota de status."""
    class Explosiva(object):
        def get(self, *a, **k):
            raise RuntimeError("config quebrada")

    estado = clock.state(Explosiva())
    assert estado["effective"] == "UTC"


# ------------------------------------------------- o que isso faz na agenda


def test_o_silencio_passa_a_ser_medido_na_hora_da_casa():
    """O defeito dele, do jeito que ele viveu: 18:41 virando horário de silêncio.

    Sem remendar relógio nenhum: pega o instante real em que ele mediu
    (21:41 UTC), converte para a hora da casa dele e confere que 18:41 não é
    silêncio. A caixa dele respondia que era, porque nunca chegou a converter.
    """
    from zoneinfo import ZoneInfo

    from project_os.apps.birdtunes import safety

    instante = dt.datetime(2026, 8, 18, 21, 41, tzinfo=dt.timezone.utc)
    na_casa = instante.astimezone(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
    assert (na_casa.hour, na_casa.minute) == (18, 41)

    agenda = {"enabled": True, "quiet_hours": {"start": "20:00", "end": "07:00"}, "windows": []}
    assert safety.is_quiet_hours(na_casa, agenda) is False, (
        "18:41 não é horário de silêncio; a caixa dele achava que sim"
    )


def test_o_app_pergunta_as_horas_no_fuso_configurado():
    """E é o ``_now`` do app que faz essa conversão, não só o teste acima."""
    from zoneinfo import ZoneInfo

    from project_os.apps.birdtunes import app as modulo

    pelo_app = modulo._now(ConfigFalsa("America/Sao_Paulo"))
    esperado = dt.datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
    assert abs((pelo_app - esperado).total_seconds()) < 5, (pelo_app, esperado)

    em_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    assert round((pelo_app - em_utc).total_seconds() / 3600.0) == -3


def test_e_sem_o_fuso_aplicado_o_erro_reaparece(monkeypatch):
    """A prova por contraste: em UTC, 21:41 cai dentro do silêncio."""
    from project_os.apps.birdtunes import safety

    agenda = {"enabled": True, "quiet_hours": {"start": "20:00", "end": "07:00"}, "windows": []}
    assert safety.is_quiet_hours(dt.datetime(2026, 8, 18, 21, 41), agenda) is True


def test_o_birdtunes_nao_tem_mais_a_copia_dele():
    """Duas cópias da mesma decisão foi como a falha ficou escondida num log."""
    fonte = open(os.path.join(RAIZ, "project_os", "apps", "birdtunes", "app.py"),
                 encoding="utf-8").read()
    assert "clock.now(config)" in fonte
    assert "ZoneInfo" not in fonte, "o app voltou a decidir fuso por conta própria"


# ------------------------------------------------------------- a base existe


def test_tzdata_e_dependencia_obrigatoria():
    """Sem isto a caixa depende de a distribuição ter trazido a base -- e a
    imagem instala com --no-install-recommends."""
    for arquivo in ("pyproject.toml", "requirements.txt"):
        texto = open(os.path.join(RAIZ, arquivo), encoding="utf-8").read()
        assert "tzdata" in texto, "falta tzdata em %s" % arquivo
    # O bloco vai até a linha que é só "]" -- cortar no primeiro "]" pegaria o
    # de `uvicorn[standard]` e leria três linhas de dependência como se fossem
    # todas.
    linhas = open(os.path.join(RAIZ, "pyproject.toml"), encoding="utf-8").read().splitlines()
    inicio = linhas.index("dependencies = [")
    fim = inicio + 1 + linhas[inicio + 1:].index("]")
    obrigatorias = "\n".join(linhas[inicio:fim])
    assert "tzdata" in obrigatorias, "tzdata não pode ser extra opcional"
    assert "optional" not in obrigatorias


def test_a_base_de_fusos_esta_mesmo_instalada_aqui():
    import zoneinfo
    assert zoneinfo.ZoneInfo("America/Sao_Paulo")


# ---------------------------------------------------------------- a tela


def test_a_rota_devolve_o_relogio(auth_client):
    corpo = auth_client.get("/api/system/clock").json()
    for chave in ("local", "utc", "epoch", "timezone", "effective", "resolved",
                  "offset_minutes", "ok", "message"):
        assert chave in corpo, "falta %s" % chave


def test_a_rota_aceita_o_relogio_de_quem_olha(auth_client):
    import time as _t
    corpo = auth_client.get("/api/system/clock", params={"browser_epoch": _t.time() + 7200}).json()
    assert corpo["clock_disagrees"] is True
    assert corpo["ok"] is False


def test_o_painel_desenha_o_relogio():
    fonte = open(os.path.join(RAIZ, "web", "views", "dashboard.js"), encoding="utf-8").read()
    assert "clockSlot" in fonte
    assert "/system/clock" in fonte
    assert "clock__time" in fonte
    assert "clockSlot, systemSlot" in fonte, "o relógio é a primeira coisa da grade"


def test_o_relogio_da_tela_e_o_da_caixa_nao_o_do_navegador():
    """Um relógio que copiasse `new Date()` mostraria a hora certa sempre e não
    denunciaria nada -- que é o defeito, não o conserto."""
    fonte = open(os.path.join(RAIZ, "web", "views", "dashboard.js"), encoding="utf-8").read()
    trecho = fonte.split("function horaDaCaixa()", 1)[1].split("function renderClock", 1)[0]
    assert "relogio.epoch" in trecho
    assert "offset_minutes" in trecho
    assert "getUTCHours" in trecho, "somar o offset e ler a hora local somaria duas vezes"


def test_as_frases_do_relogio_existem_em_portugues():
    pt = open(os.path.join(RAIZ, "web", "lib", "strings-pt.js"), encoding="utf-8").read()
    for chave in ("dash.card.clock", "dash.clock.wrong", "dash.clock.vsYou",
                  "dash.clock.running", "dash.clock.noZone"):
        assert "'%s':" % chave in pt, "falta a tradução de %s" % chave


# ------------------------------------------- o app pergunta para quem sabe


def test_a_fatia_de_um_app_ainda_acha_o_fuso_da_caixa(tmp_path, monkeypatch):
    """A causa decisiva do atraso de três horas na caixa dele.

    Um app recebe ``ctx.config``, que é uma vista de ``apps.settings.<id>``.
    Perguntar ``system.timezone`` ali procura
    ``apps.settings.birdtunes.system.timezone`` -- que não existe -- e volta
    vazio. A tela de Ajustes mostrava ``America/Sao_Paulo``, o app nunca viu,
    e o ``except`` que sobrava seguia em UTC escrevendo num log que ninguém lê.
    """
    monkeypatch.setenv("PROJECT_OS_HOME", str(tmp_path))
    from project_os.config import load_config

    cfg = load_config()
    cfg.set("system.timezone", "America/Sao_Paulo")

    fatia = cfg.app("birdtunes")
    assert fatia.get("system.timezone", "") == "", (
        "a fatia do app não enxerga a chave da caixa -- é esse o problema"
    )
    assert clock.zona(fatia)["effective"] == "America/Sao_Paulo", (
        "o relógio tem que subir da fatia para a configuração da caixa"
    )
    assert clock.zona(fatia)["resolved"] is True


def test_o_app_de_verdade_calcula_silencio_na_hora_da_casa(tmp_path, monkeypatch):
    """A conta que ele viveu, com a configuração de verdade no meio."""
    monkeypatch.setenv("PROJECT_OS_HOME", str(tmp_path))
    from zoneinfo import ZoneInfo

    from project_os.apps.birdtunes import app as modulo
    from project_os.config import load_config

    cfg = load_config()
    cfg.set("system.timezone", "America/Sao_Paulo")

    pelo_app = modulo._now(cfg.app("birdtunes"))
    esperado = dt.datetime.now(ZoneInfo("America/Sao_Paulo")).replace(tzinfo=None)
    assert abs((pelo_app - esperado).total_seconds()) < 5

    em_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    assert round((pelo_app - em_utc).total_seconds() / 3600.0) == -3, (
        "o app voltou a contar as horas em UTC"
    )


def test_subir_a_fatia_nao_abre_o_resto_da_configuracao(tmp_path, monkeypatch):
    """O app continua vendo só o que é dele; quem sobe é o relógio, não o app."""
    monkeypatch.setenv("PROJECT_OS_HOME", str(tmp_path))
    from project_os.config import load_config

    cfg = load_config()
    cfg.set("security.allow_shell", True)
    fatia = cfg.app("birdtunes")
    assert fatia.get("security.allow_shell", "nada") == "nada"


def test_os_segundos_nao_colam_na_hora():
    """Na tela dele saiu "19:22 53", que não se lê como 19:22:53."""
    fonte = open(os.path.join(RAIZ, "web", "views", "dashboard.js"), encoding="utf-8").read()
    trecho = fonte.split("function horaDaCaixa()", 1)[1].split("function renderClock", 1)[0]
    assert "ss: ':' + dois(" in trecho, "os segundos precisam vir com os dois-pontos"
