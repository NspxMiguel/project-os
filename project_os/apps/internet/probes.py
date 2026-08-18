# -*- coding: utf-8 -*-
"""Três perguntas separadas, porque "a internet caiu" quase nunca é uma coisa só.

Quando nada carrega, a causa está num de três lugares, e o conserto de cada um é
diferente:

1. **o roteador** -- o wifi caiu, o cabo saiu, o roteador travou. Quem conserta
   isso anda até o aparelho;
2. **a internet** -- o roteador está de pé e o provedor não entrega. Quem
   conserta isso liga para o provedor, e é bom ter a hora exata na mão;
3. **o DNS** -- endereço numérico responde e nome não resolve. Para a pessoa
   isso é idêntico a "caiu", e é o único dos três que costuma dar para
   consertar sozinho.

Cada medida aqui é uma conexão TCP com prazo, da biblioteca padrão. Sem ping:
ICMP precisa de root, e uma caixa que roda como usuário comum não teria como.
Sem biblioteca nova: um app que pede dependência é um app que às vezes não
instala.

Nada aqui escreve em banco nem toca em asyncio de propósito -- assim dá para
medir a lógica inteira sem rede, com relógio e socket de mentira.
"""

from __future__ import annotations

import socket
import struct
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

#: Endereços numéricos, de propósito: um nome aqui misturaria a pergunta "a
#: internet responde?" com a pergunta "o DNS responde?", que é o defeito que
#: este app existe para separar. Porta 53 porque servidor de DNS público aceita
#: TCP na 53 e é o que menos se parece com sondagem de porta.
ALVOS_INTERNET = ("1.1.1.1:53", "9.9.9.9:53", "8.8.8.8:53")

#: Um nome que existe há muito tempo e que ninguém precisa configurar.
NOME_PARA_RESOLVER = "cloudflare.com"

#: Estados, do melhor para o pior. A ordem importa: é ela que decide qual causa
#: contar quando mais de uma coisa está quebrada ao mesmo tempo -- sem roteador,
#: dizer "o DNS falhou" seria verdade e seria inútil.
NO_AR = "ok"
SEM_DNS = "dns"
SEM_INTERNET = "internet"
SEM_ROTEADOR = "roteador"

ORDEM_DA_CAUSA = (SEM_ROTEADOR, SEM_INTERNET, SEM_DNS, NO_AR)

PRAZO_PADRAO = 3.0


def _agora() -> float:
    return time.time()


def tcp_ms(host: str, porta: int, prazo: float = PRAZO_PADRAO,
           abrir: Any = None, relogio: Any = None) -> Optional[float]:
    """Milissegundos até a conexão abrir, ou ``None`` se não abriu.

    ``abrir`` e ``relogio`` existem para o teste: a lógica inteira deste módulo
    é medível sem encostar na rede.
    """
    abrir = abrir or socket.create_connection
    relogio = relogio or _agora
    comeco = relogio()
    try:
        conexao = abrir((host, porta), prazo)
    except (OSError, socket.timeout):
        return None
    try:
        return round((relogio() - comeco) * 1000.0, 1)
    finally:
        try:
            conexao.close()
        except Exception:  # pragma: no cover - fechar não pode derrubar a medida
            pass


def _par(alvo: str) -> Tuple[str, int]:
    host, _, porta = str(alvo).rpartition(":")
    if not host:
        return str(alvo), 53
    try:
        return host, int(porta)
    except ValueError:
        return host, 53


# --------------------------------------------------------------- o roteador


def _gateway_do_proc(texto: str) -> str:
    """O gateway padrão lido de ``/proc/net/route`` (Linux, que é o do Pi).

    Cada linha traz o destino em hexadecimal little-endian. Destino 00000000 é
    a rota padrão, e a terceira coluna é o endereço do roteador.
    """
    for linha in texto.splitlines()[1:]:
        campos = linha.split()
        if len(campos) < 3 or campos[1] != "00000000":
            continue
        try:
            cru = struct.pack("<L", int(campos[2], 16))
        except (ValueError, struct.error):
            continue
        return socket.inet_ntoa(cru)
    return ""


def _gateway_do_netstat(texto: str) -> str:
    """O mesmo no BSD e no macOS, onde /proc não existe."""
    for linha in texto.splitlines():
        campos = linha.split()
        if len(campos) >= 2 and campos[0] in ("default", "0.0.0.0"):
            candidato = campos[1]
            if candidato.replace(".", "").isdigit():
                return candidato
    return ""


def gateway(ler: Any = None, rodar: Any = None) -> str:
    """O endereço do roteador da casa, ou "" se não der para saber."""
    ler = ler or _ler_arquivo
    texto = ler("/proc/net/route")
    if texto:
        achado = _gateway_do_proc(texto)
        if achado:
            return achado
    rodar = rodar or _rodar
    saida = rodar(["netstat", "-rn"])
    return _gateway_do_netstat(saida) if saida else ""


def _ler_arquivo(caminho: str) -> str:
    try:
        with open(caminho, "r") as arquivo:
            return arquivo.read()
    except (IOError, OSError):
        return ""


def _rodar(argv: List[str]) -> str:
    try:
        return subprocess.check_output(
            argv, stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""


# ----------------------------------------------------------------- a medida


def medir(prazo: float = PRAZO_PADRAO, endereco_do_roteador: Optional[str] = None,
          alvos: Optional[List[str]] = None, nome: str = NOME_PARA_RESOLVER,
          tcp: Any = None, resolver: Any = None, achar_gateway: Any = None,
          relogio: Any = None) -> Dict[str, Any]:
    """Uma rodada das três perguntas. Devolve milissegundos ou ``None`` em cada."""
    tcp = tcp or tcp_ms
    relogio = relogio or _agora
    achar_gateway = achar_gateway or gateway

    roteador = endereco_do_roteador
    if roteador is None:
        roteador = achar_gateway()

    ms_roteador = None
    if roteador:
        # 53 e 80 porque roteador doméstico quase sempre atende os dois; se o
        # aparelho recusar a porta, a recusa já prova que ele está ligado --
        # quem está desligado dá tempo esgotado, não recusa.
        ms_roteador = tcp(roteador, 53, prazo)
        if ms_roteador is None:
            ms_roteador = tcp(roteador, 80, prazo)

    ms_internet = None
    quem_respondeu = ""
    for alvo in (alvos or list(ALVOS_INTERNET)):
        host, porta = _par(alvo)
        ms_internet = tcp(host, porta, prazo)
        if ms_internet is not None:
            quem_respondeu = alvo
            break

    ms_dns = None
    if resolver is None:
        resolver = socket.getaddrinfo
    comeco = relogio()
    try:
        resolver(nome, None)
        ms_dns = round((relogio() - comeco) * 1000.0, 1)
    except (OSError, socket.gaierror):
        ms_dns = None

    medida = {
        "gateway": roteador,
        "gateway_ms": ms_roteador,
        "internet_ms": ms_internet,
        "internet_target": quem_respondeu,
        "dns_ms": ms_dns,
    }
    medida["state"] = diagnosticar(medida)
    return medida


def diagnosticar(medida: Dict[str, Any]) -> str:
    """Onde quebrou, dizendo só a causa mais funda.

    Sem roteador, o resto não é notícia: com o wifi caído tudo falha junto, e
    contar as três falhas empurraria a pessoa a ligar para o provedor por causa
    de um cabo solto.
    """
    tem_roteador = medida.get("gateway")
    if tem_roteador and medida.get("gateway_ms") is None:
        return SEM_ROTEADOR
    if medida.get("internet_ms") is None:
        return SEM_INTERNET
    if medida.get("dns_ms") is None:
        return SEM_DNS
    return NO_AR


def pior(a: str, b: str) -> str:
    """O mais grave entre dois estados."""
    ordem = {estado: i for i, estado in enumerate(ORDEM_DA_CAUSA)}
    return a if ordem.get(a, 99) <= ordem.get(b, 99) else b


__all__ = [
    "ALVOS_INTERNET", "NOME_PARA_RESOLVER", "NO_AR", "SEM_DNS", "SEM_INTERNET",
    "SEM_ROTEADOR", "PRAZO_PADRAO", "tcp_ms", "gateway", "medir", "diagnosticar", "pior",
]
