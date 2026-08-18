"""Campo de cartão que chega no formatador errado vira "—" e ninguém percebe.

O contrato do cartão está escrito no topo de ``web/views/dashboard.js``: cada
campo tem ``kind``, e o ``kind`` escolhe o formatador. Os de data e número
passam o valor por ``new Date(...)`` ou ``Number(...)``, que **não levantam
erro** com o tipo errado -- devolvem ``Invalid Date`` e ``NaN``, e a tela
imprime um travessão.

Foi o que aconteceu com o BirdTunes: ``next_change`` é um dicionário
(``{event, at, window_id, message}``) e ia inteiro num campo ``relative``.
``String({...})`` é ``"[object Object]"``, então o cartão dele mostrou
"Próxima mudança: —" desde o primeiro dia -- justamente o campo que existia
para dizer a que horas a agenda ia tocar.

Nada explodiu, nenhum log saiu, nenhum teste falhou. Por isso a trava é geral e
não sobre o BirdTunes: vale para todo app que desenha cartão, agora e depois.
"""

from __future__ import annotations

import pytest

#: Os ``kind`` que passam o valor por um formatador de escalar. Um dicionário
#: ou lista em qualquer um deles vira "—" ou "NaN" na tela.
KINDS_DE_ESCALAR = {
    "relative": (str, int, float),
    "datetime": (str, int, float),
    "time": (str, int, float),
    "percent": (int, float),
    "number": (int, float),
    "progress": (int, float),
    "temperature": (int, float),
    "bytes": (int, float),
    "duration": (int, float),
    "text": (str, int, float, bool),
    "badge": (str, int, float, bool),
}


def _apps_ligados(auth_client):
    corpo = auth_client.get("/api/apps").json()
    itens = corpo.get("apps", corpo) if isinstance(corpo, dict) else corpo
    return [a["id"] for a in itens if a.get("enabled", True) and a.get("state") != "error"]


def test_todo_app_ligado_responde_status(auth_client):
    ligados = _apps_ligados(auth_client)
    assert ligados, "nenhum app ligado: o resto do arquivo não provaria nada"


def test_nenhum_campo_de_cartao_chega_com_o_tipo_errado(auth_client):
    """A trava: escalar onde o formatador espera escalar."""
    problemas = []
    for app_id in _apps_ligados(auth_client):
        resposta = auth_client.get("/api/apps/%s/status" % app_id)
        if resposta.status_code != 200:
            continue
        corpo = resposta.json()
        for campo in (corpo.get("fields") or []):
            kind = campo.get("kind", "text")
            esperado = KINDS_DE_ESCALAR.get(kind)
            if esperado is None:
                continue
            valor = campo.get("value")
            if valor is None:
                continue
            if not isinstance(valor, esperado):
                problemas.append(
                    "%s: campo %r (kind=%s) mandou %s -- a tela vai mostrar um travessão"
                    % (app_id, campo.get("label"), kind, type(valor).__name__))
    assert not problemas, "\n".join(problemas)


def test_todo_campo_tem_rotulo_e_kind_conhecido(auth_client):
    """Um ``kind`` que o dashboard não conhece cai no caso padrão e some."""
    conhecidos = set(KINDS_DE_ESCALAR) | {"boolean", "list", "link"}
    problemas = []
    for app_id in _apps_ligados(auth_client):
        resposta = auth_client.get("/api/apps/%s/status" % app_id)
        if resposta.status_code != 200:
            continue
        for campo in (resposta.json().get("fields") or []):
            if not str(campo.get("label") or "").strip():
                problemas.append("%s: campo sem rótulo" % app_id)
            kind = campo.get("kind", "text")
            if kind not in conhecidos:
                problemas.append("%s: kind desconhecido %r" % (app_id, kind))
    assert not problemas, "\n".join(problemas)


def test_o_campo_do_birdtunes_diz_alguma_coisa(auth_client):
    """O caso que originou a trava, preso pelo nome."""
    corpo = auth_client.get("/api/apps/birdtunes/status").json()
    campos = {c.get("label"): c for c in (corpo.get("fields") or [])}
    campo = campos.get("Próxima mudança")
    assert campo is not None, "o cartão parou de dizer quando a agenda toca"
    assert isinstance(campo["value"], str) and campo["value"].strip()
    assert campo["value"] != "[object Object]"


# ------------------------------------------- "nenhuma escolhida" que mentia


def test_caixa_escolhida_e_desconectada_nao_vira_nenhuma_escolhida():
    """No Pi dele: um Chromecast configurado, e o cartão dizendo que não havia
    caixa nenhuma -- porque o campo lido só existe enquanto o tocador está
    conectado. Manda escolher de novo o que já estava escolhido, quando o que
    aconteceu foi a TV sair do ar."""
    from project_os.apps.birdtunes.app import BirdTunesApp

    valor = BirdTunesApp._nome_da_caixa(None, {"output": "chromecast", "device": ""})
    assert valor != "Nenhuma escolhida"
    assert "conexão" in valor


def test_sem_saida_escolhida_continua_dizendo_que_nao_tem():
    from project_os.apps.birdtunes.app import BirdTunesApp

    assert BirdTunesApp._nome_da_caixa(None, {"output": "null", "device": ""}) == "Nenhuma escolhida"


def test_conectada_mostra_o_nome_da_caixa():
    from project_os.apps.birdtunes.app import BirdTunesApp

    valor = BirdTunesApp._nome_da_caixa(None, {"output": "chromecast", "device": "TV Quarto Miguel"})
    assert valor == "TV Quarto Miguel"
