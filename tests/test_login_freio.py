"""Conferir senha custa caro, e ninguém precisa acertar para atrapalhar.

Uma senha aqui passa por 200 mil iterações de pbkdf2-sha256. Isso é de propósito
-- é o que torna força bruta inviável -- mas num Pi 3B são centenas de
milissegundos de CPU por tentativa. O laço de eventos não trava, porque a
verificação roda num executor; só que o executor tem vários trabalhadores. Oito
tentativas simultâneas de senha errada põem os quatro núcleos a 100% moendo
pbkdf2, e a caixa inteira fica lenta -- num endpoint sem autenticação nenhuma.

Duas travas, e as duas precisam de teste porque as duas podem trancar o dono
fora de casa se eu errar a mão:

* uma senha conferida por vez;
* cada erro seguido daquele endereço deixa a próxima tentativa mais lenta, com
  teto e sem bloqueio permanente.

O que **não** pode acontecer é ele errar a senha três vezes, acertar na quarta e
não entrar. Isso tem teste aqui.
"""

from __future__ import annotations

import time


def test_a_senha_certa_continua_entrando_depois_de_erros(auth_client):
    """O caso que mais importa: errar não pode trancar a porta."""
    from project_os.api import auth as api_auth

    api_auth.ESPERA_POR_ERRO = 0.01  # o teste mede comportamento, não paciência
    api_auth.ESPERA_MAXIMA = 0.05

    for _ in range(3):
        errada = auth_client.post(
            "/api/auth/login", json={"username": "miguel", "password": "nao-e-essa"}
        )
        assert errada.status_code == 401

    certa = auth_client.post(
        "/api/auth/login", json={"username": "miguel", "password": "correct horse battery staple"}
    )
    assert certa.status_code == 200, certa.text


def test_errar_muitas_vezes_fica_mais_devagar(auth_client):
    from project_os.api import auth as api_auth

    api_auth.ESPERA_POR_ERRO = 0.15
    api_auth.ESPERA_MAXIMA = 1.0
    api_auth._erros.clear()

    inicio = time.monotonic()
    auth_client.post("/api/auth/login", json={"username": "miguel", "password": "x"})
    primeira = time.monotonic() - inicio

    for _ in range(4):
        auth_client.post("/api/auth/login", json={"username": "miguel", "password": "x"})

    inicio = time.monotonic()
    auth_client.post("/api/auth/login", json={"username": "miguel", "password": "x"})
    depois = time.monotonic() - inicio

    assert depois > primeira, "errar muitas vezes não ficou mais devagar"


def test_o_castigo_tem_teto(auth_client):
    """Sem teto, um script deixaria a tela de login inútil para o dono."""
    from project_os.api import auth as api_auth

    api_auth.ESPERA_POR_ERRO = 0.05
    api_auth.ESPERA_MAXIMA = 0.2
    api_auth._erros.clear()

    for _ in range(20):
        auth_client.post("/api/auth/login", json={"username": "miguel", "password": "x"})

    inicio = time.monotonic()
    auth_client.post("/api/auth/login", json={"username": "miguel", "password": "x"})
    gasto = time.monotonic() - inicio

    assert gasto < api_auth.ESPERA_MAXIMA + 1.5, "a espera passou do teto (%.2fs)" % gasto


def test_acertar_esquece_os_erros(auth_client):
    from project_os.api import auth as api_auth

    api_auth.ESPERA_POR_ERRO = 0.01
    api_auth._erros.clear()

    auth_client.post("/api/auth/login", json={"username": "miguel", "password": "x"})
    assert api_auth._erros, "não contou o erro"

    auth_client.post(
        "/api/auth/login", json={"username": "miguel", "password": "correct horse battery staple"}
    )
    assert not api_auth._erros, "acertar a senha não limpou o contador"


def test_a_lista_de_enderecos_nao_cresce_sem_fim():
    """Um endereço de origem forjado não pode virar vazamento de memória."""
    from project_os.api import auth as api_auth

    api_auth._erros.clear()
    agora = time.monotonic()
    for n in range(api_auth.MAX_ENDERECOS + 50):
        api_auth._erros["10.0.0.%d" % n] = (1, agora)

    api_auth._limpar_erros(agora)
    assert len(api_auth._erros) <= api_auth.MAX_ENDERECOS


def test_erros_antigos_sao_esquecidos():
    from project_os.api import auth as api_auth

    api_auth._erros.clear()
    api_auth._erros["10.0.0.9"] = (5, time.monotonic() - api_auth.JANELA_DE_ERROS - 10)
    api_auth._limpar_erros(time.monotonic())
    assert "10.0.0.9" not in api_auth._erros


def test_so_uma_senha_e_conferida_por_vez():
    import asyncio

    from project_os.api import auth as api_auth

    async def olhar():
        porta = api_auth._porta_de_senha()
        # E a segunda chamada devolve o mesmo, não um novo a cada login.
        assert api_auth._porta_de_senha() is porta
        return porta._value

    assert asyncio.run(olhar()) == 1, (
        "o semáforo deixaria várias verificações de pbkdf2 rodarem juntas, "
        "que é o que põe os quatro núcleos do Pi a 100%"
    )


def test_o_semaforo_nao_nasce_no_import():
    """No Python 3.9 um Semaphore criado no import quebra o módulo inteiro.

    E o sintoma não é um erro claro: o router não sobe e a caixa responde 405 em
    tudo. Este teste existe para essa linha nunca voltar.
    """
    from pathlib import Path

    fonte = Path("project_os/api/auth.py").read_text(encoding="utf-8")
    for linha in fonte.splitlines():
        if linha.startswith(" ") or linha.lstrip().startswith("#"):
            continue
        assert "asyncio.Semaphore(" not in linha, (
            "semáforo criado no nível do módulo: %s" % linha.strip()
        )
