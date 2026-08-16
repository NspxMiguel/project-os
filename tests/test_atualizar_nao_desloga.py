"""Atualizar mandava para a tela de login sem ter deslogado ninguém.

*"quando eu atualizei ele deslogo minha conta pq?"*

Não deslogou. A sessão é uma linha na tabela ``sessions``, com trinta dias de
validade, num banco que fica na partição de dados -- ela atravessa reinício,
troca de versão e troca de slot sem ser tocada. O que aconteceu foi a tela
concluir sozinha que ele não estava logado:

    try:
        user = await api.get('/auth/me')
    except err:
        authenticated = false      # <- qualquer erro, inclusive "não alcancei"

``api.js`` devolve ``ApiError`` com ``status: 0`` quando o pedido não chega a
lugar nenhum. E a hora exata em que isso acontece é depois de uma atualização: o
serviço reinicia, a página se recarrega logo em seguida (é o que faz a versão na
barra lateral parar de mentir), e a primeira pergunta pega a caixa ainda subindo.
Uma falha de transporte virava "faça login de novo" -- num formulário que, se a
caixa estivesse mesmo fora, também não teria com quem falar.

Agora só o servidor decide isso, e ele diz com 401 (ou 428, quando a caixa ainda
não tem dono). Sem resposta nenhuma, a tela insiste algumas vezes; se ainda
assim não alcançar, mostra "não estou conseguindo falar com a caixa", que é o
que de fato está acontecendo.

Medido no navegador, na mesma caixa e com o mesmo cookie: com o código antigo a
tela cai no login, com o novo ela entra -- e a linha da sessão continua no banco
nos dois casos.
"""

from __future__ import annotations

import io
import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(RAIZ, "web", "app.js")
API = os.path.join(RAIZ, "web", "lib", "api.js")


def _ler(caminho):
    return io.open(caminho, encoding="utf-8").read()


def _trecho_do_auth():
    """O pedaço do boot() que decide se alguém está logado."""
    fonte = _ler(APP)
    inicio = fonte.index("let user = null;")
    fim = fonte.index("store.set({user, authenticated});", inicio)
    return fonte[inicio:fim]


# --------------------------------------------------------------------------- o que decide


def test_uma_falha_de_rede_nao_desloga():
    trecho = _trecho_do_auth()
    assert "status === 401" in trecho and "status === 428" in trecho, (
        "só o servidor decide que alguém não está logado, e ele diz isso com um código"
    )
    assert re.search(r"if\s*\(\s*tentativa\s*===\s*AUTH_RETRIES\s*-\s*1\s*\)", trecho), (
        "sem insistir, a primeira falha durante o reinício continua caindo no login"
    )
    assert "await sleep(AUTH_RETRY_MS)" in trecho


def test_o_erro_do_servidor_continua_valendo():
    """500 não é 'não alcancei': insistir num servidor que respondeu errado é laço."""
    trecho = _trecho_do_auth()
    assert "if (status) {" in trecho
    assert trecho.index("if (status) {") < trecho.index("await sleep(AUTH_RETRY_MS)")


def test_quando_a_caixa_esta_fora_a_tela_diz_isso():
    """Formulário de login numa caixa inalcançável é pior que inútil."""
    assert "fatal(err instanceof ApiError" in _trecho_do_auth()


def test_as_tentativas_cobrem_um_reinicio_e_nao_um_dia():
    fonte = _ler(APP)
    tentativas = int(re.search(r"const AUTH_RETRIES = (\d+);", fonte).group(1))
    intervalo = int(re.search(r"const AUTH_RETRY_MS = (\d+);", fonte).group(1))
    assert 2 <= tentativas <= 6, tentativas
    assert 500 <= intervalo <= 3000, intervalo
    total = tentativas * intervalo
    assert 3000 <= total <= 15000, "a espera total é %dms" % total


# --------------------------------------------------------------------------- a premissa


def test_um_pedido_que_nao_chega_tem_status_zero():
    """O teste acima só faz sentido se 'não alcancei' for mesmo status 0."""
    fonte = _ler(API)
    trecho = fonte[fonte.index("Cannot reach project-os"):]
    assert "status: 0" in trecho[:400], (
        "se isto virar outro status, o boot passa a tratar falha de rede como resposta"
    )


def test_a_sessao_vive_no_banco_e_nao_na_memoria(auth_client):
    """Se a sessão morresse com o processo, nada disto adiantaria."""
    db = auth_client.app.state.db
    assert db.scalar("SELECT COUNT(*) FROM sessions", (), 0) >= 1


def test_a_sessao_dura_mais_que_uma_atualizacao(auth_client):
    """Trinta dias por padrão: uma troca de versão leva segundos."""
    from project_os import auth

    assert auth.DEFAULT_SESSION_TTL_HOURS >= 24
