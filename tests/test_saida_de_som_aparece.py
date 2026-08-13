"""A caixa que a descoberta acha tem que aparecer na lista de saídas.

A tela do BirdTunes filtra os aparelhos pelo ``device_kinds`` do backend
escolhido -- oferecer um Chromecast ao AirPlay produz um erro que ninguém
entende. Só que as duas listas foram escritas de cabeça:

* o Chromecast dizia falar com ``google_cast``, ``chromecast_audio``,
  ``google_home`` e ``nest_audio``. **Nenhum desses quatro existe** na
  descoberta. E os dois que existem de verdade -- ``cast_audio`` (Google Home,
  Nest Audio, Chromecast Audio) e ``cast_group`` (um grupo de caixas) -- não
  estavam lá. Uma Google Home, que é a caixa mais provável de tocar para os
  passarinhos, nunca aparecia como saída;
* o AirPlay listava ``airplay`` e ``raop``, que são tipos de serviço mDNS e não
  ``kind`` de aparelho nenhum.

Este teste compara as duas listas com o vocabulário da descoberta, para a
próxima caixa nova não sumir da tela em silêncio.
"""

from __future__ import annotations

import pytest

from project_os.core import discovery


def _kinds_de_audio(capacidade):
    return {
        kind for kind, caps in discovery.KIND_CAPABILITIES.items()
        if capacidade in caps
    }


def test_todo_kind_que_um_player_diz_tocar_existe_na_descoberta():
    from project_os.apps.birdtunes.players import airplay, chromecast

    for modulo in (airplay, chromecast):
        for kind in modulo.DEVICE_KINDS:
            assert kind in discovery.KIND_CAPABILITIES, (
                "%s diz tocar em %r, que a descoberta nunca produz"
                % (modulo.__name__, kind)
            )


def test_toda_caixa_de_cast_aparece_como_saida():
    from project_os.apps.birdtunes.players import chromecast

    faltando = _kinds_de_audio(discovery.CAST_MEDIA) - set(chromecast.DEVICE_KINDS)
    assert not faltando, "estas caixas de cast não aparecem na lista: %s" % faltando


def test_toda_caixa_airplay_aparece_como_saida():
    from project_os.apps.birdtunes.players import airplay

    faltando = _kinds_de_audio(discovery.AIRPLAY_AUDIO) - set(airplay.DEVICE_KINDS)
    assert not faltando, "estas caixas AirPlay não aparecem na lista: %s" % faltando


@pytest.mark.parametrize("kind", ["chromecast", "cast_audio", "cast_group"])
def test_a_caixa_chega_na_tela_pelo_http(auth_client, kind):
    """O caminho inteiro: linha na tabela de aparelhos -> lista de saídas."""
    auth_client.app.state.db.execute(
        "INSERT INTO devices (id, kind, name, address, port, properties, capabilities,"
        " first_seen, last_seen, pinned, ignored) VALUES (?,?,?,?,?,?,?,?,?,0,0)",
        ("dev-%s" % kind, kind, "Caixa da sala", "192.168.1.60", 8009, "{}",
         '["audio_out", "cast_media"]', "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    corpo = auth_client.get("/api/apps/birdtunes/outputs").json()
    backend = [b for b in corpo["backends"] if b["kind"] == "chromecast"][0]
    aparelhos = [d for d in corpo["devices"] if d["kind"] in backend["device_kinds"]]
    assert [d["id"] for d in aparelhos] == ["dev-%s" % kind]
