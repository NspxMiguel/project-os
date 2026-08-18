# -*- coding: utf-8 -*-
"""O laço que mede, o banco que lembra e as rotas que a tela usa.

A lógica das medidas mora em ``probes.py`` e não encosta em nada disto: aqui é
só quando medir, o que guardar e o que responder.

Uma decisão que vale explicar: **medida de rotina é apagada, queda não é**. As
medidas existem para desenhar a linha do tempo dos últimos dias e são milhares
por semana; a queda é o que ele vai querer citar para o provedor daqui a três
meses, e ocupa uma linha.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from project_os import auth
from project_os.core.plugins import AppContext, AppInstance
from project_os.db import rows_to_dicts, utcnow_iso

from . import probes

APP_ID = "internet"
SCHEMA_NAME = "internet"
TABELA_MEDIDAS = "app_internet_samples"
TABELA_QUEDAS = "app_internet_outages"

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS app_internet_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        state TEXT NOT NULL,
        gateway_ms REAL,
        internet_ms REAL,
        dns_ms REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_internet_samples_ts ON app_internet_samples (ts)",
    """
    CREATE TABLE IF NOT EXISTS app_internet_outages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL DEFAULT '',
        seconds INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_internet_outages_started ON app_internet_outages (started_at)",
]

INTERVALO_PADRAO = 60.0
#: Abaixo disto a medida vira sondagem: o roteador leva três batidas por minuto
#: e a resposta não fica mais verdadeira.
INTERVALO_MINIMO = 20.0
DIAS_PADRAO = 30

#: De quanto em quanto tempo apagar medida velha. Fazer isso a cada volta seria
#: um DELETE por minuto para apagar nada.
LIMPAR_A_CADA_S = 3600.0


def build_router(instance: "InternetApp") -> APIRouter:
    router = APIRouter()

    @router.get("/status")
    async def status(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
        return instance.panorama()

    @router.get("/outages")
    async def outages(
        limit: int = Query(30, ge=1, le=200),
        user: Dict[str, Any] = Depends(auth.require_auth),
    ) -> Dict[str, Any]:
        return {"outages": instance.quedas(limit)}

    @router.get("/samples")
    async def samples(
        hours: int = Query(24, ge=1, le=24 * 30),
        user: Dict[str, Any] = Depends(auth.require_auth),
    ) -> Dict[str, Any]:
        return {"samples": instance.medidas(hours)}

    @router.post("/check")
    async def check(user: Dict[str, Any] = Depends(auth.require_auth)) -> Dict[str, Any]:
        """Medir agora, sem esperar a próxima volta do laço."""
        medida = await instance.medir_uma_vez()
        return {"measurement": medida, "status": instance.panorama()}

    return router


class InternetApp(AppInstance):
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.log = getattr(ctx, "logger", None) or logging.getLogger("project_os.apps.internet")
        self.router = build_router(self)
        self._task = None  # type: Optional[asyncio.Task]
        self._threads = None  # type: Optional[concurrent.futures.ThreadPoolExecutor]
        self._ultima = {}  # type: Dict[str, Any]
        self._estado = ""
        self._estado_desde = ""
        self._ultima_limpeza = 0.0

    # -- ciclo de vida ---------------------------------------------------
    async def start(self) -> None:
        self.ctx.db.register_schema(SCHEMA_NAME, SCHEMA_STATEMENTS)
        self._recuperar_estado()
        # Thread própria, e não a do laço de eventos: uma conexão TCP com prazo
        # bloqueia, e o executor padrão é compartilhado com o sistema inteiro.
        self._threads = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="internet")
        self._task = asyncio.ensure_future(self._laco())

    async def stop(self) -> None:
        """Sair rápido, mesmo com uma medida no meio.

        Uma medida pode estar parada num socket esperando até três segundos por
        aparelho que não responde, e esse thread não é cancelável. Esperar por
        ele aqui faria o desligamento do app custar o pior caso da rede -- na
        suíte de testes isso apareceu como cada teste que sobe os apps ficando
        segundos mais lento, e numa caixa sem rede seria o serviço demorando
        para parar. O resultado da medida em voo simplesmente não interessa a
        ninguém: quem está desligando não vai ler.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._threads is not None:
            self._threads.shutdown(wait=False)
            self._threads = None

    def status(self) -> Dict[str, Any]:
        """O cartão da tela inicial: uma frase, não um despejo de campos.

        A tela inicial não sabe traduzir chave de app nenhum, então quem manda a
        frase pronta é o app -- é o mesmo caminho que o BirdTunes usa.
        """
        panorama = self.panorama()
        estado = panorama["state"]
        cartao = dict(panorama)
        cartao.update({
            "state": estado,
            "level": {probes.NO_AR: "ok", probes.SEM_DNS: "warn"}.get(estado, "danger"),
            "summary": self._frase(panorama),
            "fields": self._campos(panorama),
        })
        return cartao

    def _frase(self, panorama: Dict[str, Any]) -> str:
        estado = panorama["state"]
        if estado == probes.SEM_ROTEADOR:
            return "O roteador não responde. O problema é aqui dentro de casa, não no provedor."
        if estado == probes.SEM_INTERNET:
            return "Sem internet: o roteador está de pé e o provedor não está entregando."
        if estado == probes.SEM_DNS:
            return "Os nomes não estão resolvendo. A conexão funciona por endereço."
        if not panorama["outages_24h"]:
            return "Funcionando. Nenhuma queda nas últimas 24 horas."
        quantas = panorama["outages_24h"]
        return "Funcionando. %d %s nas últimas 24 horas, %s fora do ar." % (
            quantas, "queda" if quantas == 1 else "quedas",
            _duracao(panorama["downtime_24h_seconds"]))

    def _campos(self, panorama: Dict[str, Any]) -> List[Dict[str, Any]]:
        ultima = panorama.get("last") or {}

        def _ms(valor):
            return "%d ms" % round(valor) if valor is not None else "não respondeu"

        campos = [
            {"label": "Roteador", "value": _ms(ultima.get("gateway_ms")), "kind": "text"},
            {"label": "Internet", "value": _ms(ultima.get("internet_ms")), "kind": "text"},
            {"label": "Nomes (DNS)", "value": _ms(ultima.get("dns_ms")), "kind": "text"},
        ]
        if panorama["outages_24h"]:
            campos.append({"label": "Quedas em 24h", "value": panorama["outages_24h"],
                           "kind": "number"})
            campos.append({"label": "Fora do ar em 24h",
                           "value": panorama["downtime_24h_seconds"], "kind": "duration"})
        return campos

    # -- o laço ----------------------------------------------------------
    @property
    def intervalo(self) -> float:
        pedido = float(self.ctx.config.get("interval_seconds", INTERVALO_PADRAO) or INTERVALO_PADRAO)
        return max(INTERVALO_MINIMO, pedido)

    async def _laco(self) -> None:
        while True:
            try:
                await self.medir_uma_vez()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - o laço não pode morrer
                self.log.exception("falha ao medir a internet")
            await asyncio.sleep(self.intervalo)

    async def medir_uma_vez(self) -> Dict[str, Any]:
        """Uma rodada, fora do laço de eventos: socket com prazo bloqueia."""
        prazo = float(self.ctx.config.get("timeout_seconds", probes.PRAZO_PADRAO)
                      or probes.PRAZO_PADRAO)
        roteador = str(self.ctx.config.get("gateway", "") or "") or None
        alvos = self._alvos()
        laco = asyncio.get_event_loop()
        medida = await laco.run_in_executor(
            self._threads,
            lambda: probes.medir(prazo=prazo, endereco_do_roteador=roteador, alvos=alvos))
        self._registrar(medida)
        return medida

    def _alvos(self) -> List[str]:
        """Quem responde pela pergunta "a internet está de pé?".

        Configurável porque rede que bloqueia DNS público existe -- escola,
        empresa, portal cativo de hotel. Com os três alvos padrão bloqueados, um
        app de alvo fixo acusaria queda para sempre numa rede que funciona.
        """
        crus = self.ctx.config.get("targets", None)
        if isinstance(crus, str):
            crus = [p.strip() for p in crus.split(",")]
        lista = [str(a).strip() for a in (crus or []) if str(a).strip()]
        return lista or list(probes.ALVOS_INTERNET)

    # -- banco -----------------------------------------------------------
    def _registrar(self, medida: Dict[str, Any]) -> None:
        agora = utcnow_iso()
        self._ultima = dict(medida, ts=agora)
        self.ctx.db.execute(
            "INSERT INTO %s (ts, state, gateway_ms, internet_ms, dns_ms) VALUES (?, ?, ?, ?, ?)"
            % TABELA_MEDIDAS,
            (agora, medida["state"], medida.get("gateway_ms"),
             medida.get("internet_ms"), medida.get("dns_ms")),
        )
        if medida["state"] != self._estado:
            self._virou(medida["state"], agora)
        self._talvez_limpar()

    def _virou(self, novo: str, agora: str) -> None:
        """Abre uma queda ao sair do ar, fecha ao voltar.

        Uma queda que muda de causa no meio -- roteador que volta e provedor que
        não -- fecha a primeira e abre a segunda: são dois problemas e duas
        conversas diferentes.
        """
        anterior = self._estado
        if anterior and anterior != probes.NO_AR:
            self._fechar_queda(agora)
        if novo != probes.NO_AR:
            self.ctx.db.execute(
                "INSERT INTO %s (kind, started_at) VALUES (?, ?)" % TABELA_QUEDAS,
                (novo, agora),
            )
            self.ctx.emit("outage", {"kind": novo, "started_at": agora})
        elif anterior:
            self.ctx.emit("restored", {"after": anterior, "at": agora})
        self._estado = novo
        self._estado_desde = agora

    def _fechar_queda(self, agora: str) -> None:
        abertas = self.ctx.db.query(
            "SELECT id, started_at FROM %s WHERE ended_at = '' ORDER BY id DESC LIMIT 1"
            % TABELA_QUEDAS)
        if not abertas:
            return
        linha = rows_to_dicts(abertas)[0]
        self.ctx.db.execute(
            "UPDATE %s SET ended_at = ?, seconds = ? WHERE id = ?" % TABELA_QUEDAS,
            (agora, _segundos_entre(linha["started_at"], agora), linha["id"]),
        )

    def _recuperar_estado(self) -> None:
        """Depois de reiniciar, continuar a história em vez de recomeçar.

        Sem isto, todo reinício do serviço fecharia a queda em aberto sem
        perceber e abriria outra na volta -- e uma queda de duas horas viraria
        três de quarenta minutos.
        """
        linhas = self.ctx.db.query(
            "SELECT state, ts FROM %s ORDER BY id DESC LIMIT 1" % TABELA_MEDIDAS)
        if linhas:
            ultima = rows_to_dicts(linhas)[0]
            self._estado = str(ultima["state"])
            self._estado_desde = str(ultima["ts"])

    def _talvez_limpar(self) -> None:
        agora = time.time()
        if agora - self._ultima_limpeza < LIMPAR_A_CADA_S:
            return
        self._ultima_limpeza = agora
        dias = int(self.ctx.config.get("keep_days", DIAS_PADRAO) or DIAS_PADRAO)
        corte = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(agora - dias * 86400))
        self.ctx.db.execute("DELETE FROM %s WHERE ts < ?" % TABELA_MEDIDAS, (corte,))

    # -- leituras --------------------------------------------------------
    def quedas(self, limite: int = 30) -> List[Dict[str, Any]]:
        linhas = self.ctx.db.query(
            "SELECT id, kind, started_at, ended_at, seconds FROM %s "
            "ORDER BY started_at DESC LIMIT ?" % TABELA_QUEDAS, (limite,))
        return rows_to_dicts(linhas)

    def medidas(self, horas: int = 24) -> List[Dict[str, Any]]:
        corte = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - horas * 3600))
        linhas = self.ctx.db.query(
            "SELECT ts, state, gateway_ms, internet_ms, dns_ms FROM %s "
            "WHERE ts >= ? ORDER BY ts" % TABELA_MEDIDAS, (corte,))
        return rows_to_dicts(linhas)

    def panorama(self) -> Dict[str, Any]:
        recentes = self.quedas(limite=50)
        corte = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
        do_dia = [q for q in recentes if q["started_at"] >= corte]
        fora_do_ar = sum(int(q["seconds"] or 0) for q in do_dia)
        aberta = next((q for q in recentes if not q["ended_at"]), None)
        return {
            "state": self._estado or probes.NO_AR,
            "since": self._estado_desde,
            "gateway": (self._ultima or {}).get("gateway", ""),
            "last": self._ultima or {},
            "interval_seconds": self.intervalo,
            "outages_24h": len(do_dia),
            "downtime_24h_seconds": fora_do_ar,
            "current_outage": aberta,
            "last_outage": recentes[0] if recentes else None,
        }


def _duracao(segundos: Any) -> str:
    """"8s", "12min", "2h5min" -- o mesmo formato que a tela do app usa."""
    total = max(0, int(segundos or 0))
    if total < 60:
        return "%ds" % total
    if total < 3600:
        return "%dmin" % round(total / 60.0)
    horas, resto = divmod(total, 3600)
    minutos = int(round(resto / 60.0))
    return "%dh%dmin" % (horas, minutos) if minutos else "%dh" % horas


def _segundos_entre(comeco: str, fim: str) -> int:
    def _para_epoca(texto: str) -> float:
        try:
            return time.mktime(time.strptime(str(texto)[:19], "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError):
            return 0.0

    a, b = _para_epoca(comeco), _para_epoca(fim)
    return int(max(0.0, b - a)) if a and b else 0


def setup(ctx: AppContext) -> InternetApp:
    return InternetApp(ctx)


__all__ = ["InternetApp", "setup", "APP_ID"]
