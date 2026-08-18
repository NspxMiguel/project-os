"""A tela de atualizações esperava um clique para saber se havia atualização.

*"como checo atualizacao? como atualizo? msm versao a anos"* -- e, com a 0.4.20
já publicada, um print da tela dizendo "Esta máquina está na 0.4.14" e nada mais.

O título é um fato (a versão instalada), mas na posição em que está lê-se como
veredito: quem abre "Atualizações" está fazendo a pergunta. E a tela não
perguntava nada -- ``load()`` só copiava ``last_check`` do servidor, que é uma
variável de módulo: vale para a vida do processo, some no reinício, e nasce
vazia. Numa caixa recém-ligada o resultado era uma tela com um número de versão,
um botão, e nenhuma notícia -- igual estivesse a versão nova publicada há uma
hora ou há um ano.

Agora abrir a tela é a pergunta: se não há conferida guardada, ou ela é velha, a
tela confere sozinha. O que ela não faz é falar por cima de quem só passou por
ali -- "está na versão mais nova" continua sendo resposta a um clique. Se a
conferida automática falhar, isso aparece no cartão, porque falhar em silêncio
devolveria a tela ao que ela era.
"""

from __future__ import annotations

import io
import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEW = os.path.join(RAIZ, "web", "views", "updates.js")
API = os.path.join(RAIZ, "project_os", "api", "updates.py")
CORE = os.path.join(RAIZ, "project_os", "core", "updates.py")


def _ler(caminho):
    return io.open(caminho, encoding="utf-8").read()


def _load():
    fonte = _ler(VIEW)
    inicio = fonte.index("async function load()")
    return fonte[inicio:fonte.index("\n    //: ", inicio)]


# --------------------------------------------------------------------------- abrir é perguntar


def test_abrir_a_tela_confere():
    assert "void check(true)" in _load(), (
        "sem isto a tela continua mostrando só o que alguém conferiu antes"
    )


def test_a_conferida_automatica_nao_fala_por_cima():
    """Um aviso a cada visita à tela é ruído; o clique é que pede resposta."""
    fonte = _ler(VIEW)
    trecho = fonte[fonte.index("async function check(automatico)"):]
    trecho = trecho[:trecho.index("\n    async function install()")]
    assert "!automatico && !state.check.update_available" in trecho
    assert "if (!automatico) {" in trecho, "o erro do clique continua sendo um aviso"


def test_uma_conferida_recente_nao_e_refeita():
    """Trocar de tela e voltar não pode virar um pedido à rede por visita."""
    fonte = _ler(VIEW)
    assert "_velho(state.check)" in fonte
    escrito = re.search(r"const CHECK_FRESH_MS = ([\d\s*]+);", fonte).group(1)
    janela = eval(escrito, {"__builtins__": {}}, {})  # só dígitos e '*'
    assert 60 * 60 * 1000 <= janela <= 24 * 60 * 60 * 1000, janela


def test_o_carimbo_de_hora_e_lido_no_formato_que_o_servidor_manda():
    """``Date.parse`` de um float devolve NaN -- e NaN conferiria toda vez."""
    fonte = _ler(VIEW)
    trecho = fonte[fonte.index("function _velho("):]
    trecho = trecho[:trecho.index("\n    }")]
    assert "typeof bruto === 'number' ? bruto * 1000" in trecho
    assert "checked_at" in _ler(CORE), "se o servidor parar de mandar isto, a tela confere sempre"


def test_a_conferida_automatica_que_falha_aparece():
    fonte = _ler(VIEW)
    assert "state.checkError" in fonte
    # notice--warn, e não notice--warning: o tema define a primeira e a segunda
    # não existe em lugar nenhum. Este teste guardava o nome errado, então o
    # aviso saía sem cor e a suíte dizia que estava tudo certo.
    assert "notice notice--warn'}, t('updates.error.check')" in fonte


# --------------------------------------------------------------------------- a premissa


def test_o_resultado_guardado_morre_com_o_processo():
    """O motivo de a tela nascer sem notícia: é memória do processo, não banco.

    Se um dia isto virar linha de banco, a conferida automática passa a ser
    guiada só pela idade do carimbo -- que é o que ``_velho`` já faz.
    """
    fonte = _ler(API)
    assert re.search(r"^_last_check = None", fonte, re.M), (
        "last_check deixou de ser variável de módulo; reveja o que a tela assume"
    )


def test_o_servidor_carimba_a_hora_da_conferida(auth_client):
    corpo = auth_client.post("/api/updates/check", json={}).json()
    assert isinstance(corpo.get("checked_at"), float), corpo
    assert corpo["checked_at"] > 1_700_000_000, "isso não é um epoch em segundos"


def test_a_tela_le_o_que_o_get_devolve(auth_client):
    auth_client.post("/api/updates/check", json={})
    corpo = auth_client.get("/api/updates").json()
    assert corpo.get("last_check"), "a tela lê daqui ao abrir"
    assert "checked_at" in corpo["last_check"]
