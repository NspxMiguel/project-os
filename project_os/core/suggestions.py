"""Turning "I found a device" into "here is what you can do with it".

*"legal tbm autodetectar dispositivos e dar sujestoes doq da pra fazer"* -- the
second half of that sentence is this module. Discovery produces a list of
things; on its own that is a network scanner, not a system that helps.

A rule is a function that sees the whole picture (devices, board, apps, config)
and returns zero or more cards. Rules are deliberately dumb and independent: no
rule may raise, no rule may depend on another having run, and a rule that has
nothing to say returns nothing rather than a card saying "all good".

Three properties keep this from becoming noise:

* **Dismissal is permanent** -- a dismissed card stays dismissed, recorded in
  ``suggestion_state``. Nothing is more irritating than advice that comes back.
* **A card that has become untrue disappears on its own**, because rules are
  re-evaluated from live state rather than stored.
* **Cards say what they cost.** "Install Home Assistant" on a 1 GB board is a
  different proposition than on a Pi 5, and the card says so instead of finding
  out for the user afterwards.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from project_os.core import hardware
from project_os.db import utcnow_iso

log = logging.getLogger(__name__)

#: Ordering only. Priority is a hint to the UI, never a filter.
PRIORITY_CRITICAL = 100
PRIORITY_HIGH = 50
PRIORITY_NORMAL = 20
PRIORITY_LOW = 5


def card(
    suggestion_id: str,
    title: str,
    body: str,
    icon: str = "bulb",
    priority: int = PRIORITY_NORMAL,
    tags: Optional[List[str]] = None,
    action: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "id": suggestion_id,
        "icon": icon,
        "title": title,
        "body": body,
        "priority": int(priority),
        "tags": list(tags or []),
        "action": action or {},
    }


class Context(object):
    """Everything a rule is allowed to look at, gathered once per evaluation."""

    def __init__(self, config: Any, db: Any, devices: Any, plugins: Any = None) -> None:
        self.config = config
        self.db = db
        self.registry = devices
        self.plugins = plugins
        self.devices = []  # type: List[Dict[str, Any]]
        if devices is not None:
            try:
                self.devices = devices.devices()
            except Exception:  # pragma: no cover
                log.debug("could not list devices for suggestions", exc_info=True)
        self.board = hardware.detect()

    # -- device helpers --------------------------------------------------
    def of_kind(self, *kinds: str) -> List[Dict[str, Any]]:
        return [d for d in self.devices if d.get("kind") in kinds]

    def with_capability(self, capability: str) -> List[Dict[str, Any]]:
        return [d for d in self.devices if capability in (d.get("capabilities") or [])]

    def from_source(self, source: str) -> List[Dict[str, Any]]:
        return [d for d in self.devices if (d.get("properties") or {}).get("vendor") == source]

    # -- config helpers --------------------------------------------------
    def setting(self, path: str, default: Any = None) -> Any:
        if self.config is None:
            return default
        try:
            return self.config.get(path, default)
        except Exception:  # pragma: no cover
            return default

    def app_enabled(self, app_id: str) -> bool:
        enabled = self.setting("apps.enabled", []) or []
        return app_id in enabled

    def app_installed(self, app_id: str) -> bool:
        if self.plugins is None:
            return self.app_enabled(app_id)
        try:
            return app_id in self.plugins
        except Exception:  # pragma: no cover
            return False


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
def rule_birdtunes_output(ctx: Context) -> List[Dict[str, Any]]:
    """Speakers exist but BirdTunes has nowhere to play."""
    targets = ctx.with_capability("airplay") + ctx.with_capability("cast")
    if not targets:
        return []
    configured = ctx.setting("apps.settings.birdtunes.output.device_id")
    if configured:
        return []
    names = ", ".join(sorted(set(d["display_name"] for d in targets))[:3])
    return [
        card(
            "birdtunes-output",
            "Escolher onde o BirdTunes toca",
            "Achei %d saída(s) de áudio na rede (%s). O BirdTunes ainda não tem uma "
            "escolhida, então não vai tocar em lugar nenhum." % (len(targets), names),
            icon="bird",
            priority=PRIORITY_HIGH,
            tags=["birdtunes", "áudio"],
            action={"type": "configure", "app": "birdtunes", "field": "output"},
        )
    ]


def rule_birdtunes_schedule(ctx: Context) -> List[Dict[str, Any]]:
    """Output is set but nothing tells it when to play."""
    if not ctx.setting("apps.settings.birdtunes.output.device_id"):
        return []
    if ctx.setting("apps.settings.birdtunes.schedule.windows"):
        return []
    return [
        card(
            "birdtunes-schedule",
            "Marcar os horários do BirdTunes",
            "A saída já está escolhida. Falta dizer quando tocar — por exemplo, das "
            "09:00 às 17:00, quando você não está em casa.",
            icon="clock",
            priority=PRIORITY_HIGH,
            tags=["birdtunes", "agenda"],
            action={"type": "configure", "app": "birdtunes", "field": "schedule"},
        )
    ]


def rule_extra_speakers(ctx: Context) -> List[Dict[str, Any]]:
    """More speakers than the one configured."""
    chosen = ctx.setting("apps.settings.birdtunes.output.device_id")
    if not chosen:
        return []
    targets = ctx.with_capability("airplay") + ctx.with_capability("cast")
    others = [d for d in targets if d["id"] != chosen]
    if len(others) < 1:
        return []
    return [
        card(
            "birdtunes-multiroom",
            "Tocar em mais de um cômodo",
            "Além da saída escolhida, achei %s. Dá pra tocar em vários ao mesmo tempo."
            % ", ".join(sorted(d["display_name"] for d in others)[:3]),
            icon="speaker",
            priority=PRIORITY_LOW,
            tags=["birdtunes", "áudio"],
            action={"type": "configure", "app": "birdtunes", "field": "output"},
        )
    ]


def rule_tuya_local_keys(ctx: Context) -> List[Dict[str, Any]]:
    """Tuya devices are visible but unusable without their local keys."""
    tuya = [d for d in ctx.devices if (d.get("properties") or {}).get("needs_local_key")]
    if not tuya:
        return []
    return [
        card(
            "tuya-local-keys",
            "%d dispositivo(s) Tuya sem chave local" % len(tuya),
            "Eles aparecem na rede, mas controlar sem passar pela nuvem exige a chave "
            "local de cada um. É uma configuração de uma vez só (~10 min) e depois "
            "funciona até com a internet caída.",
            icon="key",
            priority=PRIORITY_HIGH,
            tags=["tuya", "casa"],
            action={"type": "open", "href": "#/home/providers/tuya"},
        )
    ]


def rule_kasa_ready(ctx: Context) -> List[Dict[str, Any]]:
    """Kasa devices need no credentials at all -- one click and they work."""
    kasa = [d for d in ctx.devices if (d.get("properties") or {}).get("vendor") == "TP-Link Kasa"]
    if not kasa or ctx.app_enabled("kasa"):
        return []
    return [
        card(
            "kasa-enable",
            "Ligar os %d dispositivo(s) Kasa" % len(kasa),
            "Kasa antigo não pede conta nem nuvem: já dá pra ligar e desligar daqui. "
            "Custa cerca de 8 MB de memória.",
            icon="plug",
            priority=PRIORITY_HIGH,
            tags=["kasa", "casa"],
            action={"type": "enable_app", "app": "kasa"},
        )
    ]


def rule_energy_monitoring(ctx: Context) -> List[Dict[str, Any]]:
    energy = ctx.with_capability("energy")
    if not energy:
        return []
    return [
        card(
            "energy-history",
            "Guardar o consumo dessas tomadas",
            "%s mede energia. Dá pra acompanhar o gasto por dia sem instalar mais nada."
            % ", ".join(sorted(d["display_name"] for d in energy)[:3]),
            icon="pulse",
            priority=PRIORITY_LOW,
            tags=["energia", "casa"],
            action={"type": "open", "href": "#/home/energy"},
        )
    ]


def rule_home_assistant_on_network(ctx: Context) -> List[Dict[str, Any]]:
    """There is already an HA out there -- linking beats installing a second one."""
    existing = ctx.of_kind("home_assistant")
    if not existing:
        return []
    if ctx.setting("integrations.home_assistant.url"):
        return []
    host = existing[0]["address"]
    saved = "" if ctx.board.tier == "roomy" else " Isso poupa ~450 MB desta placa."
    return [
        card(
            "ha-link-existing",
            "Já existe um Home Assistant em %s" % host,
            "Dá pra conectar nele com um token, em vez de instalar outro aqui.%s" % saved,
            icon="home",
            priority=PRIORITY_HIGH,
            tags=["home-assistant"],
            action={"type": "open", "href": "#/settings/integrations"},
        )
    ]


def rule_home_assistant_fit(ctx: Context) -> List[Dict[str, Any]]:
    """Say what HA costs *before* a 25-minute install on a small board."""
    if ctx.app_installed("home-assistant") or ctx.of_kind("home_assistant"):
        return []
    if ctx.board.tier != "minimal":
        return []
    return [
        card(
            "ha-tight-fit",
            "Home Assistant vai ficar apertado aqui",
            "Esta placa tem %d MB. O HA sozinho usa ~450 MB e leva de 10 a 25 minutos "
            "pra instalar. Funciona, mas não sobra muito — os apps nativos de Kasa e "
            "Tuya fazem o básico com ~15 MB." % ctx.board.ram_mb,
            icon="warning",
            priority=PRIORITY_NORMAL,
            tags=["home-assistant", "memória"],
            action={"type": "open", "href": "#/store/home-assistant"},
        )
    ]


def rule_esp32_on_usb(ctx: Context) -> List[Dict[str, Any]]:
    """A microcontroller is plugged in. See docs/SIMPLE-PROJECT-OS.md section 8."""
    boards = [
        d for d in ctx.devices
        if d.get("kind") == "microcontroller"
        and (d.get("properties") or {}).get("flashable") == "true"
    ]
    if not boards:
        return []
    first = boards[0]
    return [
        card(
            "esp32-flash",
            "Achei um %s na USB" % first["display_name"],
            "Dá pra gravar o Simpleproject-os nele: pinos, sensores e horários, "
            "controlados daqui ou sozinho. Sem abrir terminal.",
            icon="plug",
            priority=PRIORITY_HIGH,
            tags=["esp32", "simpleproject_os"],
            action={"type": "open", "href": "#/devices/%s/flash" % first["id"]},
        )
    ]


def rule_adopt_node(ctx: Context) -> List[Dict[str, Any]]:
    """A Simpleproject-os node is announcing itself on the LAN."""
    nodes = ctx.of_kind("project_os_node")
    if not nodes:
        return []
    return [
        card(
            "adopt-nodes",
            "%d nó Simpleproject-os na rede" % len(nodes),
            "%s se anunciou por mDNS. Adotando, os pinos e sensores dele aparecem "
            "aqui junto com o resto da casa — e ele continua funcionando sozinho se "
            "esta placa cair." % ", ".join(sorted(d["display_name"] for d in nodes)[:3]),
            icon="broadcast",
            priority=PRIORITY_HIGH,
            tags=["esp32", "simpleproject_os"],
            action={"type": "open", "href": "#/home/nodes"},
        )
    ]


def rule_esphome_devices(ctx: Context) -> List[Dict[str, Any]]:
    devices = ctx.of_kind("esphome")
    if not devices:
        return []
    return [
        card(
            "esphome-adopt",
            "%d dispositivo(s) ESPHome na rede" % len(devices),
            "Eles falam a API nativa do ESPHome. O app ESPHome traz eles pra cá "
            "sem passar por nuvem nenhuma.",
            icon="plug",
            priority=PRIORITY_NORMAL,
            tags=["esphome", "casa"],
            action={"type": "open", "href": "#/store/esphome"},
        )
    ]


def rule_mqtt_broker(ctx: Context) -> List[Dict[str, Any]]:
    brokers = ctx.of_kind("mqtt_broker")
    if not brokers or ctx.app_enabled("mqtt"):
        return []
    return [
        card(
            "mqtt-connect",
            "Tem um broker MQTT em %s" % brokers[0]["address"],
            "Conectando nele, tudo que fala MQTT Discovery (Zigbee2MQTT, Tasmota, "
            "ESPHome) aparece aqui automaticamente.",
            icon="broadcast",
            priority=PRIORITY_NORMAL,
            tags=["mqtt", "casa"],
            action={"type": "enable_app", "app": "mqtt"},
        )
    ]


def rule_undervoltage(ctx: Context) -> List[Dict[str, Any]]:
    """The single most common cause of a Pi misbehaving."""
    from project_os.core import sysinfo

    try:
        flags = sysinfo.throttled()
    except Exception:  # pragma: no cover
        return []
    if not flags.get("available"):
        return []
    if not (flags.get("undervoltage_now") or flags.get("undervoltage_since_boot")):
        return []
    now = flags.get("undervoltage_now")
    return [
        card(
            "undervoltage",
            "A fonte não está dando conta",
            "O Pi reportou subtensão %s. Isso corrompe cartão SD e faz o sistema "
            "travar de formas que parecem bug de software. Vale trocar a fonte por "
            "uma oficial." % ("agora" if now else "desde que ligou"),
            icon="pulse",
            priority=PRIORITY_CRITICAL,
            tags=["hardware", "energia"],
            action={"type": "open", "href": "#/system"},
        )
    ]


def rule_disk_space(ctx: Context) -> List[Dict[str, Any]]:
    from project_os.core import sysinfo

    try:
        mounts = sysinfo.disks()
    except Exception:  # pragma: no cover
        return []
    tight = [m for m in mounts if m["mountpoint"] == "/" and m["percent"] >= 85]
    if not tight:
        return []
    root = tight[0]
    return [
        card(
            "disk-space",
            "O cartão está %.0f%% cheio" % root["percent"],
            "Restam %.1f GB. Instalar apps grandes (Home Assistant, Frigate) vai "
            "falhar antes de terminar." % (root["free"] / (1024.0 ** 3)),
            icon="disk",
            priority=PRIORITY_CRITICAL,
            tags=["disco"],
            action={"type": "open", "href": "#/system"},
        )
    ]


def rule_no_zeroconf(ctx: Context) -> List[Dict[str, Any]]:
    """Without zeroconf, half the house is invisible and nothing says why."""
    if ctx.registry is None:
        return []
    try:
        if ctx.registry.summary().get("zeroconf"):
            return []
    except Exception:  # pragma: no cover
        return []
    return [
        card(
            "install-zeroconf",
            "Falta o zeroconf pra achar Apple TV e Chromecast",
            "Sem ele a busca por mDNS não roda, então Apple TV, HomePod, Chromecast "
            "e ESPHome não aparecem. Kasa e Tuya continuam funcionando.",
            icon="search",
            priority=PRIORITY_HIGH,
            tags=["descoberta"],
            action={"type": "open", "href": "#/system"},
        )
    ]


def rule_unsupported_board(ctx: Context) -> List[Dict[str, Any]]:
    if ctx.board.supported:
        return []
    return [
        card(
            "board-too-small",
            "Esta placa está abaixo do mínimo",
            ctx.board.reason,
            icon="warning",
            priority=PRIORITY_CRITICAL,
            tags=["hardware"],
            action={"type": "open", "href": "#/system"},
        )
    ]


def rule_auth_disabled(ctx: Context) -> List[Dict[str, Any]]:
    if ctx.setting("security.auth_enabled", True):
        return []
    return [
        card(
            "auth-disabled",
            "O project-os está sem senha",
            "Qualquer um na sua rede abre isto e tem o sistema inteiro. Faz sentido "
            "enquanto você configura; não faz depois.",
            icon="lock",
            priority=PRIORITY_CRITICAL,
            tags=["segurança"],
            action={"type": "open", "href": "#/settings/security"},
        )
    ]


#: Evaluated in order; the order does not matter, priority decides the display.
RULES = (
    rule_unsupported_board,
    rule_undervoltage,
    rule_disk_space,
    rule_auth_disabled,
    rule_no_zeroconf,
    rule_birdtunes_output,
    rule_birdtunes_schedule,
    rule_kasa_ready,
    rule_tuya_local_keys,
    rule_esp32_on_usb,
    rule_adopt_node,
    rule_home_assistant_on_network,
    rule_home_assistant_fit,
    rule_esphome_devices,
    rule_mqtt_broker,
    rule_energy_monitoring,
    rule_extra_speakers,
)  # type: tuple


class SuggestionEngine(object):
    """Runs the rules and remembers what the user dismissed."""

    def __init__(
        self,
        config: Any = None,
        db: Any = None,
        bus: Any = None,
        devices: Any = None,
        plugins: Any = None,
        rules: Optional[Iterable[Any]] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.bus = bus
        self.devices = devices
        self.plugins = plugins
        self.rules = tuple(rules) if rules is not None else RULES

    # -- evaluation ------------------------------------------------------
    def evaluate(self, include_dismissed: bool = False) -> List[Dict[str, Any]]:
        ctx = Context(self.config, self.db, self.devices, self.plugins)
        state = self._state()
        out = []  # type: List[Dict[str, Any]]
        for rule in self.rules:
            try:
                produced = rule(ctx) or []
            except Exception:
                # A broken rule costs its own card, never the whole list.
                log.warning("suggestion rule %s failed", getattr(rule, "__name__", rule),
                            exc_info=True)
                continue
            for item in produced:
                record = state.get(item["id"], {})
                if record.get("dismissed_at") and not include_dismissed:
                    continue
                item["dismissed_at"] = record.get("dismissed_at")
                item["applied_at"] = record.get("applied_at")
                out.append(item)
        out.sort(key=lambda item: (-item["priority"], item["title"]))
        return out

    #: ``evaluate`` under the name the API uses.
    def list(self, include_dismissed: bool = False) -> List[Dict[str, Any]]:
        return self.evaluate(include_dismissed=include_dismissed)

    def get(self, suggestion_id: str) -> Optional[Dict[str, Any]]:
        for item in self.evaluate(include_dismissed=True):
            if item["id"] == suggestion_id:
                return item
        return None

    # -- state -----------------------------------------------------------
    def _state(self) -> Dict[str, Dict[str, Any]]:
        if self.db is None:
            return {}
        try:
            rows = self.db.query("SELECT * FROM suggestion_state")
        except Exception:  # pragma: no cover
            return {}
        return dict((row["id"], dict(row)) for row in rows)

    def _mark(self, suggestion_id: str, column: str) -> None:
        if self.db is None:
            return
        self.db.execute(
            "INSERT INTO suggestion_state (id, %s) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET %s = excluded.%s" % (column, column, column),
            (suggestion_id, utcnow_iso()),
        )

    def dismiss(self, suggestion_id: str) -> Dict[str, Any]:
        self._mark(suggestion_id, "dismissed_at")
        self._emit("suggestion.dismissed", {"id": suggestion_id})
        return {"id": suggestion_id, "dismissed": True}

    def restore(self, suggestion_id: str) -> Dict[str, Any]:
        if self.db is not None:
            self.db.execute(
                "UPDATE suggestion_state SET dismissed_at = NULL WHERE id = ?",
                (suggestion_id,),
            )
        return {"id": suggestion_id, "dismissed": False}

    def mark_applied(self, suggestion_id: str) -> Dict[str, Any]:
        self._mark(suggestion_id, "applied_at")
        self._emit("suggestion.applied", {"id": suggestion_id})
        return {"id": suggestion_id, "applied": True}

    def _emit(self, topic: str, data: Dict[str, Any]) -> None:
        if self.bus is not None:
            try:
                self.bus.publish_nowait(topic, data)
            except Exception:  # pragma: no cover
                pass


__all__ = [
    "Context",
    "PRIORITY_CRITICAL",
    "PRIORITY_HIGH",
    "PRIORITY_LOW",
    "PRIORITY_NORMAL",
    "RULES",
    "SuggestionEngine",
    "card",
]
