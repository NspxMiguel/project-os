"""Which timezone this box is in, asked over the network.

    "puxar fuso pela net ne..."

The image boots on UTC because an image cannot know where the card will end up,
and a headless box has nobody to ask. What it does have is an internet
connection, and the address it comes out of is enough to place it -- the same
trick a phone uses before you sign in to anything.

Three rules shape this module:

* **It is a guess, and it says so.** The result carries where it came from, so
  the screen can show "America/Sao_Paulo, from the network" rather than pretend
  somebody chose it.
* **It never overwrites a choice.** A timezone typed by a person, or picked from
  the browser, wins over an IP lookup forever.
* **The endpoint is configuration, not a constant.** This is a public project;
  someone will want a different provider, or none at all, and that must be a
  setting rather than a patch ("nada harcoded, tudo configuravel").

Failure is normal here: no internet on first boot, a provider that moved, a
captive portal answering HTML. Every one of those returns "no idea" and leaves
the clock alone, because a wrong timezone silently applied is worse than an
obvious UTC.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

#: Default provider. Plain HTTP JSON, no key, no account, and it answers the one
#: question asked. Overridable through ``system.timezone_url``.
DEFAULT_LOOKUP_URL = "http://ip-api.com/json/?fields=status,timezone"

TIMEOUT = 8.0

#: Keys different providers use for the same answer, so swapping the URL for
#: another service usually needs no code at all.
_FIELDS = ("timezone", "time_zone", "timeZone", "tz")


def _plausible(name: str) -> bool:
    """A zone name, not an error page or a country code.

    ``zoneinfo`` is the real judge, but it is not always installed with data on
    a minimal system, and a cheap shape check keeps rubbish out of config.
    """
    if not name or len(name) > 64 or " " in name:
        return False
    if name in ("UTC", "GMT"):
        return True
    return "/" in name and name.replace("/", "").replace("_", "").replace("-", "").replace("+", "").isalnum()


def lookup(url: str = DEFAULT_LOOKUP_URL, timeout: float = TIMEOUT) -> Optional[str]:
    """Ask the network where this machine is. ``None`` when it cannot say."""
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    if not url:
        return None
    request = Request(url, headers={"User-Agent": "project-os"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured URL
            raw = response.read(64 * 1024)
    except (URLError, OSError) as exc:
        log.info("timezone lookup failed (%s); leaving the clock alone", exc)
        return None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        log.info("timezone lookup did not answer JSON; leaving the clock alone")
        return None
    if not isinstance(payload, dict):
        return None

    for field in _FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and _plausible(value.strip()):
            return value.strip()
    log.info("timezone lookup answered without a timezone; leaving the clock alone")
    return None


def ensure(config: Any) -> Dict[str, Any]:
    """Fill in ``system.timezone`` from the network when nobody has set it.

    Returns what happened, so a caller can log or show it. Never raises: this
    runs during boot, and a box that will not start because a lookup service is
    down would be a poor trade for a convenience.
    """
    result = {"timezone": "", "source": "", "changed": False}  # type: Dict[str, Any]
    try:
        current = str(config.get("system.timezone", "") or "").strip()
    except Exception:  # pragma: no cover - config is never this broken
        return result
    if current:
        result["timezone"] = current
        result["source"] = "configured"
        return result

    try:
        if not bool(config.get("system.timezone_auto", True)):
            return result
        url = str(config.get("system.timezone_url", DEFAULT_LOOKUP_URL) or DEFAULT_LOOKUP_URL)
        found = lookup(url)
    except Exception as exc:  # pragma: no cover - urllib is wrapped already
        log.info("timezone lookup raised %s; leaving the clock alone", exc)
        return result

    if not found:
        return result

    try:
        config.set("system.timezone", found)
        config.save()
    except Exception as exc:  # pragma: no cover
        log.warning("could not store the timezone %r: %s", found, exc)
        return result

    log.info("timezone set to %s from the network", found)
    result.update({"timezone": found, "source": "network", "changed": True})
    return result


# ---------------------------------------------------------------------------
# desde quando esta caixa está ligada
# ---------------------------------------------------------------------------
# Uma Raspberry não tem bateria de relógio. Ela sobe com a hora que estava
# gravada no cartão -- que numa imagem recém-gravada é **a hora em que a imagem
# foi construída** -- e só depois o NTP acerta.
#
# Foi medido no Pi dele, no primeiro boot do cartão novo: a imagem trazia
# fake-hwclock 2026-08-12 04:23:57 e o serviço estampou started_at
# 2026-08-12T04:24:14Z, dezessete segundos depois. O relógio de verdade era
# 15:49. A tela passaria a dizer que a caixa estava ligada há onze horas, para
# sempre, até o serviço reiniciar -- e ele ia perguntar por quê.
#
# Estampar a hora no boot é o erro. O que se guarda é um ponto no relógio
# monotônico (que não anda para trás nem pula quando o NTP corrige) e o começo é
# calculado na hora de responder: agora menos quanto tempo faz. Assim a resposta
# é certa mesmo que o relógio dê um salto de onze horas no meio.


# ---------------------------------------------------------------------------
# que horas são nesta caixa, e se dá para confiar nisso
# ---------------------------------------------------------------------------
# O fuso estava certo na configuração dele -- ``America/Sao_Paulo`` -- e mesmo
# assim o BirdTunes calculava tudo em UTC, três horas à frente da vida dele.
# ``zoneinfo`` lê a base de fusos do sistema, a imagem instala com
# ``--no-install-recommends`` e ninguém pediu ``tzdata``. Sem a base,
# ``ZoneInfo("America/Sao_Paulo")`` levanta, o app cai no relógio do sistema e
# segue em UTC -- avisando num ``log.warning`` que ninguém lê.
#
# O estrago passa despercebido porque *parece* funcionar: a caixa mostra uma
# hora, a agenda tem horários, tudo responde. Só que o horário de silêncio
# começava às 17:00 dele e a rotina do meio-dia tocava às 09:30.
#
# Daí estas funções: uma só maneira de perguntar as horas, e um estado que diz
# **quando não deu** em vez de fingir que deu.

#: Diferença a partir da qual o relógio de quem olha e o da caixa discordam de
#: verdade. Um minuto é deriva normal de NTP; cinco já é outra história.
DERIVA_TOLERADA_S = 300


def _config_da_caixa(config: Any) -> Any:
    """A configuração da caixa, mesmo recebendo a fatia de um app.

    Um app recebe ``ctx.config``, que é uma vista de ``apps.settings.<id>``:
    pedir ``system.timezone`` ali procura por
    ``apps.settings.birdtunes.system.timezone``, que não existe em lugar
    nenhum, e volta vazio. Foi exatamente assim que o BirdTunes calculou
    agenda em UTC numa caixa configurada para ``America/Sao_Paulo`` -- a tela
    de Ajustes mostrava o fuso certo, o app nunca chegou a vê-lo, e o
    ``except`` que sobrava escreveu um aviso de log e seguiu em frente.

    Que fuso a caixa está é pergunta da caixa, não do app, então aqui se sobe
    para a configuração de verdade que está atrás da fatia. Vale para qualquer
    app, e não só para o que descobriu o problema.
    """
    interno = getattr(config, "_config", None)
    if interno is not None and hasattr(interno, "get"):
        return interno
    return config


def zona(config: Any = None) -> Dict[str, Any]:
    """Qual fuso esta caixa consegue mesmo aplicar.

    Devolve o que foi pedido (``name``), o que vai valer de fato
    (``effective``) e por que eles diferem, quando diferem. Nunca levanta: é
    chamada de dentro de laço de agenda e de rota de status.
    """
    pedido = ""
    if config is not None:
        try:
            pedido = str(_config_da_caixa(config).get("system.timezone", "") or "").strip()
        except Exception:  # pragma: no cover - config nunca está tão quebrada
            pedido = ""

    if not pedido:
        return {"name": "", "effective": "", "resolved": False, "problem": "unset",
                "detail": ""}

    try:
        from zoneinfo import ZoneInfo
    except ImportError as exc:
        return {"name": pedido, "effective": "", "resolved": False,
                "problem": "no_zoneinfo", "detail": str(exc)}

    try:
        ZoneInfo(pedido)
    except Exception as exc:
        # Duas falhas bem diferentes chegam aqui, e a tela precisa dizer coisas
        # diferentes: a base de fusos não está instalada (o caso do Pi, onde a
        # imagem instala com --no-install-recommends), ou o nome é que não
        # existe. Quem separa as duas é uma zona que qualquer base tem: se nem
        # ela resolve, o que falta é a base.
        try:
            ZoneInfo("America/Sao_Paulo")
            ZoneInfo("Europe/Lisbon")
            falta_base = False
        except Exception:
            falta_base = True
        return {"name": pedido, "effective": "", "resolved": False,
                "problem": "tz_unavailable" if falta_base else "tz_unknown",
                "detail": "%s: %s" % (type(exc).__name__, exc)}

    return {"name": pedido, "effective": pedido, "resolved": True, "problem": "",
            "detail": ""}


def now(config: Any = None) -> datetime:
    """A hora local da casa, sem fuso pendurado.

    Um só lugar decide isto. Antes cada app tinha a sua cópia do ``try/except``,
    e a cópia do BirdTunes engolia a falha num aviso de log.
    """
    estado = zona(config)
    if not estado["resolved"]:
        return datetime.now()
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(estado["effective"])).replace(tzinfo=None)


def _frase(estado: Dict[str, Any], desligado: bool) -> str:
    """A frase pronta em português. O painel não traduz chave de núcleo."""
    if estado["problem"] == "unset":
        return ("Nenhum fuso escolhido: a caixa está contando as horas em UTC, "
                "que quase nunca é a hora daqui.")
    if estado["problem"] == "tz_unavailable":
        return ("A caixa não tem a base de fusos instalada, então %s não pôde ser "
                "aplicado e tudo está rodando em UTC. Atualize a caixa para "
                "instalar a base." % estado["name"])
    if estado["problem"] == "tz_unknown":
        return ("O fuso %s não existe na lista de fusos, então a caixa ficou em "
                "UTC. Escolha um em Ajustes." % estado["name"])
    if estado["problem"] == "no_zoneinfo":
        return ("Este Python não sabe ler fusos, então a caixa está em UTC mesmo "
                "com %s escolhido." % estado["name"])
    if desligado:
        return "O relógio da caixa está diferente do seu; um dos dois está errado."
    return "Relógio certo."


def state(config: Any = None, browser_epoch: Optional[float] = None) -> Dict[str, Any]:
    """Tudo que a tela precisa para mostrar o relógio e desconfiar dele.

    ``browser_epoch`` é o ``Date.now()/1000`` de quem está olhando: é ele que
    transforma "a caixa diz 21:41" em "a caixa está três horas à frente de
    você", que é a forma em que o problema fica óbvio.
    """
    estado = zona(config)
    agora_utc = datetime.now(timezone.utc)
    local = now(config)

    deriva = None
    desligado = False
    if browser_epoch is not None:
        try:
            deriva = float(browser_epoch) - agora_utc.timestamp()
            desligado = abs(deriva) > DERIVA_TOLERADA_S
        except (TypeError, ValueError):
            deriva = None

    offset = round((local - agora_utc.replace(tzinfo=None)).total_seconds() / 60.0)
    return {
        "local": local.isoformat(timespec="seconds"),
        "utc": agora_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "epoch": agora_utc.timestamp(),
        "timezone": estado["name"],
        "effective": estado["effective"] or "UTC",
        "resolved": estado["resolved"],
        "problem": estado["problem"],
        "detail": estado["detail"],
        "offset_minutes": offset,
        "drift_seconds": deriva,
        "clock_disagrees": desligado,
        "ok": estado["resolved"] and not desligado,
        "message": _frase(estado, desligado),
    }


def mark_start(state: Any) -> None:
    """Guarda o começo de duas formas: monotônica (que vale) e de parede."""
    state.started_monotonic = time.monotonic()
    state.started_at = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def uptime(state: Any) -> Optional[float]:
    """Quantos segundos esta caixa está de pé, ou None se ninguém marcou."""
    inicio = getattr(state, "started_monotonic", None)
    if inicio is None:
        return None
    return max(0.0, time.monotonic() - float(inicio))


def started_at(state: Any) -> Optional[str]:
    """Quando esta caixa subiu, contado para trás a partir de agora.

    Cai para a estampa do boot quando não há referência monotônica -- não é
    melhor, é o que existia antes, e é melhor que None.
    """
    faz = uptime(state)
    if faz is None:
        return getattr(state, "started_at", None)
    quando = datetime.now(timezone.utc) - timedelta(seconds=faz)
    return quando.isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = ["DEFAULT_LOOKUP_URL", "DERIVA_TOLERADA_S", "ensure", "lookup", "mark_start",
           "now", "started_at", "state", "uptime", "zona"]
