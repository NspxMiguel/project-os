"""Ele marcou 05:00 para os pássaros e não saiu som nenhum.

*"eu programei pra tocar as 5 e n toco a musica pros piupiu"*.

O horário de silêncio ganha da agenda **de propósito** -- é a regra que impede
música às 2 da manhã com um bicho vivo do outro lado da caixa, está escrita em
``safety.check_can_play`` e em docs/BIRDTUNES.md seção 5, e não muda aqui. O
defeito nunca foi a regra: era ela ser aplicada em silêncio.

O padrão do silêncio é 20:00-07:00. Uma janela às 05:00 cai inteira dentro
dele. O que acontecia:

* ``_would_play`` devolvia ``False`` sem registrar motivo nenhum;
* ``schedule_blocked()``, cuja razão de existir é dizer antes da hora o que vai
  impedir o horário de tocar, conferia caixa de som e acervo -- nunca o
  silêncio;
* e ``next_change`` varria oito dias, não achava nenhuma virada, e respondia
  **"Nada marcado para a próxima semana"** com o horário marcado ali na tela.

Ou seja: a agenda mostrava o horário, jurava que não havia nada marcado, e a
hora passava muda. Nada no sistema inteiro dizia a palavra "silêncio".

Este arquivo tranca as duas metades: a medida (quanto de cada janela o silêncio
come, e como encolher o silêncio para ela caber) e o aviso (as duas telas
dizendo isso antes da hora, não depois).
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from project_os.apps.birdtunes import safety, scheduler

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAINEL = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.js")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")
CSS = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.css")

CINCO_DA_MANHA = {
    "id": "piupiu", "name": "Pros piupiu", "enabled": True,
    "start": "05:00", "end": "06:00", "days": [0, 1, 2, 3, 4, 5, 6], "playlist_id": "",
}


def agenda(*janelas, **kwargs):
    return {
        "enabled": kwargs.get("enabled", True),
        "quiet_hours": kwargs.get("quiet_hours", {"start": "20:00", "end": "07:00"}),
        "windows": list(janelas),
    }


def janela(inicio, fim, **extras):
    corpo = dict(CINCO_DA_MANHA, start=inicio, end=fim)
    corpo.update(extras)
    return corpo


# ------------------------------------------------------------------ a medida


def test_a_janela_das_cinco_esta_inteira_dentro_do_silencio():
    """O caso dele, medido: 60 minutos marcados, 60 minutos calados."""
    medida = scheduler.quiet_overlap(CINCO_DA_MANHA, agenda(CINCO_DA_MANHA))
    assert medida == {"kind": "full", "quiet_minutes": 60, "total_minutes": 60}


def test_e_o_app_realmente_nao_toca_nela():
    """A medida acima não é teoria: é o que ``_would_play`` faz às 05:30."""
    momento = dt.datetime(2026, 8, 18, 5, 30)
    cfg = agenda(CINCO_DA_MANHA)
    assert scheduler.active_window(momento, cfg) is not None, "a janela está aberta"
    assert safety.is_quiet_hours(momento, cfg) is True
    assert scheduler._would_play(momento, cfg)[0] is False, "e mesmo assim não toca"


def test_uma_janela_fora_do_silencio_nao_vira_conflito():
    assert scheduler.quiet_conflicts(agenda(janela("17:00", "18:00"))) == []


def test_a_janela_que_encosta_no_silencio_conta_so_o_pedaco_perdido():
    """19:30-20:30 com silêncio às 20:00: toca meia hora e apaga no meio."""
    medida = scheduler.quiet_overlap(janela("19:30", "20:30"), agenda())
    assert medida["kind"] == "partial"
    assert medida["quiet_minutes"] == 30
    assert medida["total_minutes"] == 60


def test_a_janela_que_vira_a_meia_noite_tambem_e_medida():
    medida = scheduler.quiet_overlap(janela("23:00", "01:00"), agenda())
    assert medida == {"kind": "full", "quiet_minutes": 120, "total_minutes": 120}


def test_janela_de_comprimento_zero_nao_conflita_com_nada():
    """``active_window`` nunca casa com ela, então ela não pode virar aviso."""
    assert scheduler.quiet_overlap(janela("05:00", "05:00"), agenda())["kind"] == "none"
    assert scheduler.quiet_conflicts(agenda(janela("05:00", "05:00"))) == []


def test_janela_desligada_nao_vira_aviso():
    assert scheduler.quiet_conflicts(agenda(janela("05:00", "06:00", enabled=False))) == []


def test_agenda_desligada_nao_vira_aviso():
    """Com a agenda desligada nada toca por outro motivo -- avisar do silêncio
    ali seria apontar a causa errada."""
    assert scheduler.quiet_conflicts(agenda(CINCO_DA_MANHA, enabled=False)) == []


def test_silencio_zerado_nao_cala_ninguem():
    """``start == end`` é lido como "nunca" em ``is_quiet_hours``; a medida tem
    que concordar com ela, e não inventar uma segunda leitura."""
    cfg = agenda(CINCO_DA_MANHA, quiet_hours={"start": "22:00", "end": "22:00"})
    assert scheduler.quiet_conflicts(cfg) == []


def test_cada_janela_do_conflito_diz_qual_e():
    conflito = scheduler.quiet_conflicts(agenda(CINCO_DA_MANHA))[0]
    assert conflito["window_id"] == "piupiu"
    assert conflito["window_name"] == "Pros piupiu"
    assert conflito["start"] == "05:00" and conflito["end"] == "06:00"


# -------------------------------------------------------------- a sugestão


def test_a_sugestao_realmente_resolve_o_conflito():
    """A propriedade que importa: aplicar a sugestão zera a medida.

    Ela é escolhida testando as duas pontas com a mesma função que mede o
    problema, justamente para não poder discordar do aviso que a acompanha.
    """
    cfg = agenda(CINCO_DA_MANHA)
    proposto = scheduler.quiet_suggestion(CINCO_DA_MANHA, cfg)
    assert proposto is not None
    depois = dict(cfg, quiet_hours=proposto)
    assert scheduler.quiet_overlap(CINCO_DA_MANHA, depois)["kind"] == "none"


@pytest.mark.parametrize("inicio,fim", [
    ("05:00", "06:00"), ("02:00", "03:00"), ("19:30", "20:30"),
    ("06:30", "07:30"), ("21:00", "22:00"), ("23:00", "01:00"),
])
def test_toda_sugestao_que_sai_daqui_resolve(inicio, fim):
    alvo = janela(inicio, fim)
    cfg = agenda(alvo)
    proposto = scheduler.quiet_suggestion(alvo, cfg)
    assert proposto is not None, "%s-%s ficou sem conserto" % (inicio, fim)
    depois = dict(cfg, quiet_hours=proposto)
    assert scheduler.quiet_overlap(alvo, depois)["kind"] == "none", proposto


def test_a_sugestao_nunca_desliga_o_silencio_inteiro():
    """Uma janela igual ao silêncio só "cabe" se o silêncio virar zero.

    Zerar a proteção do bicho não é ajuste de horário e não pode sair de um
    clique -- melhor devolver nada e deixar o aviso explicar.
    """
    alvo = janela("20:00", "07:00")
    assert scheduler.quiet_suggestion(alvo, agenda(alvo)) is None
    conflito = scheduler.quiet_conflicts(agenda(alvo))[0]
    assert conflito["kind"] == "full"
    assert conflito["suggestion"] is None


def test_a_sugestao_encolhe_a_ponta_certa():
    """05:00 sai pela manhã do silêncio; 21:00 sai pela noite."""
    manha = scheduler.quiet_suggestion(CINCO_DA_MANHA, agenda(CINCO_DA_MANHA))
    assert manha == {"start": "20:00", "end": "05:00"}
    noite = scheduler.quiet_suggestion(janela("21:00", "22:00"), agenda())
    assert noite == {"start": "22:00", "end": "07:00"}


# ------------------------------------------------------------------- o aviso


def test_a_tela_para_de_dizer_que_nao_ha_nada_marcado():
    """A mentira mais cara desta tela, e a que ele leu."""
    prox = scheduler.next_change(dt.datetime(2026, 8, 18, 4, 0), agenda(CINCO_DA_MANHA))
    assert prox["event"] == "quiet_blocked"
    assert "Nada marcado" not in prox["message"]
    assert "silêncio" in prox["message"]
    assert prox["window_id"] == "piupiu"


def test_a_frase_diz_o_nome_o_horario_e_a_consequencia():
    frase = scheduler.quiet_conflict_message(scheduler.quiet_conflicts(agenda(CINCO_DA_MANHA)))
    assert "Pros piupiu" in frase
    assert "05:00" in frase and "06:00" in frase
    assert "não vai tocar" in frase, "estado sem consequência não serve: %r" % frase


def test_a_janela_sem_nome_ainda_da_para_achar_na_tela():
    frase = scheduler.quiet_conflict_message(
        scheduler.quiet_conflicts(agenda(janela("05:00", "06:00", name=""))))
    assert "05:00" in frase


def test_varias_janelas_caladas_viram_uma_frase_so():
    cfg = agenda(janela("05:00", "06:00", id="a"), janela("02:00", "03:00", id="b"))
    frase = scheduler.quiet_conflict_message(scheduler.quiet_conflicts(cfg))
    assert "2 horários" in frase


def test_a_frase_separa_o_que_nao_toca_do_que_para_no_meio():
    cfg = agenda(janela("05:00", "06:00", id="a"), janela("19:30", "20:30", id="b"))
    frase = scheduler.quiet_conflict_message(scheduler.quiet_conflicts(cfg))
    assert "1 não vai tocar" in frase and "1 vai parar" in frase, frase


def test_sem_janela_nenhuma_a_frase_antiga_continua_valendo():
    """"Nada marcado" é verdade quando não há nada marcado -- só ali."""
    prox = scheduler.next_change(dt.datetime(2026, 8, 18, 4, 0), agenda())
    assert prox["event"] == "none"
    assert prox["message"] == "Nada marcado para a próxima semana."


def test_a_janela_que_toca_continua_dizendo_a_que_horas():
    prox = scheduler.next_change(dt.datetime(2026, 8, 18, 4, 0), agenda(janela("17:00", "18:00")))
    assert prox["event"] == "starts"
    assert prox["message"] == "Toca às 17:00"


# --------------------------------------------------- o aviso chegando na tela


def test_a_agenda_devolve_o_conflito_para_a_tela(auth_client):
    """A tela não recalcula nada: quem mede é o servidor, numa chamada só."""
    corpo = auth_client.put("/api/apps/birdtunes/schedule", json={
        "enabled": True,
        "quiet_hours": {"start": "20:00", "end": "07:00"},
        "windows": [CINCO_DA_MANHA],
    })
    assert corpo.status_code == 200, corpo.text

    agenda_agora = auth_client.get("/api/apps/birdtunes/schedule").json()
    assert "quiet_conflicts" in agenda_agora, (
        "a lista tem que vir sempre, e não só quando o silêncio é o aviso da vez"
    )
    conflitos = agenda_agora["quiet_conflicts"]
    assert len(conflitos) == 1 and conflitos[0]["kind"] == "full"
    assert agenda_agora["next_change"]["event"] == "quiet_blocked"


def test_com_tudo_pronto_o_silencio_vira_o_aviso_da_vez(auth_client, monkeypatch):
    """Sem caixa de som o aviso é a caixa de som -- é o impedimento mais fundo.

    Resolvidos os de baixo, o silêncio tem que aparecer, senão a tela volta a
    dizer que está tudo certo com um horário que não vai tocar.
    """
    from project_os.apps.birdtunes import app as modulo

    monkeypatch.setattr(
        modulo.library, "candidate_set",
        lambda db, playlist_id, output: ([{"id": "t1"}], ""),
    )
    assert auth_client.put(
        "/api/apps/birdtunes/config", json={"output.type": "airplay"}).status_code == 200
    assert auth_client.put("/api/apps/birdtunes/schedule", json={
        "enabled": True,
        "quiet_hours": {"start": "20:00", "end": "07:00"},
        "windows": [CINCO_DA_MANHA],
    }).status_code == 200

    bloqueio = auth_client.get("/api/apps/birdtunes/schedule").json()["blocked"]
    assert bloqueio is not None, "a janela das 05:00 não vai tocar e ninguém avisou"
    assert bloqueio["code"] == "quiet_hours"
    assert "silêncio" in bloqueio["message"]
    assert bloqueio["suggestion"] == {"start": "20:00", "end": "05:00"}


def test_um_horario_que_toca_nao_gera_aviso_nenhum(auth_client, monkeypatch):
    from project_os.apps.birdtunes import app as modulo

    monkeypatch.setattr(
        modulo.library, "candidate_set",
        lambda db, playlist_id, output: ([{"id": "t1"}], ""),
    )
    auth_client.put("/api/apps/birdtunes/config", json={"output.type": "airplay"})
    auth_client.put("/api/apps/birdtunes/schedule", json={
        "enabled": True,
        "quiet_hours": {"start": "20:00", "end": "07:00"},
        "windows": [janela("17:00", "18:00")],
    })
    assert auth_client.get("/api/apps/birdtunes/schedule").json()["blocked"] is None


# ------------------------------------------------------------------ a pintura


def test_o_painel_sabe_desenhar_o_conflito():
    fonte = open(PAINEL, encoding="utf-8").read()
    assert "quiet_conflicts" in fonte, "a lista de horários precisa marcar cada janela"
    assert "'quiet_hours'" in fonte, "o aviso de cima precisa reconhecer o código novo"
    assert "bt-window__quiet" in fonte


def test_o_botao_de_ajustar_so_aparece_com_sugestao_do_servidor():
    """Sem sugestão medida não há botão: um clique que não resolve é pior que
    nenhum."""
    fonte = open(PAINEL, encoding="utf-8").read()
    assert "blocked.code === 'quiet_hours' && blocked.suggestion" in fonte


def test_as_frases_novas_existem_em_portugues():
    pt = open(PT, encoding="utf-8").read()
    for chave in ("bt.schedule.quiet_fix", "bt.schedule.window.muted",
                  "bt.schedule.window.clipped"):
        assert "'%s':" % chave in pt, "falta a tradução de %s" % chave


def test_a_marca_da_janela_tem_estilo():
    css = open(CSS, encoding="utf-8").read()
    assert ".bt-window__quiet" in css
    assert '[data-kind="partial"]' in css, (
        "cortada pela metade e calada por inteiro não podem ter a mesma cara"
    )


def test_a_sugestao_nao_troca_o_conflito_por_uma_noite_sem_silencio():
    """Encurtar a ponta errada também "resolve" -- destruindo a proteção.

    Silêncio 20:00-07:00 com uma janela às 21:00: mexer no fim livra a janela e
    de quebra deixa a madrugada inteira liberada, de onze horas de silêncio
    para uma. Entre as saídas que funcionam vence a que preserva mais silêncio.
    """
    alvo = janela("21:00", "22:00")
    proposto = scheduler.quiet_suggestion(alvo, agenda(alvo))
    assert proposto == {"start": "22:00", "end": "07:00"}
    assert scheduler._minutos_de_silencio(proposto) == 9 * 60


@pytest.mark.parametrize("inicio,fim", [
    ("05:00", "06:00"), ("02:00", "03:00"), ("19:30", "20:30"),
    ("06:30", "07:30"), ("21:00", "22:00"), ("23:00", "01:00"),
])
def test_nenhuma_sugestao_derruba_mais_da_metade_do_silencio(inicio, fim):
    """Trava geral: o conserto de um clique nunca custa a maior parte da
    proteção, seja qual for o horário que ele marcar."""
    alvo = janela(inicio, fim)
    cfg = agenda(alvo)
    antes = scheduler._minutos_de_silencio(cfg["quiet_hours"])
    proposto = scheduler.quiet_suggestion(alvo, cfg)
    assert proposto is not None
    depois = scheduler._minutos_de_silencio(proposto)
    assert depois >= antes / 2.0, (
        "%s-%s derrubaria o silêncio de %d para %d minutos" % (inicio, fim, antes, depois))


@pytest.mark.parametrize("caladas,cortadas,esperado", [
    (1, 1, "1 não vai tocar e 1 vai parar antes do fim."),
    (2, 1, "2 não vão tocar e 1 vai parar antes do fim."),
    (1, 2, "1 não vai tocar e 2 vão parar antes do fim."),
])
def test_a_frase_misturada_concorda_em_numero(caladas, cortadas, esperado):
    """Saiu "1 não vão tocar e 1 vão parar" na primeira volta.

    A frase é a única coisa que ele lê da tela; escrita errada ela já perde
    metade da autoridade que precisa ter para ele acreditar e ir consertar.
    """
    janelas = []
    for i in range(caladas):
        janelas.append(janela("0%d:00" % (i + 1), "0%d:30" % (i + 1), id="c%d" % i))
    for i in range(cortadas):
        janelas.append(janela("19:%02d" % (i * 5), "20:30", id="p%d" % i))
    frase = scheduler.quiet_conflict_message(scheduler.quiet_conflicts(agenda(*janelas)))
    assert frase.endswith(esperado), frase
