"""Ajudantes -- the machines that lend this ProjectOS what the Pi does not have.

*"opção de conectar outros pcs pra ajuda no processamento tbm é legal... tipo a,
tem um pc ligado, sem nada processando, e o rp ta sofrendo... se ele tiver com o
app baixado nele dale. suporte a windows mac e linux"* and *"o esp32 tbm n
precisa ser s'ó um sistema, poderia ser tbm algo q ajuda o rp, algum acessorio"*.

Both sentences describe the same thing, so they are one subsystem. A helper is
any machine that pairs with the Pi and declares what it can contribute: a PC
declares ``cpu``/``transcode``/``gpu``, an ESP32 declares ``sensor``/``relay``/
``display``. The Pi stays the brain either way.

Three decisions worth knowing before reading the code:

**Helpers connect outbound.** The PC opens a websocket to the Pi, never the
reverse. Nobody has to forward a port, open a firewall, or give their laptop a
fixed address, and a helper that goes to sleep just stops talking.

**Pairing is a six-digit code, not a token you paste.** The code lives in the
database rather than in memory so restarting the Pi mid-pairing does not
silently kill the code someone is already typing. It is short-lived and
single-use; what it buys you is a long random token, and only the token's hash
is stored -- a stolen database does not hand out helper identities.

**Offloading is a job queue, not process migration.** Nothing here moves a
running program to another machine; that is a research project, not a feature.
What it does is hand whole tasks -- transcode this file, run this scan -- to
whoever declared they can do that kind of work. Being honest about this in the
code keeps the UI from promising magic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from typing import Any, Dict, Iterable, List, Optional

from projectos.db import row_to_dict, rows_to_dicts, utcnow_iso

log = logging.getLogger(__name__)

# -- kinds -------------------------------------------------------------------

#: A desktop, laptop or server running the helper agent. Windows, macOS, Linux.
KIND_PC = "pc"
#: Another Raspberry Pi (or any SBC) running the agent. Same protocol as a PC.
KIND_PI = "pi"
#: A microcontroller running SimpleProjectOS. Talks the same protocol, but with
#: a tiny capability set and no job execution beyond its own hardware.
KIND_ESP32 = "esp32"
#: Something that paired without saying what it is.
KIND_OTHER = "other"

KINDS = (KIND_PC, KIND_PI, KIND_ESP32, KIND_OTHER)

# -- capabilities ------------------------------------------------------------
#
# A capability is a promise: "ask me for jobs that need this". Job routing is
# nothing more than set containment, which is why the vocabulary is a flat list
# of strings instead of a class hierarchy nobody would extend.

CAP_CPU = "cpu"  # general compute: scripts, batch work
CAP_GPU = "gpu"  # hardware acceleration available
CAP_TRANSCODE = "transcode"  # ffmpeg present, can re-encode media
CAP_DOWNLOAD = "download"  # fetch large files on the Pi's behalf
CAP_STORAGE = "storage"  # spare disk offered to the Pi
CAP_SENSOR = "sensor"  # reports readings (temperature, motion, light)
CAP_ACTUATOR = "actuator"  # relay, motor, anything that moves the world
CAP_DISPLAY = "display"  # a screen ProjectOS can draw on
CAP_BUTTON = "button"  # physical input that fires events
CAP_INFRARED = "infrared"  # IR blaster / receiver
CAP_AUDIO = "audio"  # can play sound (an ESP32 with a DAC counts)

CAPABILITIES = (
    CAP_CPU, CAP_GPU, CAP_TRANSCODE, CAP_DOWNLOAD, CAP_STORAGE,
    CAP_SENSOR, CAP_ACTUATOR, CAP_DISPLAY, CAP_BUTTON, CAP_INFRARED, CAP_AUDIO,
)

#: What each kind is allowed to claim. An ESP32 announcing ``gpu`` is either a
#: bug or a lie, and both are worth dropping rather than routing work to.
KIND_CAPABILITIES = {
    KIND_PC: set(CAPABILITIES) - {CAP_SENSOR, CAP_ACTUATOR, CAP_BUTTON},
    KIND_PI: set(CAPABILITIES),
    KIND_ESP32: {
        CAP_SENSOR, CAP_ACTUATOR, CAP_DISPLAY, CAP_BUTTON, CAP_INFRARED, CAP_AUDIO,
    },
    KIND_OTHER: set(CAPABILITIES),
}

#: Human wording for the UI. Kept here so the screen and the API agree.
CAPABILITY_LABELS = {
    CAP_CPU: "processamento",
    CAP_GPU: "aceleracao por GPU",
    CAP_TRANSCODE: "converter video e audio",
    CAP_DOWNLOAD: "baixar arquivos grandes",
    CAP_STORAGE: "espaco em disco",
    CAP_SENSOR: "sensores",
    CAP_ACTUATOR: "reles e motores",
    CAP_DISPLAY: "tela",
    CAP_BUTTON: "botoes fisicos",
    CAP_INFRARED: "infravermelho",
    CAP_AUDIO: "audio",
}

# -- pairing -----------------------------------------------------------------

CODE_DIGITS = 6
CODE_TTL_SECONDS = 600.0
#: Enough attempts to survive a genuine collision, few enough to fail loudly.
CODE_ATTEMPTS = 20
TOKEN_BYTES = 32

# -- liveness ----------------------------------------------------------------

#: How long after its last word a helper is still considered online. Generous:
#: an ESP32 on battery may only wake once a minute.
ONLINE_GRACE_SECONDS = 120.0

# -- jobs --------------------------------------------------------------------

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

JOB_STATES = (JOB_QUEUED, JOB_RUNNING, JOB_DONE, JOB_FAILED, JOB_CANCELLED)
JOB_OPEN_STATES = (JOB_QUEUED, JOB_RUNNING)

#: A job whose helper vanished mid-run goes back to the queue after this long.
JOB_STALE_SECONDS = 900.0
#: Give up after this many tries. A job that fails five times is not unlucky.
JOB_MAX_ATTEMPTS = 5


# --------------------------------------------------------------------------- helpers (util)


def _json_load(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        log.debug("stored JSON was unreadable, using the fallback", exc_info=True)
        return fallback


def hash_token(token: str) -> str:
    """SHA-256 of a token.

    Not PBKDF2 on purpose: unlike a password this is 32 random bytes we
    generated ourselves, so there is nothing to brute force and no reason to
    make every heartbeat pay for 200k iterations.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def clean_capabilities(kind: str, values: Optional[Iterable[str]]) -> List[str]:
    """Keep the capabilities that exist and that this kind of machine can have."""
    allowed = KIND_CAPABILITIES.get(kind, set(CAPABILITIES))
    out = []  # type: List[str]
    for value in values or []:
        name = str(value or "").strip().lower()
        if name in allowed and name not in out:
            out.append(name)
    return out


def clean_kind(value: Optional[str]) -> str:
    name = str(value or "").strip().lower()
    return name if name in KINDS else KIND_OTHER


def _iso(moment: Any) -> str:
    """Format a datetime exactly the way :func:`utcnow_iso` does.

    Same shape everywhere is not cosmetic: expiry is decided by comparing these
    strings, and ``...:26Z`` versus ``...:26.480Z`` sorts by punctuation.
    """
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: Optional[str]) -> Optional[Any]:
    from datetime import datetime, timezone

    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _seconds_between(later: Optional[str], earlier: Optional[str]) -> Optional[float]:
    """Difference between two stored ISO timestamps, in seconds."""
    a = _parse_iso(later)
    b = _parse_iso(earlier)
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


def _shape(row: Any) -> Optional[Dict[str, Any]]:
    item = row_to_dict(row)
    if item is None:
        return None
    item.pop("token_hash", None)
    item["capabilities"] = _json_load(item.get("capabilities"), [])
    item["facts"] = _json_load(item.get("facts"), {})
    item["enabled"] = bool(item.get("enabled", 1))
    age = _seconds_between(utcnow_iso(), item.get("last_seen"))
    item["seconds_since_seen"] = age
    item["online"] = bool(item["enabled"] and age is not None and age <= ONLINE_GRACE_SECONDS)
    item["labels"] = [CAPABILITY_LABELS.get(c, c) for c in item["capabilities"]]
    return item


def _shape_job(row: Any) -> Optional[Dict[str, Any]]:
    item = row_to_dict(row)
    if item is None:
        return None
    item["payload"] = _json_load(item.get("payload"), {})
    item["needs"] = _json_load(item.get("needs"), [])
    item["result"] = _json_load(item.get("result"), None)
    item["attempts"] = int(item.get("attempts") or 0)
    return item


# --------------------------------------------------------------------------- pairing


def new_code(db: Any, kind: Optional[str] = None, ttl: float = CODE_TTL_SECONDS) -> Dict[str, Any]:
    """Mint a pairing code for someone to type into the agent."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expires = _iso(now + timedelta(seconds=float(ttl)))
    created = _iso(now)
    wanted = clean_kind(kind) if kind else None

    for _ in range(CODE_ATTEMPTS):
        code = "".join(str(secrets.randbelow(10)) for _ in range(CODE_DIGITS))
        existing = db.one("SELECT code FROM helper_codes WHERE code = ?", (code,))
        if existing is not None:
            continue
        db.execute(
            "INSERT INTO helper_codes (code, kind, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (code, wanted, created, expires),
        )
        return {"code": code, "kind": wanted, "created_at": created,
                "expires_at": expires, "expires_in": float(ttl)}
    raise RuntimeError("could not generate a free pairing code")


def purge_codes(db: Any) -> int:
    """Drop expired and used codes. Cheap enough to call on every listing."""
    cursor = db.execute(
        "DELETE FROM helper_codes WHERE used_at IS NOT NULL OR expires_at < ?",
        (utcnow_iso(),),
    )
    return int(cursor.rowcount or 0)


def open_codes(db: Any) -> List[Dict[str, Any]]:
    purge_codes(db)
    rows = db.query("SELECT * FROM helper_codes ORDER BY created_at DESC")
    return rows_to_dicts(rows)


def revoke_code(db: Any, code: str) -> bool:
    cursor = db.execute("DELETE FROM helper_codes WHERE code = ?", (str(code or ""),))
    return bool(cursor.rowcount)


class PairingError(Exception):
    """The code was wrong, expired, already used, or for another kind of machine."""

    def __init__(self, reason: str, message: str) -> None:
        Exception.__init__(self, message)
        self.reason = reason
        self.message = message


def pair(
    db: Any,
    code: str,
    name: str = "",
    kind: str = KIND_OTHER,
    platform: str = "",
    capabilities: Optional[Iterable[str]] = None,
    facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Trade a valid pairing code for a helper identity and its token.

    The token is returned exactly once -- only its hash is stored. An agent that
    loses it pairs again.
    """
    code = str(code or "").strip()
    row = db.one("SELECT * FROM helper_codes WHERE code = ?", (code,))
    if row is None:
        raise PairingError("invalid_code", "esse codigo nao existe")
    entry = row_to_dict(row) or {}
    if entry.get("used_at"):
        raise PairingError("code_used", "esse codigo ja foi usado")
    expiry = _seconds_between(entry.get("expires_at"), utcnow_iso())
    if expiry is None or expiry <= 0:
        raise PairingError("code_expired", "esse codigo expirou, gere outro")

    kind = clean_kind(kind)
    wanted = entry.get("kind")
    if wanted and wanted != kind:
        raise PairingError(
            "kind_mismatch",
            "esse codigo foi gerado para um ajudante do tipo %s" % wanted,
        )

    helper_id = uuid.uuid4().hex[:16]
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = utcnow_iso()
    clean = clean_capabilities(kind, capabilities)
    label = str(name or "").strip() or ("ajudante %s" % helper_id[:6])

    with db.transaction():
        db.execute(
            """INSERT INTO helpers
                 (id, kind, name, platform, token_hash, capabilities, facts,
                  paired_at, last_seen, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (helper_id, kind, label, str(platform or ""), hash_token(token),
             json.dumps(clean), json.dumps(facts or {}), now, now),
        )
        db.execute(
            "UPDATE helper_codes SET used_at = ?, helper_id = ? WHERE code = ?",
            (now, helper_id, code),
        )

    log.info("helper %s (%s) paired: %s", label, kind, ", ".join(clean) or "nothing declared")
    helper = get(db, helper_id)
    return {"helper": helper, "token": token}


# --------------------------------------------------------------------------- registry


def get(db: Any, helper_id: str) -> Optional[Dict[str, Any]]:
    return _shape(db.one("SELECT * FROM helpers WHERE id = ?", (str(helper_id or ""),)))


def authenticate(db: Any, token: str) -> Optional[Dict[str, Any]]:
    """Find the helper holding this token, or None. Disabled helpers do not pass."""
    if not token:
        return None
    row = db.one("SELECT * FROM helpers WHERE token_hash = ?", (hash_token(token),))
    helper = _shape(row)
    if helper is None or not helper.get("enabled"):
        return None
    return helper


def helpers(db: Any, kind: Optional[str] = None, only_online: bool = False) -> List[Dict[str, Any]]:
    if kind:
        rows = db.query("SELECT * FROM helpers WHERE kind = ? ORDER BY paired_at", (kind,))
    else:
        rows = db.query("SELECT * FROM helpers ORDER BY paired_at")
    out = [item for item in (_shape(row) for row in rows) if item is not None]
    if only_online:
        out = [item for item in out if item["online"]]
    return out


def touch(
    db: Any,
    helper_id: str,
    facts: Optional[Dict[str, Any]] = None,
    capabilities: Optional[Iterable[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Heartbeat. Facts and capabilities may change between beats -- a laptop
    that just had ffmpeg installed should not have to pair again to say so."""
    helper = get(db, helper_id)
    if helper is None:
        return None
    fields = ["last_seen = ?"]
    params = [utcnow_iso()]  # type: List[Any]
    if facts is not None:
        fields.append("facts = ?")
        params.append(json.dumps(facts))
    if capabilities is not None:
        fields.append("capabilities = ?")
        params.append(json.dumps(clean_capabilities(helper["kind"], capabilities)))
    params.append(helper_id)
    db.execute("UPDATE helpers SET %s WHERE id = ?" % ", ".join(fields), params)
    return get(db, helper_id)


def rename(db: Any, helper_id: str, name: str) -> Optional[Dict[str, Any]]:
    db.execute("UPDATE helpers SET name = ? WHERE id = ?", (str(name or "").strip(), helper_id))
    return get(db, helper_id)


def set_enabled(db: Any, helper_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
    db.execute("UPDATE helpers SET enabled = ? WHERE id = ?", (1 if enabled else 0, helper_id))
    if not enabled:
        release_jobs(db, helper_id)
    return get(db, helper_id)


def forget(db: Any, helper_id: str) -> bool:
    """Remove a helper. Its running jobs go back to the queue rather than
    disappearing with it -- the work was still wanted."""
    helper = get(db, helper_id)
    if helper is None:
        return False
    with db.transaction():
        release_jobs(db, helper_id)
        db.execute("UPDATE helper_jobs SET helper_id = NULL WHERE helper_id = ?", (helper_id,))
        db.execute("DELETE FROM helpers WHERE id = ?", (helper_id,))
    log.info("helper %s forgotten", helper.get("name") or helper_id)
    return True


def capabilities_available(db: Any) -> List[str]:
    """Every capability at least one online helper is offering right now."""
    out = []  # type: List[str]
    for helper in helpers(db, only_online=True):
        for capability in helper["capabilities"]:
            if capability not in out:
                out.append(capability)
    return out


# --------------------------------------------------------------------------- jobs


def submit(
    db: Any,
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
    needs: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Queue a task for whichever helper can do it.

    Queuing never fails for lack of a helper: the job waits. A PC that boots an
    hour later picks up what the Pi could not do at 3am.
    """
    job_id = uuid.uuid4().hex[:16]
    wanted = [str(n).strip().lower() for n in (needs or []) if str(n).strip()]
    db.execute(
        """INSERT INTO helper_jobs (id, kind, payload, needs, state, created_at, attempts)
           VALUES (?, ?, ?, ?, ?, ?, 0)""",
        (job_id, str(kind), json.dumps(payload or {}), json.dumps(wanted),
         JOB_QUEUED, utcnow_iso()),
    )
    return get_job(db, job_id) or {}


def get_job(db: Any, job_id: str) -> Optional[Dict[str, Any]]:
    return _shape_job(db.one("SELECT * FROM helper_jobs WHERE id = ?", (str(job_id or ""),)))


def jobs(db: Any, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    if state:
        rows = db.query(
            "SELECT * FROM helper_jobs WHERE state = ? ORDER BY created_at DESC LIMIT ?",
            (state, int(limit)),
        )
    else:
        rows = db.query(
            "SELECT * FROM helper_jobs ORDER BY created_at DESC LIMIT ?", (int(limit),)
        )
    return [item for item in (_shape_job(row) for row in rows) if item is not None]


def jobs_of(db: Any, helper_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """The recent history of one helper -- what the detail screen shows."""
    rows = db.query(
        "SELECT * FROM helper_jobs WHERE helper_id = ? ORDER BY created_at DESC LIMIT ?",
        (str(helper_id or ""), int(limit)),
    )
    return [item for item in (_shape_job(row) for row in rows) if item is not None]


def take(db: Any, helper: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Hand this helper the oldest queued job it is able to run.

    The pick and the claim happen inside one transaction so two helpers asking
    at the same moment cannot walk away with the same job. Matching is done in
    Python rather than SQL because ``needs`` is a JSON list; the queue is short
    enough that this is not worth a join table.
    """
    if not helper or not helper.get("enabled"):
        return None
    mine = set(helper.get("capabilities") or [])
    with db.transaction():
        rows = db.query(
            "SELECT * FROM helper_jobs WHERE state = ? ORDER BY created_at", (JOB_QUEUED,)
        )
        for row in rows:
            job = _shape_job(row)
            if job is None or not set(job["needs"]).issubset(mine):
                continue
            db.execute(
                """UPDATE helper_jobs
                     SET state = ?, helper_id = ?, started_at = ?, attempts = attempts + 1
                   WHERE id = ? AND state = ?""",
                (JOB_RUNNING, helper["id"], utcnow_iso(), job["id"], JOB_QUEUED),
            )
            return get_job(db, job["id"])
    return None


def finish(db: Any, job_id: str, result: Any = None) -> Optional[Dict[str, Any]]:
    db.execute(
        "UPDATE helper_jobs SET state = ?, result = ?, error = NULL, finished_at = ? WHERE id = ?",
        (JOB_DONE, json.dumps(result), utcnow_iso(), job_id),
    )
    return get_job(db, job_id)


def fail(db: Any, job_id: str, error: str = "") -> Optional[Dict[str, Any]]:
    """Record a failure -- back to the queue if it is still worth retrying.

    A job that failed because the helper lost power deserves another go; one
    that fails five times is broken, and retrying forever only hides that.
    """
    job = get_job(db, job_id)
    if job is None:
        return None
    if job["attempts"] < JOB_MAX_ATTEMPTS:
        db.execute(
            """UPDATE helper_jobs SET state = ?, error = ?, helper_id = NULL, started_at = NULL
               WHERE id = ?""",
            (JOB_QUEUED, str(error or ""), job_id),
        )
    else:
        db.execute(
            "UPDATE helper_jobs SET state = ?, error = ?, finished_at = ? WHERE id = ?",
            (JOB_FAILED, str(error or ""), utcnow_iso(), job_id),
        )
    return get_job(db, job_id)


def cancel(db: Any, job_id: str) -> Optional[Dict[str, Any]]:
    job = get_job(db, job_id)
    if job is None or job["state"] not in JOB_OPEN_STATES:
        return job
    db.execute(
        "UPDATE helper_jobs SET state = ?, finished_at = ? WHERE id = ?",
        (JOB_CANCELLED, utcnow_iso(), job_id),
    )
    return get_job(db, job_id)


def release_jobs(db: Any, helper_id: str) -> int:
    """Put a helper's running jobs back in the queue. Called when it goes away."""
    cursor = db.execute(
        """UPDATE helper_jobs SET state = ?, helper_id = NULL, started_at = NULL
           WHERE helper_id = ? AND state = ?""",
        (JOB_QUEUED, helper_id, JOB_RUNNING),
    )
    return int(cursor.rowcount or 0)


def requeue_stale(db: Any, older_than: float = JOB_STALE_SECONDS) -> int:
    """Rescue jobs whose helper stopped answering without saying goodbye.

    A crashed agent never sends a failure, so without this the job would sit in
    ``running`` forever looking like progress.
    """
    now = utcnow_iso()
    freed = 0
    for row in db.query("SELECT * FROM helper_jobs WHERE state = ?", (JOB_RUNNING,)):
        job = _shape_job(row)
        if job is None:
            continue
        age = _seconds_between(now, job.get("started_at"))
        if age is None or age < float(older_than):
            continue
        if job["attempts"] >= JOB_MAX_ATTEMPTS:
            db.execute(
                "UPDATE helper_jobs SET state = ?, error = ?, finished_at = ? WHERE id = ?",
                (JOB_FAILED, "o ajudante parou de responder", now, job["id"]),
            )
            continue
        db.execute(
            """UPDATE helper_jobs SET state = ?, helper_id = NULL, started_at = NULL,
                                      error = ? WHERE id = ?""",
            (JOB_QUEUED, "o ajudante parou de responder", job["id"]),
        )
        freed += 1
    if freed:
        log.info("%d job(s) went back to the queue: the helper stopped answering", freed)
    return freed


def can_run(db: Any, needs: Optional[Iterable[str]] = None) -> bool:
    """Is there an online helper able to take a job with these needs?

    Used by the UI to offer offloading only when it would actually happen,
    instead of queueing work into a void.
    """
    wanted = set(str(n).strip().lower() for n in (needs or []) if str(n).strip())
    for helper in helpers(db, only_online=True):
        if wanted.issubset(set(helper["capabilities"])):
            return True
    return False


def stats(db: Any) -> Dict[str, Any]:
    all_helpers = helpers(db)
    online = [item for item in all_helpers if item["online"]]
    counts = {}  # type: Dict[str, int]
    for state in JOB_STATES:
        counts[state] = int(
            db.scalar("SELECT COUNT(*) FROM helper_jobs WHERE state = ?", (state,), 0) or 0
        )
    by_kind = {}  # type: Dict[str, int]
    for helper in all_helpers:
        by_kind[helper["kind"]] = by_kind.get(helper["kind"], 0) + 1
    return {
        "helpers": len(all_helpers),
        "online": len(online),
        "by_kind": by_kind,
        "capabilities": capabilities_available(db),
        "jobs": counts,
        "queued": counts.get(JOB_QUEUED, 0),
        "running": counts.get(JOB_RUNNING, 0),
    }


__all__ = [
    "CAPABILITIES", "CAPABILITY_LABELS", "KINDS", "KIND_CAPABILITIES",
    "KIND_PC", "KIND_PI", "KIND_ESP32", "KIND_OTHER",
    "JOB_QUEUED", "JOB_RUNNING", "JOB_DONE", "JOB_FAILED", "JOB_CANCELLED",
    "PairingError", "authenticate", "can_run", "cancel", "capabilities_available",
    "clean_capabilities", "clean_kind", "fail", "finish", "forget", "get", "get_job",
    "hash_token", "helpers", "jobs", "jobs_of", "new_code", "open_codes", "pair", "purge_codes",
    "release_jobs", "rename", "requeue_stale", "revoke_code", "set_enabled", "stats",
    "submit", "take", "touch",
]
