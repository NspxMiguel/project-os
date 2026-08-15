"""A sugestão que faz o BirdTunes existir não podia aparecer nunca.

Numa casa com seis Chromecasts, três HomePods e uma Google Home -- medido na
rede de verdade --, o painel sugeria duas coisas, e nenhuma delas falava de
caixa de som. O motivo:

    targets = ctx.with_capability("airplay") + ctx.with_capability("cast")

``"airplay"`` e ``"cast"`` **não existem** no vocabulário da descoberta. Os
nomes de verdade são ``airplay_audio`` e ``cast_media``. Como a lista vinha
sempre vazia, ``rule_birdtunes_output`` ("achei caixas na rede e o BirdTunes não
tem onde tocar", que é o cartão que faz o app começar a servir para alguma
coisa) e ``rule_extra_speakers`` retornavam ``[]`` em qualquer casa do mundo.

É o mesmo erro que os players do BirdTunes tinham -- listas de nomes escritas de
cabeça, sem ninguém comparar com o que a descoberta produz. Lá isso já é teste
(``test_saida_de_som_aparece``); aqui não era.
"""

from __future__ import annotations

import pytest

from project_os.core import discovery, suggestions


def _caixa(device_id, kind, name, caps):
    return {
        "id": device_id, "kind": kind, "name": name, "display_name": name,
        "address": "10.0.0.9", "capabilities": list(caps), "properties": {},
        "online": True, "ignored": False,
    }


class ConfigFalso(object):
    def __init__(self, valores=None):
        self.valores = valores or {}

    def get(self, path, default=None):
        return self.valores.get(path, default)


class RegistroFalso(object):
    """O Context lê os aparelhos de um registro, não de uma lista."""

    def __init__(self, devices):
        self._devices = devices

    def devices(self, include_ignored=False):
        return list(self._devices)


def _contexto(devices, valores=None):
    return suggestions.Context(
        config=ConfigFalso(valores), db=None, devices=RegistroFalso(devices),
    )


# --------------------------------------------------------------------------- vocabulário


def test_toda_capacidade_que_uma_regra_pede_existe_na_descoberta():
    """O guarda que faltava: nome inventado vira regra morta, calada."""
    vocabulario = set()
    for caps in discovery.KIND_CAPABILITIES.values():
        vocabulario |= set(caps)

    ctx = _contexto([])
    pedidas = []

    original = suggestions.Context.with_capability

    def espiao(self, capability):
        pedidas.append(capability)
        return original(self, capability)

    suggestions.Context.with_capability = espiao
    try:
        for regra in suggestions.RULES:
            try:
                regra(ctx)
            except Exception:  # a regra pode precisar de mais contexto; o que
                pass          # importa aqui é o nome que ela pediu
    finally:
        suggestions.Context.with_capability = original

    inventadas = [nome for nome in pedidas if nome not in vocabulario]
    assert not inventadas, "regra pede capacidade que a descoberta nunca produz: %s" % inventadas


def test_todo_kind_que_uma_regra_pede_existe_na_descoberta():
    import re
    import inspect

    fonte = inspect.getsource(suggestions)
    pedidos = set(re.findall(r'of_kind\("([a-z_]+)"\)', fonte))
    inventados = sorted(pedidos - set(discovery.KIND_CAPABILITIES))
    assert not inventados, "regra pede tipo de aparelho que não existe: %s" % inventados


def test_toda_propriedade_que_uma_regra_le_alguem_escreve():
    """A terceira lista escrita de cabeça: as chaves de ``properties``.

    ``needs_local_key``, ``flashable``, ``vendor`` -- as regras filtram por
    essas chaves e quem as escreve é a descoberta, noutro arquivo. Um nome
    trocado aqui não quebra nada: só faz o cartão nunca aparecer.
    """
    import inspect
    import re

    fonte_regras = inspect.getsource(suggestions)
    fonte_descoberta = inspect.getsource(discovery)

    lidas = set(re.findall(r'properties["\']?\s*\)?\s*or\s*\{\}\)\.get\("([a-z_]+)"', fonte_regras))
    lidas |= set(re.findall(r'\.get\("properties"[^)]*\)\s*or\s*\{\}\)\.get\("([a-z_]+)"', fonte_regras))
    assert lidas, "o teste não achou nenhuma leitura de properties -- padrão mudou?"

    orfas = [chave for chave in lidas if '"%s"' % chave not in fonte_descoberta]
    assert not orfas, "regra lê propriedade que a descoberta nunca escreve: %s" % orfas


# --------------------------------------------------------------------------- o cartão


def test_uma_google_home_na_rede_gera_a_sugestao_de_saida():
    caixa = _caixa("dev-1", "cast_audio", "Banheiro Suíte", ["audio_out", discovery.CAST_MEDIA])
    cartoes = suggestions.rule_birdtunes_output(_contexto([caixa]))
    assert [c["id"] for c in cartoes] == ["birdtunes-output"]
    assert "Banheiro Suíte" in cartoes[0]["body"]


def test_uma_apple_tv_tambem_conta():
    tv = _caixa("dev-2", "apple_tv", "Quarto", ["audio_out", discovery.AIRPLAY_AUDIO])
    cartoes = suggestions.rule_birdtunes_output(_contexto([tv]))
    assert len(cartoes) == 1


def test_a_mesma_caixa_nao_conta_duas_vezes():
    """Um aparelho que fala os dois protocolos é um aparelho, não dois."""
    ambos = _caixa(
        "dev-3", "homepod", "Sala",
        ["audio_out", discovery.AIRPLAY_AUDIO, discovery.CAST_MEDIA],
    )
    cartoes = suggestions.rule_birdtunes_output(_contexto([ambos]))
    assert "1 saída(s)" in cartoes[0]["body"]


def test_com_a_saida_ja_escolhida_o_cartao_some():
    caixa = _caixa("dev-1", "cast_audio", "Cozinha", ["audio_out", discovery.CAST_MEDIA])
    ctx = _contexto([caixa], {"apps.settings.birdtunes.output.device_id": "dev-1"})
    assert suggestions.rule_birdtunes_output(ctx) == []


def test_as_outras_caixas_viram_o_cartao_de_varios_comodos():
    escolhida = _caixa("dev-1", "cast_audio", "Cozinha", ["audio_out", discovery.CAST_MEDIA])
    outra = _caixa("dev-2", "homepod", "Sala", ["audio_out", discovery.AIRPLAY_AUDIO])
    ctx = _contexto([escolhida, outra], {"apps.settings.birdtunes.output.device_id": "dev-1"})
    cartoes = suggestions.rule_extra_speakers(ctx)
    assert [c["id"] for c in cartoes] == ["birdtunes-multiroom"]
    assert "Sala" in cartoes[0]["body"] and "Cozinha" not in cartoes[0]["body"]


def test_sem_caixa_nenhuma_nao_ha_cartao():
    assert suggestions.rule_birdtunes_output(_contexto([])) == []


# --------------------------------------------------------------------------- pelo HTTP


def test_o_painel_recebe_o_cartao_com_uma_caixa_no_banco(auth_client):
    import json

    auth_client.app.state.db.execute(
        "INSERT INTO devices (id, kind, name, address, port, properties, capabilities,"
        " first_seen, last_seen, pinned, ignored) VALUES (?,?,?,?,?,?,?,?,?,0,0)",
        ("dev-cast", "cast_audio", "Banheiro Suíte", "10.0.0.60", 8009, "{}",
         json.dumps(["audio_out", discovery.CAST_MEDIA]),
         "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    corpo = auth_client.get("/api/suggestions", params={"refresh": "true"}).json()
    ids = [s["id"] for s in corpo["suggestions"]]
    assert "birdtunes-output" in ids, "com uma caixa na rede, o cartão tem que estar lá"
