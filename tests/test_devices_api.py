"""A tela de um aparelho: ela existe e responde.

Todo aparelho da lista tem um link para a própria página, e essa página
respondia 500 -- a rota diz que devolve um mapa e devolvia o objeto Device, o
que o FastAPI recusa. O mesmo valia para fixar, ignorar e renomear, que passam
pelo PATCH. Nenhum teste tocava nessas duas rotas.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("home")


def _one_device(client):
    from project_os.core.discovery import Observation

    registry = client.app.state.devices
    observations = [Observation(
        source="mdns", service_type="_airplay._tcp.local.", instance="TV Quarto",
        kind="apple_tv", name_hint="TV Quarto", address="10.0.0.50",
        host="tv-quarto", port=7000, capabilities=["audio_out"],
    )]
    from project_os.core.discovery import merge_observations

    devices = merge_observations(observations)
    registry._persist(devices)
    return devices[0].id


def test_a_device_has_its_own_page(auth_client) -> None:
    device_id = _one_device(auth_client)

    response = auth_client.get("/api/devices/" + device_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == device_id
    assert body["name"] == "TV Quarto"
    assert "audio_out" in body["capabilities"]


def test_pinning_and_renaming_answer_with_the_device(auth_client) -> None:
    device_id = _one_device(auth_client)

    response = auth_client.patch(
        "/api/devices/" + device_id, json={"pinned": True, "name": "TV do bicho"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pinned"] is True
    assert body["custom_name"] == "TV do bicho"


def test_the_recipes_card_gets_recipes_not_an_error(auth_client) -> None:
    """"O que dá para fazer com este aparelho" lia o aparelho como mapa."""
    device_id = _one_device(auth_client)

    response = auth_client.get("/api/devices/" + device_id + "/recipes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device"]["id"] == device_id
    assert isinstance(body["recipes"], list)
    assert body["count"] == len(body["recipes"])
