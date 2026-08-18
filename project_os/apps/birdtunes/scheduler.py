"""The schedule: *"horarios q vc n ta em casa ... horarios q vc qr q ele toque"*.

Windows are pure data (start/end/days/playlist), evaluated by pure functions
so they are unit-testable without an event loop. :class:`SchedulerLoop` is the
thin asyncio wrapper that turns "is a window active right now" into actual
play/stop calls -- it holds no scheduling logic of its own, only a timer and
two callbacks, so a bug in "which window wins" is always reproducible with a
plain function call in a test.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import safety

log = logging.getLogger(__name__)

_DEFAULT_TIME = "00:00"
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
#: Os mesmos dias como a tela fala. Os de cima continuam existindo porque
#: são apelidos aceitos na entrada ("monday", "mon"), e não texto de tela.
_DIAS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

#: One-click presets the UI offers; they only prefill the editor (section 5).
PRESETS = [
    {
        "id": "while_out",
        "name": "Enquanto eu não estou",
        "start": "09:00", "end": "17:00", "days": [0, 1, 2, 3, 4],
    },
    {
        "id": "mornings",
        "name": "De manhã",
        "start": "07:00", "end": "10:00", "days": [0, 1, 2, 3, 4, 5, 6],
    },
    {
        "id": "afternoons",
        "name": "De tarde",
        "start": "14:00", "end": "18:00", "days": [0, 1, 2, 3, 4, 5, 6],
    },
    {
        "id": "weekends",
        "name": "Só no fim de semana",
        "start": "10:00", "end": "16:00", "days": [5, 6],
    },
]


def _parse_hhmm(value: Any, default: str) -> dt.time:
    text = str(value or default)
    try:
        hour, minute = text.split(":", 1)
        return dt.time(int(hour) % 24, int(minute) % 60)
    except (ValueError, TypeError):
        hour, minute = default.split(":", 1)
        return dt.time(int(hour), int(minute))


#: Accepted spellings for a weekday, so a schedule written by hand in
#: config.yaml ("mon") means the same thing as one written by the UI (0).
_DAY_ALIASES = {}  # type: Dict[str, int]
for _index, _name in enumerate(_WEEKDAY_NAMES):
    _DAY_ALIASES[_name.lower()] = _index
    _DAY_ALIASES[_name.lower()[:3]] = _index
_DAY_ALIASES.update({
    "seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5, "dom": 6,
})


def _coerce_day(value: Any) -> int:
    """A weekday as 0..6, or ValueError naming what was wrong."""
    if isinstance(value, bool):  # bool is an int in Python; never a weekday
        raise ValueError("days: %r is not a weekday" % (value,))
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError("days: %d is out of range (0 = Monday .. 6 = Sunday)" % value)
    text = str(value).strip().lower()
    if text.isdigit():
        return _coerce_day(int(text))
    if text in _DAY_ALIASES:
        return _DAY_ALIASES[text]
    raise ValueError("days: %r is not a weekday" % (value,))


def _coerce_hhmm(value: Any, field: str) -> str:
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":", 1)
        parsed = dt.time(int(hour), int(minute))
    except (ValueError, TypeError):
        raise ValueError("%s: %r is not a time of day (expected HH:MM)" % (field, value))
    return "%02d:%02d" % (parsed.hour, parsed.minute)


_SCHEDULE_KEYS = ("enabled", "quiet_hours", "windows")
_WINDOW_KEYS = ("id", "name", "enabled", "days", "start", "end", "playlist_id", "volume")


def normalize_schedule(body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a schedule coming off the wire, or raise ValueError.

    The endpoint used to write whatever it was handed straight into config, so
    ``{"schedule": {...}}`` (one wrapper too many) stored a nested dict, answered
    200, and changed nothing -- and ``days: ["mon"]`` fell through to "every day"
    because the parser swallowed its own error. Both are the kind of silence that
    matters here: this decides when a speaker turns itself on in someone's house.

    A ``{"schedule": {...}}`` wrapper is unwrapped rather than rejected: it is
    the shape GET returns, so round-tripping the response has to work.
    """
    if not isinstance(body, dict):
        raise ValueError("the schedule must be an object")
    if list(body.keys()) == ["schedule"] and isinstance(body["schedule"], dict):
        body = body["schedule"]

    unknown = [key for key in body if key not in _SCHEDULE_KEYS]
    if unknown:
        raise ValueError("unknown field(s): %s" % ", ".join(sorted(unknown)))

    result = {"enabled": bool(body.get("enabled", True))}  # type: Dict[str, Any]

    quiet = body.get("quiet_hours") or {}
    if not isinstance(quiet, dict):
        raise ValueError("quiet_hours must be an object with start and end")
    result["quiet_hours"] = {
        "start": _coerce_hhmm(quiet.get("start", "20:00"), "quiet_hours.start"),
        "end": _coerce_hhmm(quiet.get("end", "07:00"), "quiet_hours.end"),
    }

    windows_in = body.get("windows") or []
    if not isinstance(windows_in, list):
        raise ValueError("windows must be a list")
    windows = []  # type: List[Dict[str, Any]]
    for index, raw in enumerate(windows_in):
        if not isinstance(raw, dict):
            raise ValueError("windows[%d] must be an object" % index)
        unknown = [key for key in raw if key not in _WINDOW_KEYS]
        if unknown:
            raise ValueError("windows[%d]: unknown field(s): %s" % (index, ", ".join(sorted(unknown))))
        window = {
            "id": str(raw.get("id") or "window-%d" % index),
            "name": str(raw.get("name") or ""),
            "enabled": bool(raw.get("enabled", True)),
            "start": _coerce_hhmm(raw.get("start"), "windows[%d].start" % index),
            "end": _coerce_hhmm(raw.get("end"), "windows[%d].end" % index),
            "playlist_id": str(raw.get("playlist_id") or ""),
        }
        days = raw.get("days")
        if days is None:
            window["days"] = list(range(7))
        elif isinstance(days, (list, tuple)):
            try:
                window["days"] = sorted(set(_coerce_day(day) for day in days))
            except ValueError as exc:
                raise ValueError("windows[%d].%s" % (index, exc))
        else:
            raise ValueError("windows[%d].days must be a list" % index)
        if "volume" in raw:
            try:
                volume = float(raw["volume"])
            except (TypeError, ValueError):
                raise ValueError("windows[%d].volume must be a number between 0 and 1" % index)
            if not 0.0 <= volume <= 1.0:
                raise ValueError("windows[%d].volume must be between 0 and 1" % index)
            window["volume"] = volume
        windows.append(window)
    result["windows"] = _dedupe_windows(windows)
    return result


def _dedupe_windows(windows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop repeats and make ids unique.

    Tapping a preset twice used to append the same window again, and the screen
    then showed "While I'm out 09:00 - 17:00" twice with no way to tell them
    apart or remove one. Two identical windows also mean nothing: the first one
    wins every time. Same id with different times is a different story -- that is
    a real second window, so it keeps its own id instead of being thrown away.
    """
    seen_shape = set()
    seen_ids = set()
    out = []  # type: List[Dict[str, Any]]
    for window in windows:
        shape = (
            window["name"], window["start"], window["end"],
            tuple(window["days"]), window["playlist_id"], window.get("volume"),
        )
        if shape in seen_shape:
            continue
        seen_shape.add(shape)
        window_id = window["id"]
        if window_id in seen_ids:
            suffix = 2
            while "%s-%d" % (window_id, suffix) in seen_ids:
                suffix += 1
            window_id = "%s-%d" % (window_id, suffix)
            window["id"] = window_id
        seen_ids.add(window_id)
        out.append(window)
    return out


def active_window(moment: dt.datetime, schedule_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The first enabled window matching ``moment`` -- order is precedence (section 5)."""
    if not schedule_cfg.get("enabled", True):
        return None
    windows = schedule_cfg.get("windows") or []
    weekday = moment.weekday()
    now = moment.time()
    for window in windows:
        if not isinstance(window, dict) or not window.get("enabled", True):
            continue
        days = window.get("days")
        if days is not None:
            try:
                allowed = set(_coerce_day(day) for day in days)
            except (TypeError, ValueError) as exc:
                # A window nobody can read is a window that does not fire. The
                # old fallback here was "every day", which turns a typo in a
                # hand-edited config into a speaker playing at times nobody asked
                # for -- the one failure mode this app must not have.
                log.warning("ignoring window %r: %s", window.get("id", "?"), exc)
                continue
            if weekday not in allowed:
                continue
        start = _parse_hhmm(window.get("start"), _DEFAULT_TIME)
        end = _parse_hhmm(window.get("end"), _DEFAULT_TIME)
        if start <= end:
            hit = start <= now < end
        else:
            # Crosses midnight: the "today" half runs from start to 24:00,
            # the "tonight into tomorrow" half from 00:00 to end.
            hit = now >= start or now < end
        if hit:
            return window
    return None


def _would_play(moment: dt.datetime, schedule_cfg: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    window = active_window(moment, schedule_cfg)
    if window is None:
        return False, None
    if safety.is_quiet_hours(moment, schedule_cfg):
        return False, None
    return True, window


def _describe_moment(moment: dt.datetime, reference: dt.datetime) -> str:
    time_text = moment.strftime("%H:%M")
    if moment.date() == reference.date():
        return time_text
    if (moment.date() - reference.date()).days == 1:
        return "%s de amanhã" % time_text
    return "%s de %s" % (time_text, _DIAS[moment.weekday()])


def next_change(moment: dt.datetime, schedule_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """What happens next, and when -- shown permanently in the UI (section 5).

    A scheduled app that looks idle is indistinguishable from a broken one, so
    this never just says "stopped"; it searches minute by minute, up to eight
    days out, for the next point where "would a session be playing" flips.
    """
    if not schedule_cfg.get("enabled", True):
        return {"event": "none", "at": None, "window_id": None, "message": "A agenda está desligada."}

    playing_now, current = _would_play(moment, schedule_cfg)
    cursor = moment
    step = dt.timedelta(minutes=1)
    for _ in range(60 * 24 * 8):
        cursor = cursor + step
        playing, window = _would_play(cursor, schedule_cfg)
        if playing != playing_now:
            when = _describe_moment(cursor, moment)
            if playing:
                return {
                    "event": "starts", "at": cursor.isoformat(),
                    "window_id": window.get("id") if window else None,
                    "message": "Toca às %s" % when,
                }
            return {
                "event": "stops", "at": cursor.isoformat(),
                "window_id": current.get("id") if current else None,
                "message": "Para às %s" % when,
            }
    # Chegar aqui com janelas marcadas significa que o silêncio comeu todas
    # elas. Responder "nada marcado" seria a mentira mais cara desta tela: foi
    # exatamente ela que fez um horário das 05:00 parecer inexistente.
    conflitos = quiet_conflicts(schedule_cfg)
    if conflitos:
        return {
            "event": "quiet_blocked", "at": None,
            "window_id": conflitos[0].get("window_id") or None,
            "message": quiet_conflict_message(conflitos),
            "conflicts": conflitos,
        }
    return {
        "event": "none", "at": None, "window_id": None,
        "message": "Nada marcado para a próxima semana.",
    }


# -- o silêncio contra os horários -------------------------------------------
#
# O horário de silêncio ganha de tudo, inclusive de um "tocar agora" (ver
# ``safety.check_can_play`` e docs/BIRDTUNES.md seção 5) -- é a regra que
# protege o bicho de música às 2 da manhã, e ela não muda. O problema é outro:
# uma janela marcada dentro do silêncio era engolida **sem dizer nada**. A
# pessoa marcava 05:00, o padrão do silêncio é 20:00-07:00, e a tela ainda
# respondia "Nada marcado para a próxima semana". As funções abaixo existem
# para que isso vire aviso antes da hora, em vez de silêncio depois dela.


def _minutos_da_janela(window: Dict[str, Any]) -> List[int]:
    """Os minutos do dia cobertos pela janela, já virando a meia-noite.

    Uma janela de comprimento zero não cobre minuto nenhum -- é a mesma leitura
    de :func:`active_window`, onde ``start == end`` nunca casa.
    """
    inicio = _parse_hhmm(window.get("start"), _DEFAULT_TIME)
    fim = _parse_hhmm(window.get("end"), _DEFAULT_TIME)
    de = inicio.hour * 60 + inicio.minute
    ate = fim.hour * 60 + fim.minute
    if de == ate:
        return []
    if de < ate:
        return list(range(de, ate))
    return list(range(de, 24 * 60)) + list(range(0, ate))


def quiet_overlap(window: Dict[str, Any], schedule_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Quanto desta janela o horário de silêncio cala.

    O silêncio só olha a hora do dia, nunca o dia da semana, então basta varrer
    os minutos da janela uma vez: o resultado vale para todos os dias dela.
    """
    minutos = _minutos_da_janela(window)
    total = len(minutos)
    if not total:
        return {"kind": "none", "quiet_minutes": 0, "total_minutes": 0}
    base = dt.datetime(2001, 1, 1)
    calados = sum(
        1 for minuto in minutos
        if safety.is_quiet_hours(base + dt.timedelta(minutes=minuto), schedule_cfg)
    )
    if calados == 0:
        kind = "none"
    elif calados >= total:
        kind = "full"
    else:
        kind = "partial"
    return {"kind": kind, "quiet_minutes": calados, "total_minutes": total}


def _hhmm(window: Dict[str, Any], field: str) -> str:
    return _parse_hhmm(window.get(field), _DEFAULT_TIME).strftime("%H:%M")


def quiet_suggestion(window: Dict[str, Any], schedule_cfg: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Como encolher o silêncio para esta janela caber, ou ``None``.

    Em vez de deduzir qual das duas pontas mexer, testa as duas com a mesma
    função que mede o problema -- assim a sugestão não pode discordar do aviso
    que a acompanha.

    Entre as que resolvem, vence a que **preserva mais silêncio**. Não é
    detalhe: com o padrão 20:00-07:00 e uma janela às 21:00, encurtar o fim
    também livra a janela, só que reduz o silêncio de onze horas para uma e
    deixa a madrugada inteira desprotegida. Resolver o conflito destruindo a
    proteção que causou o conflito não é conserto.

    Uma sugestão que zeraria o silêncio (``start == end``, que
    :func:`safety.is_quiet_hours` lê como "nunca") é recusada pelo mesmo
    motivo, levado ao limite: desligar a proteção inteira não sai de um clique.
    """
    quiet = (schedule_cfg or {}).get("quiet_hours") or {}
    atual = {
        "start": str(quiet.get("start") or "20:00"),
        "end": str(quiet.get("end") or "07:00"),
    }
    candidatos = []
    for campo, valor in (("end", _hhmm(window, "start")), ("start", _hhmm(window, "end"))):
        proposto = dict(atual)
        proposto[campo] = valor
        if proposto["start"] == proposto["end"]:
            continue
        teste = dict(schedule_cfg or {})
        teste["quiet_hours"] = proposto
        if quiet_overlap(window, teste)["kind"] == "none":
            candidatos.append(proposto)
    if not candidatos:
        return None
    candidatos.sort(key=_minutos_de_silencio, reverse=True)
    return candidatos[0]


def _minutos_de_silencio(quiet: Dict[str, str]) -> int:
    """Quantos minutos do dia este horário de silêncio cobre."""
    inicio = _parse_hhmm(quiet.get("start"), "20:00")
    fim = _parse_hhmm(quiet.get("end"), "07:00")
    de = inicio.hour * 60 + inicio.minute
    ate = fim.hour * 60 + fim.minute
    if de == ate:
        return 0
    if de < ate:
        return ate - de
    return (24 * 60 - de) + ate


def quiet_conflicts(schedule_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """As janelas ligadas que o silêncio cala, no todo ou em parte."""
    schedule_cfg = schedule_cfg or {}
    if not schedule_cfg.get("enabled", True):
        return []
    conflitos = []
    for window in schedule_cfg.get("windows") or []:
        if not isinstance(window, dict) or not window.get("enabled", True):
            continue
        medida = quiet_overlap(window, schedule_cfg)
        if medida["kind"] == "none":
            continue
        conflitos.append({
            "window_id": str(window.get("id", "")),
            "window_name": str(window.get("name", "")),
            "start": _hhmm(window, "start"),
            "end": _hhmm(window, "end"),
            "kind": medida["kind"],
            "quiet_minutes": medida["quiet_minutes"],
            "total_minutes": medida["total_minutes"],
            "suggestion": quiet_suggestion(window, schedule_cfg),
        })
    return conflitos


def _nome_da_janela(conflito: Dict[str, Any]) -> str:
    nome = (conflito.get("window_name") or "").strip()
    if nome:
        return '"%s"' % nome
    return "O horário das %s" % conflito.get("start", "?")


def quiet_conflict_message(conflitos: List[Dict[str, Any]]) -> str:
    """A frase pronta, em português, que as duas telas mostram.

    Vem montada daqui porque o painel do app não sabe conjugar isto sozinho e
    o cartão do painel principal não traduz chave de app nenhuma.
    """
    calados = [c for c in conflitos if c.get("kind") == "full"]
    cortados = [c for c in conflitos if c.get("kind") == "partial"]
    if len(calados) == 1 and not cortados:
        c = calados[0]
        return "%s (%s às %s) está inteiro dentro do horário de silêncio, então não vai tocar." % (
            _nome_da_janela(c), c.get("start", "?"), c.get("end", "?"))
    if calados and not cortados:
        return "%d horários estão dentro do horário de silêncio, então não vão tocar." % len(calados)
    if len(cortados) == 1 and not calados:
        c = cortados[0]
        return "%s (%s às %s) entra no horário de silêncio e vai parar antes do fim." % (
            _nome_da_janela(c), c.get("start", "?"), c.get("end", "?"))
    if cortados and not calados:
        return "%d horários entram no horário de silêncio e vão parar antes do fim." % len(cortados)
    return "%d horários batem com o horário de silêncio: %d %s tocar e %d %s parar antes do fim." % (
        len(calados) + len(cortados),
        len(calados), "não vai" if len(calados) == 1 else "não vão",
        len(cortados), "vai" if len(cortados) == 1 else "vão")


async def _maybe_call(callback: Optional[Callable[..., Any]], *args: Any) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class SchedulerLoop(object):
    """Ticks once every ``interval`` seconds and calls back on a state change.

    Deliberately dumb: it asks :func:`_would_play` for the truth and only
    reacts to the answer changing, so "should we be playing" always has one
    source of truth that a test can call directly without an event loop.
    """

    def __init__(
        self,
        get_schedule: Callable[[], Dict[str, Any]],
        on_should_play: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_should_stop: Optional[Callable[[], Any]] = None,
        interval: float = 15.0,
        clock: Optional[Callable[[], dt.datetime]] = None,
        on_still_open: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self._get_schedule = get_schedule
        self._on_should_play = on_should_play
        self._on_should_stop = on_should_stop
        # Chamado a cada volta em que a janela já estava aberta. Sem isto, uma
        # janela só era olhada no instante em que abria: se a música parasse no
        # meio -- TV desligada, conexão caída, erro no meio do caminho -- o
        # silêncio durava até o fim da janela, e para quem marcou o horário isso
        # é indistinguível de "não tocou".
        self._on_still_open = on_still_open
        self._interval = float(interval)
        self._clock = clock or dt.datetime.now
        self._task = None  # type: Optional[asyncio.Future]
        self._active_window_id = None  # type: Optional[str]

    @property
    def active_window_id(self) -> Optional[str]:
        return self._active_window_id

    async def tick(self) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """One evaluation, exposed directly so tests do not need real sleeps."""
        moment = self._clock()
        schedule_cfg = self._get_schedule() or {}
        playing, window = _would_play(moment, schedule_cfg)
        window_id = window.get("id") if window else None
        if playing and window_id != self._active_window_id:
            self._active_window_id = window_id
            await _maybe_call(self._on_should_play, window)
        elif playing and self._on_still_open is not None:
            await _maybe_call(self._on_still_open, window)
        elif not playing and self._active_window_id is not None:
            self._active_window_id = None
            await _maybe_call(self._on_should_stop)
        return playing, window

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except Exception:  # pragma: no cover - a bad tick must not kill the loop
                    log.warning("scheduler tick failed", exc_info=True)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()


__all__ = [
    "PRESETS",
    "SchedulerLoop",
    "active_window",
    "next_change",
    "quiet_conflicts",
    "quiet_conflict_message",
    "quiet_overlap",
    "quiet_suggestion",
]
