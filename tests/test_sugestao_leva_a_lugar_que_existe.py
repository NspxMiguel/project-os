"""Todo botão de sugestão tem que cair numa tela que existe.

O painel é a primeira coisa que aparece depois de entrar, e os cartões de
sugestão são a parte dele que dá conselho. Sete dos destinos não existiam:

* ``#/home/providers/tuya``, ``#/home/energy``, ``#/home/nodes`` -- nunca houve
  uma rota ``#/home`` neste build;
* ``#/store/home-assistant`` e ``#/store/esphome`` -- a loja só tinha ``#/store``;
* ``#/devices/<id>/flash`` -- o roteador casa por número de segmentos
  (``web/lib/router.js``), então três segmentos caíam no "não achei";
* ``#/settings/integrations`` e ``#/settings/security`` -- seções que a tela de
  Configurações não conhecia, e que desenhavam as abas com nada embaixo.

Bastava espetar um ESP32 na USB para nascer um cartão vermelho de prioridade
alta que levava a uma página de erro.

Este teste lê as rotas de ``web/app.js`` e os destinos de ``suggestions.py`` e
compara os dois. É o mesmo casamento que o roteador faz em tempo de execução:
mesma quantidade de segmentos, ``:param`` casa com qualquer coisa.
"""

from __future__ import annotations

import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rotas_registradas():
    """Todo padrão passado para r.add / r.addAll em web/app.js."""
    with open(os.path.join(RAIZ, "web", "app.js"), encoding="utf-8") as arquivo:
        texto = arquivo.read()
    return set(re.findall(r"'(#/[^']*)'\s*:", texto)) | set(
        re.findall(r"r\.add\(\s*'(#/[^']*)'", texto)
    )


def casa(destino: str, rotas) -> bool:
    caminho = destino.split("?", 1)[0]
    partes = [p for p in caminho.split("/") if p]
    for rota in rotas:
        alvo = [p for p in rota.split("/") if p]
        if len(alvo) != len(partes):
            continue
        if all(a.startswith(":") or a == p for a, p in zip(alvo, partes)):
            return True
    return False


def destinos_das_sugestoes():
    with open(os.path.join(RAIZ, "project_os", "core", "suggestions.py"), encoding="utf-8") as arquivo:
        texto = arquivo.read()
    crus = re.findall(r'"href":\s*"([^"]+)"', texto)
    # `#/devices/%s/flash` era assim: o %s é um id, e conta como um segmento.
    return [c.replace("%s", "algum-id") for c in crus]


def test_todo_destino_de_sugestao_tem_rota():
    rotas = rotas_registradas()
    assert "#/store/:id" in rotas, "a loja precisa de rota por item"
    quebrados = [d for d in destinos_das_sugestoes() if not casa(d, rotas)]
    assert not quebrados, "sugestões apontando para o vazio: %s" % ", ".join(quebrados)


def test_as_secoes_de_configuracoes_citadas_existem():
    """``#/settings/integrations`` casa com ``#/settings/:section`` e mesmo assim
    desenhava uma tela vazia: a seção precisa estar na lista de abas."""
    with open(os.path.join(RAIZ, "web", "views", "settings.js"), encoding="utf-8") as arquivo:
        texto = arquivo.read()
    linha = re.search(r"const SECTIONS = \[(.*?)\]", texto, re.S).group(1)
    secoes = set(re.findall(r"'([a-z]+)'", linha))
    for destino in destinos_das_sugestoes():
        if destino.startswith("#/settings/"):
            secao = destino.split("/")[2].split("?")[0]
            assert secao in secoes, "a aba %r não existe em Configurações" % secao


def test_nenhuma_sugestao_oferece_app_que_nao_existe(monkeypatch):
    """Kasa e MQTT eram oferecidos como um clique; nunca foram escritos."""
    from project_os.core import suggestions

    class PluginsSoComOsQueExistem(object):
        def has(self, app_id):
            return app_id in ("birdtunes", "whatsapp-bot")

        def __contains__(self, app_id):
            return self.has(app_id)

    class RegistroFalso(object):
        def devices(self):
            return [
                {"id": "a", "kind": "smart_plug", "display_name": "Tomada da sala",
                 "address": "192.168.1.30", "capabilities": ["switch", "energy"],
                 "properties": {"vendor": "TP-Link Kasa"}},
                {"id": "b", "kind": "mqtt_broker", "display_name": "Broker",
                 "address": "192.168.1.31", "capabilities": [], "properties": {}},
            ]

        def summary(self):
            return {"available": True}

    engine = suggestions.SuggestionEngine(
        config=None, db=None, devices=RegistroFalso(), plugins=PluginsSoComOsQueExistem()
    )
    ids = {item["id"] for item in engine.evaluate()}
    assert "kasa-enable" not in ids
    assert "mqtt-connect" not in ids


def test_o_cartao_do_kasa_volta_quando_o_app_existir():
    """A porta não foi fechada com prego: o cartão espera o app, não sumiu."""
    from project_os.core import suggestions

    class PluginsComKasa(object):
        def has(self, app_id):
            return True

        def __contains__(self, app_id):
            return True

    class RegistroFalso(object):
        def devices(self):
            return [{"id": "a", "kind": "smart_plug", "display_name": "Tomada",
                     "address": "192.168.1.30", "capabilities": ["switch"],
                     "properties": {"vendor": "TP-Link Kasa"}}]

        def summary(self):
            return {"available": True}

    engine = suggestions.SuggestionEngine(
        config=None, db=None, devices=RegistroFalso(), plugins=PluginsComKasa()
    )
    assert "kasa-enable" in {item["id"] for item in engine.evaluate()}
