"""Security tests.

project-os hands a browser a file manager and a command runner. These tests exist to make
sure the front door is actually locked: the auth gate, the filesystem sandbox, and the
opt-in switches that keep remote code execution off unless the user deliberately turns it
on.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


# ------------------------------------------------------------------------------ auth gate


def test_health_is_public_and_reports_setup_state(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["setup_required"] is True


def test_everything_else_is_locked_before_setup(client) -> None:
    for path in ("/api/devices", "/api/system/stats", "/api/settings", "/api/apps"):
        response = client.get(path)
        assert response.status_code == 428, "%s should demand setup, got %s" % (
            path,
            response.status_code,
        )
        assert response.json()["error"] == "setup_required"


def test_setup_creates_the_admin_and_cannot_be_replayed(client) -> None:
    creds = {"username": "miguel", "password": "correct horse battery staple"}
    assert client.post("/api/setup", json=creds).status_code in (200, 201)

    replay = client.post("/api/setup", json={"username": "intruder", "password": "hunter2hunter2"})
    assert replay.status_code in (403, 409), "setup must not be re-runnable once an admin exists"


def test_unauthenticated_requests_are_rejected_after_setup(client) -> None:
    creds = {"username": "miguel", "password": "correct horse battery staple"}
    client.post("/api/setup", json=creds)
    client.cookies.clear()

    response = client.get("/api/settings")
    assert response.status_code == 401


def test_login_rejects_a_wrong_password(client) -> None:
    creds = {"username": "miguel", "password": "correct horse battery staple"}
    client.post("/api/setup", json=creds)
    client.cookies.clear()

    bad = client.post("/api/auth/login", json={"username": "miguel", "password": "wrong"})
    assert bad.status_code == 401


def test_password_is_never_stored_in_plaintext(auth_client, home: Path) -> None:
    from project_os import paths
    from project_os.db import Database

    database = Database(paths.db_file())
    row = database.one("SELECT password_hash FROM users LIMIT 1")
    assert row is not None
    stored = row["password_hash"]
    assert "correct horse battery staple" not in stored
    assert stored.startswith("pbkdf2_sha256$")


def test_authenticated_session_can_read_settings(auth_client) -> None:
    response = auth_client.get("/api/settings")
    assert response.status_code == 200


def test_secrets_are_redacted_in_the_settings_response(auth_client) -> None:
    """The token has to be stored for its absence to mean anything.

    The first version sent the flat body, which stores nothing at all -- so the
    secret was never there to leak and the test passed without exercising
    redaction once.
    """
    gravado = auth_client.put(
        "/api/settings", json={"values": {"integrations.home_assistant.token": "s3cr3t-token"}}
    )
    assert gravado.json()["changed"] == ["integrations.home_assistant.token"], gravado.text

    body = auth_client.get("/api/settings").json()
    assert "s3cr3t-token" not in str(body)
    assert "home_assistant" in str(body), "a chave sumiu inteira; isto não prova redação"


def test_um_corpo_sem_values_e_recusado_em_vez_de_ignorado(auth_client) -> None:
    """O silêncio que escondeu o buraco do terminal por meses.

    ``{"security.allow_shell": true}`` respondia 200 com ``changed: []``: campo
    desconhecido ignorado, nada mudado, ninguém avisado. Agora é 422 com o nome
    do campo -- para mim, para a tela, e para qualquer script dele.
    """
    resposta = auth_client.put("/api/settings", json={"security.allow_shell": True})
    assert resposta.status_code == 422, resposta.text
    assert "allow_shell" in resposta.text, resposta.text

    correto = auth_client.put(
        "/api/settings", json={"values": {"security.allow_shell": True}}
    )
    assert correto.status_code == 200
    assert correto.json()["changed"] == ["security.allow_shell"]


def test_the_layers_half_of_the_response_is_redacted_too(auth_client) -> None:
    """A mesma resposta traz "settings" e "layers". As duas passam pela máscara.

    O "layers" existe para explicar de onde cada valor veio (padrão, arquivo,
    variável de ambiente) e saía cru: o token do Home Assistant em texto limpo
    duas chaves abaixo do ``********``. Redação que outra metade da resposta
    desfaz não é redação.
    """
    auth_client.put(
        "/api/settings", json={"values": {"integrations.home_assistant.token": "s3cr3t-token"}}
    )
    camadas = auth_client.get("/api/settings").json()["layers"]
    assert "s3cr3t-token" not in str(camadas)
    assert "********" in str(camadas), "nem mascarou nem guardou -- o valor sumiu do layers"


# ----------------------------------------------------------------------- filesystem sandbox

TRAVERSAL_ATTEMPTS = [
    "../../../../etc/passwd",
    "..",
    "../",
    "./../../etc/passwd",
    "/etc/passwd",
    "/etc/shadow",
    "subdir/../../../../etc/passwd",
    "....//....//etc/passwd",
    "%2e%2e/%2e%2e/etc/passwd",
    "..\\..\\windows\\system32",
]


@pytest.mark.parametrize("attempt", TRAVERSAL_ATTEMPTS)
def test_path_traversal_is_rejected(home: Path, attempt: str) -> None:
    from project_os.core import files
    from project_os.errors import ApiError

    with pytest.raises(ApiError) as excinfo:
        files.resolve(attempt)
    assert excinfo.value.status in (400, 403)


def test_a_symlink_pointing_outside_the_sandbox_is_rejected(home: Path, tmp_path: Path) -> None:
    """Resolving must check the *target*, not the link path."""
    from project_os.core import files
    from project_os.errors import ApiError

    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("not yours")

    link = home / "escape"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("symlinks unavailable")

    with pytest.raises(ApiError):
        files.resolve("escape/secret.txt")


def test_a_nul_byte_in_the_path_is_rejected(home: Path) -> None:
    from project_os.core import files
    from project_os.errors import ApiError

    with pytest.raises(ApiError):
        files.resolve("notes\x00.txt")


def test_paths_inside_the_sandbox_resolve_fine(home: Path) -> None:
    from project_os.core import files

    (home / "notes.txt").write_text("hello")
    resolved = files.resolve("notes.txt")
    assert Path(resolved).read_text() == "hello"


def test_the_files_api_refuses_traversal_over_http(auth_client) -> None:
    response = auth_client.get("/api/files/list", params={"path": "../../../../etc"})
    assert response.status_code in (400, 403)


# -------------------------------------------------------------------- opt-in danger switches


def test_shell_is_disabled_by_default(auth_client) -> None:
    response = auth_client.post("/api/shell/exec", json={"command": "echo hello", "cwd": "."})
    assert response.status_code == 403
    body = response.json()
    assert "allow_shell" in str(body), "the error must name the setting to flip"


def test_service_control_is_disabled_by_default(auth_client) -> None:
    response = auth_client.post("/api/system/services/ssh/restart")
    assert response.status_code == 403
    assert "allow_service_control" in str(response.json())


def ligar(auth_client, **valores) -> None:
    """Change settings the way the settings screen does: {"values": {...}}.

    Spelled out because the first version of the test below sent the flat dict.
    A flat body is not an error -- ``values`` simply defaults to empty, the PUT
    answers 200 with ``changed: []``, and nothing is turned on. The test then
    asserted 403 against a shell that had never been enabled, and passed for
    three months while the hole it was written to guard was wide open.
    """
    response = auth_client.put("/api/settings", json={"values": valores})
    assert response.status_code == 200, response.text
    assert sorted(response.json()["changed"]) == sorted(valores), response.text


def test_shell_stays_disabled_when_auth_is_off(auth_client) -> None:
    """An unauthenticated RCE endpoint must be impossible to configure by accident.

    Two switches, each defensible alone: ``allow_shell`` because the terminal is
    the point of Advanced mode, ``auth_enabled: false`` because "it is my house,
    I do not want a login screen". Together they used to mean: anything on the
    network -- including the light bulbs this box exists to talk to -- can run
    commands as the service user, which has passwordless apt, which is root.
    """
    ligar(auth_client, **{"security.allow_shell": True})
    autenticado = auth_client.post("/api/shell/exec", json={"command": "echo oi", "cwd": "."})
    assert autenticado.status_code == 200, "o terminal ligado tem que funcionar para o dono"

    ligar(auth_client, **{"security.auth_enabled": False})
    auth_client.cookies.clear()
    response = auth_client.post("/api/shell/exec", json={"command": "id", "cwd": "."})
    assert response.status_code == 403, response.text
    assert "auth_enabled" in str(response.json()), "the error must name the setting to flip"


def test_the_terminal_websocket_also_refuses_when_auth_is_off(auth_client) -> None:
    """The same hole, one door over. The PTY is the more dangerous of the two.

    Asserted on the first frame rather than on "some exception happened": the
    first version of this test expected any exception, and the socket happily
    answered ``{"type": "ready", "shell": "/bin/zsh", "pid": ...}`` before an
    unrelated CancelledError at teardown made the test pass.
    """
    ligar(auth_client, **{"security.allow_shell": True, "security.auth_enabled": False})
    auth_client.cookies.clear()

    # O quadro fica guardado fora do try de propósito: sair do "with" levanta
    # CancelledError quando o servidor fecha, e zerar a variável no except
    # apagaria justamente a prova de que o terminal tinha aberto.
    quadro = None
    try:
        with auth_client.websocket_connect("/api/shell/ws") as ws:
            quadro = ws.receive_json()
    except Exception:
        pass  # recusado no aperto de mão, ou fechado antes de falar

    assert quadro is None or quadro.get("type") != "ready", (
        "o terminal abriu um pty sem sessão: %s" % str(quadro)[:200]
    )


def test_websocket_requires_authentication(client) -> None:
    """No frame, not merely "some exception".

    ``pytest.raises((WebSocketDisconnect, Exception))`` also passes when the
    socket answers happily and something unrelated blows up on the way out --
    which is exactly how the terminal socket kept a hole hidden for months.
    """
    client.post("/api/setup", json={"username": "miguel", "password": "correct horse battery"})
    client.cookies.clear()

    quadro = None
    try:
        with client.websocket_connect("/api/ws") as ws:
            quadro = ws.receive_json()
    except Exception:
        pass

    assert quadro is None, "o websocket falou com quem não tem sessão: %s" % str(quadro)[:200]


def test_frontend_assets_are_revalidated(client) -> None:
    """Unversioned asset names plus heuristic caching = old UI against a new API.

    ``no-cache`` still lets the browser store the file; it just has to ask
    first, which the ETag answers with a 304 when nothing changed.
    """
    for path in ("/", "/style.css", "/app.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == "no-cache", path


def test_app_panel_assets_are_revalidated(auth_client) -> None:
    """An app's own panel.js is served by a different route -- same rule."""
    response = auth_client.get("/api/apps/birdtunes/ui/panel.js")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-cache"


def test_close_waits_for_statements_running_in_other_threads(tmp_path) -> None:
    """Closing under a busy worker thread used to segfault the whole process.

    Connections are shared across threads (check_same_thread=False), and a
    BirdTunes import writes progress from an executor thread. Closing its
    connection mid-statement is a use-after-free in the sqlite3 C module.
    """
    import threading

    from project_os.db import Database

    database = Database(tmp_path / "race.db")
    database.migrate()
    database.execute("CREATE TABLE IF NOT EXISTS race (n INTEGER)")

    started = threading.Event()
    stop = threading.Event()
    failures = []

    def writer() -> None:
        started.set()
        while not stop.is_set():
            try:
                database.execute("INSERT INTO race (n) VALUES (1)")
            except Exception as exc:  # a closed database may raise -- never crash
                failures.append(exc)
                return

    thread = threading.Thread(target=writer)
    thread.start()
    started.wait(timeout=2)
    stop.set()
    database.close()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert database._inflight == 0
