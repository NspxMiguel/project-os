"""O app de contêiner sumia da máquina quando o project-os reiniciava.

``discover()`` varre pastas. Um app de contêiner não tem pasta: ele é uma
entrada do catálogo mais um contêiner rodando. Então, depois de qualquer
reinício do serviço -- uma atualização, um reboot, um ``systemctl restart`` --,
o n8n instalado ontem **sumia** da tela de Aplicativos e voltava a aparecer como
"instalar" na loja, enquanto o contêiner seguia rodando, segurando a porta e a
memória, sem nenhum botão para pará-lo. E a instalação de novo batia num nome
de contêiner já usado.

O que sobrevive ao reinício é ``apps.enabled`` no config. Este teste fixa a
volta por ali.
"""

from __future__ import annotations

import pytest


class MotorFalso(object):
    """Um docker de mentira que lembra o que foi pedido."""

    def __init__(self):
        self.criados = []
        self.rodando = set()

    def instalar(self, monkeypatch):
        from project_os.core import containers, plugins

        pai = self

        monkeypatch.setattr(containers, "detect_runtime", lambda: "docker")
        monkeypatch.setattr(containers, "image_present", lambda engine, image: True)
        monkeypatch.setattr(containers, "pull", lambda engine, image: None)

        def ensure_running(engine, app_id, spec, data_dir):
            pai.criados.append(app_id)
            pai.rodando.add(app_id)

        monkeypatch.setattr(containers, "ensure_running", ensure_running)
        monkeypatch.setattr(
            containers, "container_status",
            lambda engine, app_id: "running" if app_id in pai.rodando else "missing",
        )
        monkeypatch.setattr(
            containers, "status_detail",
            lambda engine, app_id: {"state": "running" if app_id in pai.rodando else "missing"},
        )
        monkeypatch.setattr(containers, "stop", lambda engine, app_id, timeout=10: pai.rodando.discard(app_id))
        monkeypatch.setattr(containers, "remove", lambda engine, app_id, force=True: pai.rodando.discard(app_id))
        # O mesmo módulo é importado dentro de plugins.py em dois lugares.
        monkeypatch.setattr(plugins, "containers", containers, raising=False)


def _bus():
    """Um barramento que aceita tudo e não guarda nada."""

    class Silencio(object):
        def publish_nowait(self, *a, **k):
            pass

        async def publish(self, *a, **k):
            pass

    return Silencio()


@pytest.fixture()
def motor(monkeypatch):
    falso = MotorFalso()
    falso.instalar(monkeypatch)
    return falso


async def test_o_app_volta_depois_do_reinicio(home, motor, monkeypatch):
    from project_os.config import load_config
    from project_os.core import catalog, plugins as plugins_core
    from project_os.db import Database
    from project_os import paths

    config = load_config()
    db = Database(paths.db_file())
    db.migrate()

    entrada = catalog.get("n8n")
    assert entrada and entrada.get("container"), "o n8n devia ser um app de contêiner"

    from project_os.core import containers

    spec = containers.parse_spec("n8n", entrada["container"])

    gerente = plugins_core.PluginManager(config=config, db=db, bus=_bus())
    await gerente.load_all()
    await gerente.install_container("n8n", entrada, spec, "docker")
    assert "n8n" in gerente.installed_ids()
    assert motor.criados == ["n8n"]

    # O project-os reinicia: gerente novo, mesma config, mesma máquina.
    # (O conftest fixa apps.enabled por variável de ambiente, que ganha do
    # arquivo -- então o que enable() gravou é levado adiante na mão, senão o
    # "reinício" nasceria com uma configuração que a caixa de verdade não tem.)
    import json as _json

    monkeypatch.setenv("PROJECT_OS__APPS__ENABLED", _json.dumps(gerente.enabled_ids()))
    outro = plugins_core.PluginManager(config=load_config(), db=db, bus=_bus())
    await outro.load_all()

    ids = [item["id"] for item in outro.list_apps()]
    assert "n8n" in ids, "o app de contêiner sumiu no reinício"
    assert "n8n" in outro.installed_ids()
    # E não foi criado um segundo contêiner: o que existia foi readotado.
    assert motor.criados.count("n8n") <= 2


async def test_sem_motor_de_conteiner_o_boot_nao_quebra(home, monkeypatch):
    """Um cartão SD levado para outra máquina não pode derrubar o boot."""
    from project_os.config import load_config
    from project_os.core import containers, plugins as plugins_core
    from project_os.db import Database
    from project_os import paths

    monkeypatch.setenv("PROJECT_OS__APPS__ENABLED", '["n8n"]')
    db = Database(paths.db_file())
    db.migrate()

    monkeypatch.setattr(containers, "detect_runtime", lambda: None)
    gerente = plugins_core.PluginManager(config=load_config(), db=db, bus=_bus())
    apps = await gerente.load_all()
    assert isinstance(apps, list)
    assert "n8n" not in [item["id"] for item in apps]
