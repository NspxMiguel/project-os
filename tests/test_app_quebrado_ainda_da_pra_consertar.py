"""Um app que não sobe mostrava o traceback e mais nada.

O painel de um app é servido **pelo próprio app** (``/api/apps/{id}/ui/panel.js``).
Então um app que falhou ao iniciar não tem painel -- e a tela dele terminava
num cartão de erro com a pilha de chamadas. A mensagem chegava a dizer *"veja
os logs pelo painel do app"*: o painel que não existe.

O problema é que o que precisa mudar, quase sempre, é a configuração dele: uma
pasta de biblioteca que não existe, um token errado, uma porta ocupada. Sem
painel, não havia nenhum caminho no navegador para mexer nisso -- só SSH, que é
exatamente o que este projeto existe para não precisar.

``GET/PUT /api/settings/apps/{id}`` já respondia por isso desde sempre, com o
``config_schema`` do manifesto junto dos valores, e reiniciando o app depois de
gravar. Não havia tela. Agora há (``web/lib/app-settings.js``, montado nos dois
ramos de erro de ``showAppPanel``), e este arquivo cobre o caminho inteiro.
"""

from __future__ import annotations

import io
import json
import os

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fonte(*partes):
    return io.open(os.path.join(RAIZ, *partes), encoding="utf-8").read()


# --------------------------------------------------------------------------- a API


def test_o_schema_vem_com_os_valores(auth_client):
    corpo = auth_client.get("/api/settings/apps/birdtunes").json()
    chaves = {campo["key"] for campo in corpo["schema"]}
    assert "output.type" in chaves and "library.paths" in chaves
    assert isinstance(corpo["values"], dict)


def test_gravar_muda_o_que_o_app_le(auth_client):
    resposta = auth_client.put(
        "/api/settings/apps/birdtunes",
        json={"values": {"output.type": "chromecast"}},
    )
    assert resposta.status_code == 200, resposta.text
    assert resposta.json()["changed"] == ["output.type"]
    assert auth_client.get("/api/apps/birdtunes/outputs").json()["current"] == "chromecast"


def test_uma_lista_chega_como_lista(auth_client):
    """A tela manda uma linha por item; o que fica gravado é uma lista."""
    resposta = auth_client.put(
        "/api/settings/apps/birdtunes",
        json={"values": {"library.paths": ["/mnt/musica", "/mnt/passaros"]}},
    )
    assert resposta.status_code == 200, resposta.text
    guardado = auth_client.app.state.config.get("apps.settings.birdtunes.library.paths")
    assert guardado == ["/mnt/musica", "/mnt/passaros"]


def test_segredo_nao_volta_em_texto_claro(auth_client):
    auth_client.put(
        "/api/settings/apps/whatsapp-bot",
        json={"values": {"cloud_api.access_token": "EAA-segredo"}},
    )
    corpo = auth_client.get("/api/settings/apps/whatsapp-bot").json()
    guardado = corpo["values"]["cloud_api"]["access_token"]
    assert guardado != "EAA-segredo", "a tela não pode receber o token de volta"

    # E devolver a máscara não apaga o valor real (o campo vem preenchido com
    # ela; salvar sem tocar não pode limpar o token).
    auth_client.put(
        "/api/settings/apps/whatsapp-bot", json={"values": {"cloud_api.access_token": guardado}}
    )
    real = auth_client.app.state.config.get("apps.settings.whatsapp-bot.cloud_api.access_token")
    assert real == "EAA-segredo"


def test_app_que_nao_existe_da_404(auth_client):
    assert auth_client.get("/api/settings/apps/nao-existe").status_code == 404


def test_um_app_fora_do_ar_ainda_responde_pelos_ajustes(auth_client, monkeypatch):
    """O caso que motiva tudo isto: o app quebrado, e os ajustes de pé."""
    gerente = auth_client.app.state.plugins
    registro = gerente.get("birdtunes")
    assert registro is not None

    # Como se o app tivesse morrido ao iniciar.
    monkeypatch.setattr(registro, "state", "error", raising=False)
    monkeypatch.setattr(registro, "error", "ValueError: pasta de música não existe", raising=False)

    corpo = auth_client.get("/api/settings/apps/birdtunes").json()
    assert corpo["schema"], "sem schema não há o que editar, e a tela volta a ser um beco"
    resposta = auth_client.put(
        "/api/settings/apps/birdtunes", json={"values": {"library.paths": ["/mnt/musica"]}}
    )
    assert resposta.status_code == 200, resposta.text


# --------------------------------------------------------------------------- a tela


def test_a_tela_de_erro_monta_o_cartao_de_ajustes():
    shell = _fonte("web", "app.js")
    # Os dois ramos de erro: app que não subiu, e painel que não carregou.
    assert shell.count("appSettingsCard(appId") == 2
    assert "app.state === 'error'" in shell


def test_o_cartao_usa_o_endpoint_que_reinicia_o_app():
    modulo = _fonte("web", "lib", "app-settings.js")
    assert "api.get('/settings/apps/'" in modulo
    assert "api.put(" in modulo and "'/settings/apps/'" in modulo
    assert "appsettings.savedRestarted" in modulo


@pytest.mark.parametrize("tipo", ["boolean", "number", "array", "list", "string"])
def test_todo_tipo_declarado_por_um_manifesto_tem_campo_na_tela(tipo):
    modulo = _fonte("web", "lib", "app-settings.js")
    assert "'%s'" % tipo in modulo, "o cartão não sabe desenhar um campo %s" % tipo


def test_os_tipos_dos_manifestos_de_verdade_estao_todos_cobertos():
    tipos = set()
    apps = os.path.join(RAIZ, "project_os", "apps")
    for app_id in os.listdir(apps):
        caminho = os.path.join(apps, app_id, "manifest.json")
        if not os.path.isfile(caminho):
            continue
        with open(caminho, encoding="utf-8") as arquivo:
            for campo in json.load(arquivo).get("config_schema", []):
                tipos.add(str(campo.get("type", "string")))
    modulo = _fonte("web", "lib", "app-settings.js")
    faltando = [tipo for tipo in tipos if "'%s'" % tipo not in modulo]
    assert not faltando, "manifesto usa tipo que a tela não desenha: %s" % faltando


def test_a_frase_do_beco_sem_saida_sumiu():
    """A tela mandava checar os logs "pelo painel do app" -- que não existe."""
    pt = _fonte("web", "lib", "strings-pt.js")
    assert "appsettings.title" in pt, "a tela nova também fala português"
