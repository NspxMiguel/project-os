"""A caixa sumiu da rede sem ter saído do ar.

*"n foi, n tocou"* — e o Pi estava ligado o tempo todo.

Medido em 21/08, com a caixa três dias sem ser tocada:

* `project-os.local` não resolvia mais para IPv4, `10.0.0.104` não respondia
  ping, e varrer a rede inteira não achou nenhum MAC de Raspberry;
* o mesmo nome resolvia para **IPv6**, e `ping6` respondia em 8 ms;
* a **porta 22 estava aberta** por IPv6, com banner `OpenSSH_9.2p1 -2+deb12u10`;
* e a **porta 80 dava `connection refused`** — não timeout, não filtro.

"Connection refused" com SSH funcionando se lê como "o serviço morreu". Não era:
a unidade sobe com ``--host 0.0.0.0``, que abre um soquete **AF_INET apenas**.
Uma conexão IPv6 para uma porta que só existe em IPv4 é recusada exatamente
assim. O sshd escuta nas duas famílias, então ele aparecia; o project-os, não.

O que tinha caído era o IPv4 da caixa na rede local — e com ele o Chromecast,
que é IPv4, e portanto a música. Só que do lado de fora a caixa parecia morta,
e ninguém tinha como saber a diferença.

Um soquete ``AF_INET6`` com ``IPV6_V6ONLY`` desligado atende as duas famílias no
mesmo descritor. O Linux já vem assim (``net.ipv6.bindv6only=0``), mas isso é um
sysctl que qualquer um muda; aqui a opção é desligada na mão, e se não der,
volta-se ao IPv4 de sempre em vez de subir atendendo metade da rede.
"""

from __future__ import annotations

import socket

import pytest

from project_os.__main__ import soquete_de_pilha_dupla


@pytest.fixture
def porta_livre():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    porta = s.getsockname()[1]
    s.close()
    return porta


def _fecha(sock):
    if sock is not None:
        sock.close()


# --------------------------------------------------------- o defeito medido


def test_zero_zero_zero_zero_sozinho_recusa_ipv6(porta_livre):
    """O fato por trás do diagnóstico errado, preso aqui.

    Sem isto, "connection refused na 80 e SSH de pé" continuaria parecendo
    prova de serviço morto.
    """
    servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    servidor.bind(("0.0.0.0", porta_livre))
    servidor.listen(8)
    try:
        cliente = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        cliente.settimeout(3)
        with pytest.raises(OSError) as erro:
            cliente.connect(("::1", porta_livre))
        cliente.close()
        assert erro.value.errno in (
            socket.errno.ECONNREFUSED, socket.errno.EADDRNOTAVAIL,
        ), erro.value
    finally:
        servidor.close()


# --------------------------------------------------------------- o conserto


def test_o_soquete_atende_as_duas_familias(porta_livre):
    sock = soquete_de_pilha_dupla("0.0.0.0", porta_livre)
    if sock is None:
        pytest.skip("esta máquina não deixou desligar o IPV6_V6ONLY")
    try:
        assert sock.family == socket.AF_INET6
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0

        for familia, endereco in ((socket.AF_INET, "127.0.0.1"),
                                  (socket.AF_INET6, "::1")):
            cliente = socket.socket(familia, socket.SOCK_STREAM)
            cliente.settimeout(3)
            cliente.connect((endereco, porta_livre))
            cliente.close()
    finally:
        _fecha(sock)


def test_dois_pontos_dois_pontos_tambem_conta_como_em_tudo(porta_livre):
    sock = soquete_de_pilha_dupla("::", porta_livre)
    if sock is None:
        pytest.skip("esta máquina não deixou desligar o IPV6_V6ONLY")
    try:
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
    finally:
        _fecha(sock)


def test_endereco_especifico_continua_sendo_respeitado(porta_livre):
    """Escolher um endereço é uma decisão; não é papel deste código desfazer."""
    assert soquete_de_pilha_dupla("127.0.0.1", porta_livre) is None
    assert soquete_de_pilha_dupla("10.0.0.104", porta_livre) is None


def test_sem_ipv6_na_maquina_volta_para_o_caminho_antigo(porta_livre, monkeypatch):
    """Melhor o IPv4 de sempre do que não subir."""
    monkeypatch.setattr(socket, "has_ipv6", False)
    assert soquete_de_pilha_dupla("0.0.0.0", porta_livre) is None


def test_porta_ocupada_nao_derruba_a_inicializacao(porta_livre):
    """Se o bind falhar, quem chama segue pelo caminho de antes em vez de
    estourar no boot -- um erro aqui deixaria a caixa sem subir nada."""
    ocupando = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    ocupando.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    try:
        ocupando.bind(("::", porta_livre))
        ocupando.listen(1)
    except OSError:
        pytest.skip("não deu para ocupar a porta nesta máquina")
    try:
        assert soquete_de_pilha_dupla("0.0.0.0", porta_livre) is None
    finally:
        ocupando.close()


def test_o_lancador_usa_o_soquete_quando_ele_existe():
    """Sem isto o soquete seria criado e ignorado."""
    import os
    fonte = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "project_os", "__main__.py"), encoding="utf-8").read()
    assert "servidor.run(sockets=[escuta])" in fonte
    assert 'options.pop("host", None)' in fonte, (
        "o Config não pode receber host e soquete ao mesmo tempo"
    )
