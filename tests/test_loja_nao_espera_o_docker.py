"""Abrir a Loja custava dez segundos parados esperando o Docker.

Medido no navegador, com o Docker Desktop subindo: ``GET /api/store`` levou
**10,3 s**. O motivo é honesto e foi uma correção anterior -- o cartão precisa
dizer, *antes* do clique, se o motor de contêiner responde, e "responder" só se
sabe perguntando (``docker info``). O que não estava certo era o preço:

* a pergunta saía a cada carregamento de página, e com o daemon morto ou
  subindo ela custa o timeout inteiro (8 s) antes de desistir;
* e era o timeout do *instalar* -- oito segundos fazem sentido para não abortar
  uma instalação, e nenhum para desenhar um cartão.

Agora a resposta fica guardada por alguns segundos e a tela usa um timeout
curto. O "não" vale menos tempo que o "sim": quem acabou de ligar o Docker não
pode esperar meio minuto para a Loja perceber.
"""

from __future__ import annotations

import pytest


def _containers():
    """O módulo que a aplicação de teste está usando *agora*.

    A fixture ``home`` esvazia ``sys.modules`` para o config reler o ambiente,
    então um ``import`` no topo deste arquivo guardaria a cópia *anterior* do
    módulo: trocar o ``_run`` dela não muda nada para quem responde ao HTTP, e
    o cache que este arquivo limpa não é o cache que a Loja consulta. Custou
    uma hora de teste verde mentindo.
    """
    from project_os.core import containers

    return containers


def _motor(monkeypatch, chamadas, retorno=0, demora=0.0):
    import subprocess

    containers = _containers()
    monkeypatch.setattr(
        containers.shutil, "which",
        lambda nome: "/usr/bin/" + nome if nome == "docker" else None,
    )

    def rodar(argv, timeout=None):
        chamadas.append({"argv": list(argv), "timeout": timeout})
        if demora and timeout is not None and demora > timeout:
            # O _run de verdade traduz o TimeoutExpired em ContainerError; o de
            # mentira precisa fazer o mesmo, senão o teste passa a exercitar um
            # caminho que não existe.
            raise containers.ContainerError(
                "%s took longer than %ds" % (" ".join(argv[:2]), int(timeout))
            )
        return subprocess.CompletedProcess(argv, retorno, b"", b"")

    monkeypatch.setattr(containers, "_run", rodar)
    # Trocar o motor por um de mentira invalida o que estava guardado: subir a
    # aplicação de teste já pergunta uma vez, com o docker de verdade.
    containers.forget_runtime_status()
    return containers


def test_a_segunda_pergunta_nao_sai_de_casa(monkeypatch):
    chamadas = []
    containers = _motor(monkeypatch, chamadas)
    primeira = containers.runtime_status()
    segunda = containers.runtime_status()
    assert primeira == segunda
    assert len(chamadas) == 1, "perguntou de novo dentro do prazo"


def test_force_pergunta_de_novo(monkeypatch):
    """O botão "verificar de novo" não pode receber a resposta velha."""
    chamadas = []
    containers = _motor(monkeypatch, chamadas)
    containers.runtime_status()
    containers.runtime_status(force=True)
    assert len(chamadas) == 2


def test_o_nao_vale_menos_tempo_que_o_sim():
    containers = _containers()
    assert containers.STATUS_TTL_ERRO < containers.STATUS_TTL_OK


def test_esquecer_faz_perguntar_de_novo(monkeypatch):
    chamadas = []
    containers = _motor(monkeypatch, chamadas)
    containers.runtime_status()
    containers.forget_runtime_status()
    containers.runtime_status()
    assert len(chamadas) == 2


def test_a_tela_usa_o_timeout_curto(monkeypatch):
    chamadas = []
    containers = _motor(monkeypatch, chamadas)
    containers.runtime_status(timeout=containers.PING_TIMEOUT_TELA)
    assert chamadas[0]["timeout"] == containers.PING_TIMEOUT_TELA
    assert containers.PING_TIMEOUT_TELA < containers.PING_TIMEOUT


def test_o_motor_que_nao_responde_vira_recado_e_nao_espera_de_novo(monkeypatch):
    """Timeout não pode virar exceção na tela -- vira "não respondeu"."""
    chamadas = []
    containers = _motor(monkeypatch, chamadas, demora=30.0)
    estado = containers.runtime_status(timeout=containers.PING_TIMEOUT_TELA)
    assert estado["available"] is False
    assert estado["engine"] == "docker", "o motor continua nomeado: ele está instalado"
    assert "3" in estado["reason"] or "longer" in estado["reason"]
    # E a página seguinte, dentro do prazo curto, não paga o timeout de novo.
    containers.runtime_status(timeout=containers.PING_TIMEOUT_TELA)
    assert len(chamadas) == 1


def test_a_loja_inteira_responde_rapido_com_o_motor_morto(auth_client, monkeypatch):
    """O caminho de verdade: uma página da Loja, um `docker info` no máximo."""
    chamadas = []
    _motor(monkeypatch, chamadas, demora=30.0)

    primeira = auth_client.get("/api/store")
    assert primeira.status_code == 200
    segunda = auth_client.get("/api/store")
    assert segunda.status_code == 200

    assert len(chamadas) == 1, "duas páginas, %d perguntas ao docker" % len(chamadas)
    conteineres = [i for i in segunda.json()["items"] if i["kind"] == "container"]
    assert conteineres and conteineres[0]["container_runtime"]["available"] is False
