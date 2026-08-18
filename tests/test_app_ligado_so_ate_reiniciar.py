""""Iniciar" um app desligado dava um app que sumia no próximo boot.

O log da caixa dele conta a história inteira em duas linhas, com dois dias de
distância entre elas e nada no meio:

    15:05:41  project-os 0.4.21 starting
    15:05:41  apps: 2 discovered, 0 running
    15:05:53  app birdtunes: start (by admin)

O BirdTunes tocou por dois dias porque alguém apertou "Iniciar" naquela sessão
-- não porque estivesse ligado. ``apps.enabled`` continuou ``[]``. Reiniciar a
caixa (uma atualização, uma queda de luz, um ``systemctl restart``) devolvia
"0 running", e o app cuja única função é fazer coisa na hora marcada não estava
lá para fazer.

``start`` sobe o app agora; ``enable`` escreve em ``apps.enabled`` **e** sobe.
A tela oferecia ``start`` para app desligado, que é a armadilha: funciona na
hora, e a promessa de "toca todo dia às 8" não sobrevive à tomada.

Duas travas: o botão de app desligado tem que chamar ``enable``, e um app que
esteja rodando fora da lista tem que dizer isso na cara, com o botão que
resolve.
"""

from __future__ import annotations

import os

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELA = os.path.join(RAIZ, "web", "views", "apps.js")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")


def test_start_nao_persiste_e_enable_persiste(tmp_path, monkeypatch):
    """O fato por trás de tudo, medido na configuração de verdade."""
    monkeypatch.setenv("PROJECT_OS_HOME", str(tmp_path))
    from project_os.config import load_config

    cfg = load_config()
    assert cfg.get("apps.enabled", None) == [], (
        "o padrão é caixa sem nada ligado; se mudar, este arquivo precisa mudar junto"
    )


def test_o_botao_de_app_desligado_chama_enable():
    fonte = open(TELA, encoding="utf-8").read()
    trecho = fonte.split("function appCard(app)", 1)[1].split("return card(", 1)[0]
    assert "state_ === 'disabled'" in trecho
    desligado = trecho.split("state_ === 'disabled'", 1)[1][:220]
    assert "'enable'" in desligado, "app desligado voltou a receber start"
    assert "'start'" not in desligado.split("else", 1)[0], (
        "o ramo do desligado não pode chamar start"
    )


def test_app_parado_continua_com_start():
    """Parado é diferente de desligado: ele já está na lista, só não está de pé."""
    fonte = open(TELA, encoding="utf-8").read()
    trecho = fonte.split("function appCard(app)", 1)[1].split("return card(", 1)[0]
    assert "state_ === 'stopped' || state_ === 'error'" in trecho
    assert "actionButton(app, 'start'" in trecho


def test_rodando_fora_da_lista_vira_aviso():
    fonte = open(TELA, encoding="utf-8").read()
    assert "app.enabled === false" in fonte
    assert "apps.onlyForNow" in fonte
    assert "apps.action.keepOn" in fonte, "o aviso precisa do botão que resolve"


def test_o_aviso_fala_portugues():
    pt = open(PT, encoding="utf-8").read()
    for chave in ("apps.onlyForNow", "apps.onlyForNow.detail", "apps.action.keepOn"):
        assert "'%s':" % chave in pt, "falta a tradução de %s" % chave
    linha = [l for l in pt.splitlines() if "'apps.onlyForNow.detail'" in l][0]
    assert "reiniciar" in linha, "o aviso tem que dizer o que acontece: %s" % linha


def test_a_rota_de_apps_conta_se_esta_ligado(auth_client):
    """Sem este campo a tela não tem como saber que precisa avisar."""
    corpo = auth_client.get("/api/apps").json()
    apps = corpo.get("apps", corpo)
    assert apps, "sem app nenhum o teste não prova nada"
    for app in apps:
        assert "enabled" in app, "falta 'enabled' em %s" % app.get("id")


def _lista_de_ligados(auth_client):
    corpo = auth_client.get("/api/settings").json()
    valores = corpo.get("settings") or corpo.get("values") or corpo
    apps = valores.get("apps") or {}
    return apps.get("enabled")


def test_enable_escreve_na_lista_e_start_nao(auth_client):
    """A diferença entre os dois verbos, exercida pela mesma rota que a tela usa."""
    alvo = auth_client.get("/api/apps").json()
    apps = alvo.get("apps", alvo)
    app_id = apps[0]["id"]

    assert auth_client.post(
        "/api/apps/%s/action" % app_id, json={"action": "disable"}).status_code == 200
    depois_de_desligar = _lista_de_ligados(auth_client)
    assert app_id not in (depois_de_desligar or [])

    assert auth_client.post(
        "/api/apps/%s/action" % app_id, json={"action": "start"}).status_code == 200
    assert app_id not in (_lista_de_ligados(auth_client) or []), (
        "start não pode escrever na lista -- é o que faz dele temporário, e é "
        "por isso que a tela não pode oferecer start para app desligado"
    )

    assert auth_client.post(
        "/api/apps/%s/action" % app_id, json={"action": "enable"}).status_code == 200
    assert app_id in (_lista_de_ligados(auth_client) or []), (
        "enable tem que persistir, senão o app some no próximo boot"
    )
