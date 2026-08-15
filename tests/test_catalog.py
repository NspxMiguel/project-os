"""The catalog is data, so these tests guard the data.

A typo in :file:`project_os/data/catalog.yaml` cannot be caught by the type
checker or by importing anything -- it shows up as a store entry with a blank
icon, or an app the install endpoint cannot act on. That is what this file is
for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from project_os.core import catalog

WEB = Path(__file__).resolve().parent.parent / "web"


def icon_names():
    """Every icon name defined in web/lib/icons.js.

    Parsed from the source rather than duplicated here, so adding an icon to the
    frontend is enough -- there is no second list to keep in step.
    """
    source = (WEB / "lib" / "icons.js").read_text(encoding="utf-8")
    body = source.split("export const ICONS = {", 1)[1]
    return set(re.findall(r"^  '?([a-zA-Z0-9_-]+)'?:", body, re.M))


def test_o_readme_conta_a_loja_certa():
    """O número que o README promete tem que ser o número que a loja cumpre.

    "whatever the store carries" virou uma conta explícita porque a loja lista
    34 itens e instala 9: o resto diz no cartão por que não instala. Um README
    que promete 34 é a mesma fachada, só que na porta de entrada.
    """
    import os
    import re

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(raiz, "README.md"), encoding="utf-8") as arquivo:
        readme = arquivo.read()

    achado = re.search(r"store lists (\d+) items and installs (\d+)", readme)
    assert achado, "o README não conta mais a loja -- se mudou de texto, mude este teste junto"
    prometidos, instalaveis = int(achado.group(1)), int(achado.group(2))

    entradas = catalog.all_entries()
    de_verdade = [e for e in entradas if catalog.install_block(e) is None]
    assert prometidos == len(entradas), "README diz %d itens, a loja tem %d" % (
        prometidos, len(entradas),
    )
    assert instalaveis == len(de_verdade), "README diz que instala %d, instalam %d" % (
        instalaveis, len(de_verdade),
    )


def test_the_catalog_file_loads() -> None:
    entries = catalog.all_entries()
    assert entries, "the catalog file produced no entries at all"


def test_every_entry_has_what_the_store_renders() -> None:
    for entry in catalog.all_entries():
        for field in ("id", "name", "kind", "category", "summary", "description", "icon"):
            assert entry.get(field), "%s is missing %s" % (entry.get("id"), field)
        assert entry["kind"] in catalog.KINDS
        assert isinstance(entry["ram_mb"], int)
        assert isinstance(entry["disk_mb"], int)


def test_ids_are_unique() -> None:
    ids = [entry["id"] for entry in catalog.all_entries()]
    assert len(ids) == len(set(ids))


def test_no_emoji_anywhere_in_the_catalog() -> None:
    """> "evita emojis por favor" """
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")
    for entry in catalog.all_entries():
        blob = " ".join(str(value) for value in entry.values())
        found = emoji.findall(blob)
        assert not found, "%s contains %r" % (entry["id"], found)


def test_every_icon_exists_in_the_frontend_set() -> None:
    known = icon_names()
    for entry in catalog.all_entries():
        assert entry["icon"] in known, (
            "%s uses icon %r, which web/lib/icons.js does not define"
            % (entry["id"], entry["icon"])
        )


def test_entries_that_do_not_fit_are_still_listed() -> None:
    """A store that hides the big ones teaches you it is incomplete."""

    class TinyBoard(object):
        ram_total_mb = 400
        ram_mb = 512
        model = "test"
        tier = "minimal"

    listed = catalog.entries(TinyBoard())
    assert len(listed) == len(catalog.all_entries())
    too_big = [item for item in listed if not item["fits"]]
    assert too_big, "on a 400 MB board something must be marked as not fitting"
    for item in too_big:
        assert item["fit_reason"], "%s is refused without saying why" % item["id"]


@pytest.mark.parametrize("app_id", ["birdtunes", "jellyfin", "whatsapp-bot", "home-assistant"])
def test_the_apps_he_asked_for_are_in_the_store(app_id: str) -> None:
    assert catalog.get(app_id) is not None


def test_a_clean_install_has_no_apps_enabled() -> None:
    """> "quero q por padrao ele venha so com apps normais po, sem nada, q nem o ha" """
    from project_os import config

    assert config.DEFAULTS["apps"]["enabled"] == []


# --------------------------------------------------------------- não escritos
# O catálogo lista alguns builtins que ainda não existem como código (kasa,
# tuya, mqtt -- pedidos 17-19). A loja tem que dizer isso na carta, em vez de
# oferecer um botão cujo único desfecho possível é "No app called 'kasa'".
def test_a_builtin_that_is_not_written_yet_is_not_offered(auth_client) -> None:
    response = auth_client.get("/api/store")
    assert response.status_code == 200, response.text
    items = {item["id"]: item for item in response.json()["items"]}

    assert items["birdtunes"]["installable"] is True
    assert items["kasa"]["installable"] is False
    assert "ainda não foi feito" in items["kasa"]["install_reason"]


def test_installing_one_says_so_instead_of_failing_with_an_internal_error(auth_client) -> None:
    response = auth_client.post("/api/store/kasa/install", json={})
    assert response.status_code == 501, response.text
    body = response.json()
    assert body["error"] == "installer_pending"
    assert "ainda não foi feito" in body["message"]
    # A resposta antiga era "Nenhum app chamado 'kasa'", que soa como defeito da
    # caixa em vez de app que ainda não existe.
    assert "Nenhum app chamado" not in body["message"]
