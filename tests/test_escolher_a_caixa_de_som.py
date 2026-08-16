"""Abrir "Onde toca" e não ter caixa de som nenhuma para escolher.

*"n consigo selecionar dispositivo"*.

Reproduzido numa caixa 0.4.14 com 32 aparelhos achados na rede: a folha abria com
um seletor só, "Conexão", em "Nenhuma (só registra)" -- e mais nada. A lista de
caixas de som era montada dentro de um ``currentBackend === 'null' ? null : ...``,
quer dizer, só existia **depois** de trocar a conexão para AirPlay ou Chromecast.
Quem abriu a folha para escolher a caixa não tinha caixa para escolher, e a tela
não dizia que faltava um passo antes.

O passo não devia existir. Ninguém em casa sabe que HomePod fala AirPlay e que a
Google Home fala Chromecast -- isso está no próprio aparelho, e o app já sabe
(``device_kinds`` de cada backend). Agora a folha lista as caixas de som direto,
uma lista só, e escolher uma grava a conexão junto.

E tinha um segundo motivo para não conseguir escolher: o mesmo HomePod aparecia
duas vezes, com o mesmo nome. O id era ``<tipo>:<MAC>`` e o tipo é palpite -- o
mesmo aparelho responde ao mDNS ora com o modelo (``homepod``), ora sem ele
(``apple_tv``). Cada palpite abria uma linha nova e a antiga ficava no banco. Na
hora de escolher eram dois nomes idênticos e nenhuma forma de saber qual.
"""

from __future__ import annotations

import io
import os

import pytest

pytestmark = pytest.mark.usefixtures("home")

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAINEL = os.path.join(RAIZ, "project_os", "apps", "birdtunes", "web", "panel.js")
PT = os.path.join(RAIZ, "web", "lib", "strings-pt.js")


def _painel():
    return io.open(PAINEL, encoding="utf-8").read()


def _folha():
    """Só o corpo de renderSheet(), que é onde a folha se monta."""
    fonte = _painel()
    inicio = fonte.index("function renderSheet()")
    return fonte[inicio:fonte.index("\n    // ", inicio + 10)]


# --------------------------------------------------------------------------- a folha


def test_a_lista_de_caixas_nao_depende_de_escolher_a_conexao_antes():
    """O bug inteiro cabia num operador ternário."""
    folha = _folha()
    assert "currentBackend === 'null' ? null" not in folha, (
        "era isto que fazia a folha abrir sem caixa de som nenhuma para escolher"
    )
    assert "t2('bt.output.device')" in folha


def test_escolher_a_caixa_grava_a_conexao_junto():
    """O aparelho diz por onde toca; a pergunta não volta para quem escolheu."""
    folha = _folha()
    assert "backendPorTipo[alvo.kind].kind" in folha
    assert "device_id: alvo ? alvo.id : ''" in folha


def test_so_aparece_caixa_que_alguma_conexao_saiba_tocar():
    """A rede tem 32 aparelhos; impressora não é caixa de som."""
    folha = _folha()
    assert "devices.filter((d) => backendPorTipo[d.kind])" in folha


def test_a_caixa_que_este_cartao_nao_toca_diz_o_porque_antes_do_clique():
    folha = _folha()
    assert "disabled: !backendPorTipo[d.kind].available" in folha
    assert "backendPorTipo[d.kind].hint" in folha


def test_sem_caixa_nenhuma_a_folha_oferece_procurar():
    """"Procure na tela Aparelhos" é mandar a pessoa embora da tela onde ela está."""
    folha = _folha()
    assert "usable.length === 0" in folha
    assert "t2('bt.output.scan')" in folha
    assert "/devices/scan" in folha


def test_a_conexao_vira_informacao_e_nao_pergunta():
    folha = _folha()
    assert "fmtStr('bt.output.via'" in folha, (
        "t2() não troca parâmetro nenhum -- com ele a frase iria para a tela com o %s"
    )


def test_o_portugues_das_frases_novas_existe():
    pt = io.open(PT, encoding="utf-8").read()
    for chave in ("bt.output.via", "bt.output.scan", "bt.output.none", "bt.output.empty"):
        assert "'%s':" % chave in pt, "falta o português de %s" % chave
    assert "'bt.output.via': 'Toca por %s.'" in pt, "a frase precisa do %s para o nome entrar"


def test_a_frase_da_conexao_nao_ficou_para_tras():
    """Rótulo de um campo que não existe mais é frase morta nos dois dicionários."""
    assert "bt.output.backend" not in _painel()
    assert "bt.output.backend" not in io.open(PT, encoding="utf-8").read()


# --------------------------------------------------------------------------- a tela muda


def test_uma_chamada_que_falhou_nao_passa_calada():
    """``catch { return null }`` é o que fazia a tela do print dele mentir.

    Com ``/outputs`` falhando, ``backends`` fica vazio e o seletor aparece sem
    opção nenhuma -- a caixa vazia do print. Com ``/stats`` falhando, os quatro
    números viram "—". Nada dizia que não tinha sido possível perguntar.
    """
    fonte = _painel()
    inicio = fonte.index("async function safeGet(")
    trecho = fonte[inicio:fonte.index("\n    }", inicio)]
    assert "state.offline = {" in trecho, "o erro precisa ficar anotado em algum lugar"
    assert "state.offline = null" in trecho, "e precisa sair quando a resposta vem"


def test_a_tela_diz_quando_nao_esta_falando_com_o_app():
    fonte = _painel()
    assert "function offlineWarning()" in fonte
    assert "nodes.unshift(offlineWarning());" in fonte, "a função existe e ninguém chama"
    assert "t2('bt.offline.retry')" in fonte, "avisar sem dar como tentar de novo é meio aviso"


def test_nao_conseguir_perguntar_nao_e_nao_ter_achado():
    """Procurar de novo não resolve um app que não responde."""
    folha = _folha()
    assert "t2(state.offline ? 'bt.offline' : 'bt.output.empty')" in folha
    assert "usable.length === 0 && !state.offline" in folha, (
        "o botão de procurar não pode aparecer quando nem dá para perguntar"
    )


def test_o_portugues_do_aviso_de_fora_do_ar_existe():
    pt = io.open(PT, encoding="utf-8").read()
    for chave in ("bt.offline", "bt.offline.unreachable", "bt.offline.retry"):
        assert "'%s':" % chave in pt, "falta o português de %s" % chave


# --------------------------------------------------------------------------- o aparelho repetido


def _observar(kind, mac, nome, servico="_airplay._tcp.local.", propriedades=None):
    from project_os.core.discovery import Observation

    return Observation(
        source="mdns", service_type=servico, instance=nome, kind=kind,
        name_hint=nome, address="10.0.0.42", host="homepod-direito", port=7000,
        capabilities=["audio_out"], properties=propriedades or {},
        strong_keys=["mac:" + mac],
    )


def _varrer(client, observacoes):
    from project_os.core.discovery import merge_observations

    registry = client.app.state.devices
    devices = merge_observations(observacoes)
    registry._persist(devices)
    return devices


def test_o_mesmo_aparelho_com_outro_palpite_de_tipo_nao_vira_linha_nova(auth_client):
    mac = "22d61dba1645"
    primeiro = _varrer(auth_client, [_observar("homepod", mac, "HomePod Direito")])[0]
    _varrer(auth_client, [_observar("apple_tv", mac, "HomePod Direito")])

    linhas = auth_client.get("/api/devices").json()["devices"]
    iguais = [d for d in linhas if mac in d["id"]]
    assert len(iguais) == 1, "o mesmo HomePod ficou na lista %d vezes" % len(iguais)
    assert iguais[0]["id"] == primeiro.id, (
        "o id tem que continuar o mesmo: é ele que está guardado em output.device_id"
    )


def test_o_tipo_da_linha_acompanha_a_ultima_varredura(auth_client):
    """Manter o id não é manter o palpite: quem toca o aparelho olha o kind."""
    mac = "22d61dba1645"
    _varrer(auth_client, [_observar("apple_tv", mac, "HomePod Direito")])
    _varrer(auth_client, [_observar("homepod", mac, "HomePod Direito")])

    linha = [d for d in auth_client.get("/api/devices").json()["devices"] if mac in d["id"]][0]
    assert linha["kind"] == "homepod"


def test_o_que_o_usuario_decidiu_atravessa_a_juncao(auth_client):
    """Nome dado, fixado e ignorado não são cache de varredura."""
    mac = "22d61dba1645"
    velho = _varrer(auth_client, [_observar("homepod", mac, "HomePod Direito")])[0]
    resposta = auth_client.patch(
        "/api/devices/" + velho.id, json={"name": "Caixa da sala", "pinned": True}
    )
    assert resposta.status_code == 200, resposta.text

    _varrer(auth_client, [_observar("apple_tv", mac, "HomePod Direito")])
    linha = [d for d in auth_client.get("/api/devices").json()["devices"] if mac in d["id"]][0]
    assert linha["custom_name"] == "Caixa da sala"
    assert linha["pinned"] is True


def test_dois_aparelhos_diferentes_continuam_dois(auth_client):
    """A junção é por identidade; juntar por nome apagaria caixa de som de verdade."""
    _varrer(auth_client, [_observar("homepod", "22d61dba1645", "HomePod Direito")])
    _varrer(auth_client, [_observar("homepod", "8ad2f226c37d", "HomePod Esquerdo")])

    linhas = auth_client.get("/api/devices").json()["devices"]
    assert len({d["id"] for d in linhas}) == 2


def test_a_identidade_e_o_que_sobra_depois_do_tipo():
    from project_os.core.discovery import _identity_token

    assert _identity_token("homepod:mac-22d61dba1645") == "mac-22d61dba1645"
    assert _identity_token("apple_tv:mac-22d61dba1645") == "mac-22d61dba1645"
    assert _identity_token("apple_tv:10-0-0-42:7000") == "10-0-0-42:7000"
    assert _identity_token("sem-dois-pontos") == ""
    assert _identity_token("") == ""
