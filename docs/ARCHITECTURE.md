# project-os — Architecture Contract

> **This document is the build contract.** Every module must conform to it exactly.
> Signatures, config keys, table names, event topics and HTTP paths below are
> normative. If something is missing, follow the nearest established pattern —
> do not invent a competing convention.

## 0. What project-os is

project-os is **the system layer of a Raspberry Pi**, not a container and not an app.
You flash Raspberry Pi OS Lite, run the installer, and the Pi becomes a headless
appliance you drive entirely from a browser on another machine — the same shape
Home Assistant OS has, but general purpose: it hosts *your projects*.

- It runs natively (systemd + venv), owns its own services, and can install,
  supervise and expose other software (including Home Assistant) as **apps**.
- Two UI modes: **Simple** (HA-style dashboard, cards, app store) and
  **Advanced** (full machine: files, terminal, services, logs, raw config).
- The first bundled app is **BirdTunes**, a music player for pet birds that casts
  to AirPlay (Apple TV / HomePod) or Chromecast.

Target hardware: **Raspberry Pi 3B, 1 GB RAM, ARM**. Every decision is subordinate
to that: no bundler, no node runtime, no container runtime required, lazy imports,
idle RSS budget ≈ 90 MB.

---

## 1. Hard constraints

| Constraint | Rule |
|---|---|
| Python | **3.9+ compatible.** Every module starts with `from __future__ import annotations`. No `match`, no runtime `X \| Y`, no `list[str]` outside annotations. |
| Required deps | `fastapi`, `uvicorn`, `pyyaml` only. |
| Optional deps | `psutil`, `pyatv`, `pychromecast`, `zeroconf`, `httpx`. **Never** import these at module top level — import inside the function and degrade gracefully. |
| Frontend | **Zero build step.** Plain ES modules + CSS. No npm, no bundler, no CDN (must work offline on a LAN). |
| Hardcoding | Nothing user-specific in source. No IPs, tokens, device names, paths, or media. All of it is config with sane defaults. |
| Blocking I/O | Never block the event loop. Wrap sync libs (`pychromecast`, `psutil` disk walks, file ops) in `await asyncio.get_running_loop().run_in_executor(None, fn)` or `anyio.to_thread.run_sync`. |
| Licensing | No bundled copyrighted audio. Test tracks are synthesized locally from public-domain melodies. |

Dependency versions verified working on Python 3.9:
`fastapi==0.128.8`, `uvicorn==0.39.0`, `PyYAML==6.0.3`, `psutil==7.2.2`,
`zeroconf==0.148.0`, `pyatv==0.18.0`, `PyChromecast==13.1.0`, `httpx==0.28.1`.

---

## 2. File tree

```
project_os/                        # python package (importable)
├── __init__.py                   # __version__
├── __main__.py                   # python -m project_os
├── main.py                       # create_app() -> FastAPI, lifespan, router wiring
├── config.py                     # layered config
├── paths.py                      # PROJECT_OS_HOME resolution
├── db.py                         # sqlite wrapper + schema
├── events.py                     # EventBus
├── auth.py                       # users, sessions, require_auth dependency
├── errors.py                     # ApiError + handler
├── core/
│   ├── discovery.py              # mDNS + probe, DeviceRegistry
│   ├── suggestions.py            # capability inference -> suggestion cards
│   ├── plugins.py                # AppContext, AppInstance, PluginManager
│   ├── store.py                  # app repositories / install
│   ├── sysinfo.py                # cpu/mem/temp/disk/net/host
│   ├── services.py               # systemd + power control
│   ├── files.py                  # sandboxed FS
│   ├── shell.py                  # command runner sessions
│   └── ha.py                     # Home Assistant client (optional integration)
├── api/
│   ├── __init__.py               # api_router aggregating everything
│   ├── auth.py  system.py  devices.py  suggestions.py
│   ├── apps.py  store.py  files.py  shell.py  settings.py  ws.py
└── apps/
    └── birdtunes/
        ├── manifest.json
        ├── app.py                # async def setup(ctx) -> AppInstance
        ├── library.py  recommender.py  scheduler.py  safety.py  metadata.py
        ├── players/
        │   ├── base.py airplay.py chromecast.py ha_player.py local.py
        └── web/panel.js  panel.css
web/                              # core frontend (static, served at /)
├── index.html  style.css  app.js
├── lib/    dom.js store.js router.js api.js ws.js format.js toast.js icons.js
└── views/  setup.js login.js dashboard.js devices.js apps.js store.js
            settings.js system.js files.js terminal.js logs.js services.js
scripts/    install.sh uninstall.sh project-os.service make_test_tracks.py dev.sh
tests/      conftest.py test_*.py
docs/       ARCHITECTURE.md API.md APPS.md INSTALL.md
```

---

## 3. Paths & config

`paths.py`

```python
def home() -> Path            # $PROJECT_OS_HOME or ~/.project_os ; created on demand
def config_file() -> Path     # home()/config.yaml
def db_file() -> Path         # home()/project_os.db
def apps_dir() -> Path        # home()/apps           (user-installed apps)
def data_dir(app_id) -> Path  # home()/data/<app_id>
def media_dir() -> Path       # home()/media
def log_file() -> Path        # home()/project_os.log
def builtin_apps_dir() -> Path
def web_dir() -> Path
```

`config.py` — layered: **defaults → config.yaml → env (`PROJECT_OS__SERVER__PORT=8099`, `__` = nesting) → runtime API writes (persisted)**.

```python
class Config:
    def get(self, path: str, default=None): ...      # "server.port"
    def set(self, path: str, value) -> None: ...     # in-memory
    def save(self) -> None: ...                      # atomic write to config.yaml
    def as_dict(self) -> dict: ...                   # secrets redacted when redact=True
    def app(self, app_id: str) -> AppConfig: ...     # view over apps.settings.<id>
def load_config() -> Config
```

Defaults (normative):

```yaml
server:   {host: "0.0.0.0", port: 8099, base_url: ""}   # base_url auto-detected if empty
security:
  auth_enabled: true
  session_ttl_hours: 720
  allow_shell: false            # Advanced terminal — opt-in
  allow_service_control: false  # systemctl / reboot — opt-in
  allow_file_write: true
  file_roots: []                # extra roots beyond PROJECT_OS_HOME; [] = home only
ui:       {default_mode: "simple", theme: "auto", accent: "#5ac8a8"}
discovery:
  enabled: true
  interval_seconds: 300
  probe_hosts: []
apps:
  autostart: true
  enabled: ["birdtunes"]
  settings: {}                  # apps.settings.<app_id>: {...}
  repositories: [{name: "builtin", url: "builtin://core"}]
integrations:
  home_assistant: {enabled: false, url: "", token: ""}
logging:  {level: "INFO", retain_lines: 2000}
```

Secrets (`*.token`, `*.password`, `*_secret`) are redacted by `as_dict(redact=True)` and never leave the box in `GET /api/settings`.

---

## 4. Database

SQLite at `paths.db_file()`, WAL, `row_factory = sqlite3.Row`.

```python
class Database:
    def connect(self) -> sqlite3.Connection      # per-thread, cached
    def query(self, sql, params=()) -> list[sqlite3.Row]
    def one(self, sql, params=()) -> Optional[sqlite3.Row]
    def execute(self, sql, params=()) -> sqlite3.Cursor
    def executemany(self, sql, seq) -> None
    def migrate(self) -> None                     # idempotent, versioned
    def register_schema(self, name: str, statements: list[str]) -> None  # for apps
```

Core schema:

```sql
users(id INTEGER PK, username TEXT UNIQUE, password_hash TEXT, created_at TEXT)
sessions(token TEXT PK, user_id INTEGER, created_at TEXT, expires_at TEXT, user_agent TEXT)
devices(id TEXT PK, kind TEXT, name TEXT, address TEXT, port INTEGER,
        properties TEXT, capabilities TEXT, first_seen TEXT, last_seen TEXT,
        pinned INTEGER DEFAULT 0, ignored INTEGER DEFAULT 0, custom_name TEXT)
kv(namespace TEXT, key TEXT, value TEXT, PRIMARY KEY(namespace, key))
log(id INTEGER PK AUTOINCREMENT, ts TEXT, level TEXT, source TEXT, message TEXT, data TEXT)
suggestion_state(id TEXT PK, dismissed_at TEXT, applied_at TEXT)
```

Apps create tables prefixed `app_<app_id>_` via `ctx.db.register_schema()`.

---

## 5. Events

```python
class EventBus:
    def subscribe(self) -> AsyncIterator[Event]    # async context manager friendly
    async def publish(self, topic: str, data: dict) -> None
    def publish_nowait(self, topic: str, data: dict) -> None
```
`Event = {"topic": str, "ts": iso8601, "data": dict}`. Bounded per-subscriber queue
(256); slow consumers drop oldest, never block the publisher.

Topics: `system.stats`, `device.found`, `device.updated`, `device.lost`,
`app.state`, `suggestion.new`, `log`, `notify`, and `app.<id>.*` for plugins
(BirdTunes uses `app.birdtunes.state`).

---

## 6. Auth

- pbkdf2-hmac-sha256, 200 000 iterations, per-user salt.
  Stored as `pbkdf2_sha256$200000$<salt_hex>$<hash_hex>`. Verify with `hmac.compare_digest`.
- No users yet → **setup mode**: only `GET /api/health`, `POST /api/setup` and static
  assets work; everything else returns `428` with `{"error":"setup_required"}`.
- `POST /api/auth/login` sets cookie `project_os_session` (HttpOnly, SameSite=Lax,
  `Secure` only when request is https). Also accepted: `Authorization: Bearer <token>`.
- `require_auth` FastAPI dependency on every router except health/setup/login/static.
- `security.auth_enabled: false` bypasses auth (documented as LAN-only, warned in UI).

---

## 7. Plugin (app) contract

`manifest.json`:

```json
{
  "id": "birdtunes", "name": "BirdTunes", "version": "0.1.0",
  "description": "...", "author": "...", "icon": "bird",
  "category": "media", "entrypoint": "app:setup",
  "ui": {"panel": "web/panel.js", "styles": "web/panel.css"},
  "permissions": ["devices", "network", "media"],
  "config_schema": [
    {"key": "output.type", "type": "select", "label": "Output",
     "options": [{"value":"airplay","label":"AirPlay"}], "default": "airplay",
     "help": "..."}
  ],
  "min_project_os": "0.1.0"
}
```

`config_schema` field types: `string | number | boolean | select | device | path | time | tags | password`.
`device` renders a picker fed by `/api/devices` filtered by `"device_kinds": [...]`.

Python side:

```python
async def setup(ctx: AppContext) -> AppInstance
```

```python
class AppContext:
    id: str; manifest: dict; data_dir: Path; logger: logging.Logger
    config: AppConfig      # .get(path, default) .set(path, v) .save() .as_dict()
    db: Database
    bus: EventBus
    devices: DeviceRegistry
    core: Config
    def emit(self, event: str, data: dict) -> None    # -> topic "app.<id>.<event>"
    def base_url(self) -> str                          # LAN-reachable http://ip:port

class AppInstance:
    router: Optional[APIRouter]     # mounted at /api/apps/<id>
    async def start(self) -> None
    async def stop(self) -> None
    def status(self) -> dict        # small dict for dashboard cards
```

Frontend panel — ESM default export:

```js
export default {
  id: 'birdtunes',
  title: 'BirdTunes',
  async mount(root, ctx) { /* build DOM into root */ return () => {/* cleanup */} }
}
```
`ctx = { api, ws, appApi, config, toast, navigate, fmt, h, mode }` where
`appApi.get('/status')` → `/api/apps/birdtunes/status`.

Discovery order: `~/.project_os/apps/<id>/` overrides `project_os/apps/<id>/`.
A failing app is isolated: it is marked `error`, its traceback is stored, and the
rest of the system boots normally.

---

## 8. Core HTTP API

All JSON. Errors: `{"error": "<code>", "message": "<human>", "detail": {...}}`.

```
GET    /api/health                          public
POST   /api/setup            {username,password}
POST   /api/auth/login       {username,password}
POST   /api/auth/logout
GET    /api/auth/me

GET    /api/system/info                     host, model, os, kernel, python, uptime, version
GET    /api/system/stats                    cpu%, per-core, mem, swap, disk[], temp_c, load, net, uptime
GET    /api/system/services                 systemd units (allow-list + project_os*)
POST   /api/system/services/{unit}/{start|stop|restart}
GET    /api/system/logs?source=&lines=200
POST   /api/system/power/{reboot|shutdown}

GET    /api/devices?kind=&include_ignored=
POST   /api/devices/scan
GET    /api/devices/{id}
PATCH  /api/devices/{id}     {custom_name?, pinned?, ignored?}

GET    /api/suggestions
POST   /api/suggestions/{id}/dismiss
POST   /api/suggestions/{id}/apply

GET    /api/apps
GET    /api/apps/{id}
POST   /api/apps/{id}/{enable|disable|restart}
GET    /api/apps/{id}/config
PUT    /api/apps/{id}/config
GET    /api/apps/{id}/ui/{path}             serves the app's web/ dir

GET    /api/store
POST   /api/store/install    {id|url}
DELETE /api/store/{id}

GET    /api/files/list?path=
GET    /api/files/read?path=
PUT    /api/files/write      {path, content}
POST   /api/files/{mkdir|delete|rename|upload}
GET    /api/files/download?path=

POST   /api/shell/exec       {command, cwd}  -> {id}     (403 unless allow_shell)
WS     /api/shell/ws/{id}
POST   /api/shell/{id}/kill

GET    /api/settings                         redacted
PUT    /api/settings         {path: value}
GET    /api/settings/schema

WS     /api/ws                               event stream
```

---

## 9. Device discovery & suggestions

`core/discovery.py` scans mDNS for at least:

| Service | Inferred kind |
|---|---|
| `_airplay._tcp` / `_raop._tcp` | `apple_tv`, `homepod`, `airplay_speaker` |
| `_googlecast._tcp` | `chromecast`, `cast_group`, `cast_audio` |
| `_hap._tcp` | `homekit` |
| `_home-assistant._tcp` / `_hass._tcp` | `home_assistant` |
| `_printer._tcp`, `_ipp._tcp` | `printer` |
| `_ssh._tcp`, `_sftp-ssh._tcp` | `computer` |
| `_http._tcp`, `_https._tcp` | `web_service` |
| `_spotify-connect._tcp` | `speaker` |
| `_mqtt._tcp` | `mqtt_broker` |

Model inference from TXT records: `am=` (AirPlay model, e.g. `AppleTV14,1`,
`AudioAccessory5,1` = HomePod mini), `md=` (Cast model), `fn=` (friendly name).
Capabilities are a set of strings: `audio_out`, `video_out`, `cast_media`,
`airplay_audio`, `remote_control`, `automation_hub`, `shell`, `web_ui`.

`DeviceRegistry` persists to `devices`, dedupes by stable id
(`<kind>:<uuid-or-mac-or-host>`), emits `device.found/updated/lost`, and exposes:

```python
def list(self, kind=None, capability=None, include_ignored=False) -> list[Device]
def get(self, device_id) -> Optional[Device]
async def scan(self, timeout: float = 6.0) -> list[Device]
```

`core/suggestions.py` turns devices + system state into cards:

```python
@dataclass
class Suggestion:
    id: str; title: str; body: str; icon: str
    action: Optional[dict]        # {"type":"set_config","path":...,"value":...} | {"type":"open","href":...} | {"type":"install_app","app":...}
    priority: int; tags: list[str]
```
Examples it must produce: Apple TV found → offer as BirdTunes output; Chromecast
found → same; Home Assistant found → offer to connect the integration; no media in
library → offer to generate test tracks; shell disabled + Advanced mode → explain
how to enable; disk >85% → warn; default password / auth disabled → security warning.

---

## 10. BirdTunes

Purpose: play calm music for pet birds, on a schedule, on a real speaker, learning
from like / dislike / "play this less".

Config (`apps.settings.birdtunes`):

```yaml
output:   {type: "airplay", device_id: "", ha_entity: "", volume: 0.35, max_volume: 0.6}
library:  {paths: [], follow_symlinks: false, extensions: [".mp3",".m4a",".flac",".wav",".ogg"]}
schedule: {enabled: true, quiet_hours: {start: "20:00", end: "07:00"},
           sessions: [{start: "09:00", end: "11:00", days: [0,1,2,3,4,5,6]}]}
playback: {fade_seconds: 3, max_session_minutes: 120, gap_seconds: 2, shuffle: true}
recommender: {like_boost: 0.8, dislike_decay: 0.35, less_decay: 0.6,
              recency_window: 8, novelty_bonus: 0.25, explore_rate: 0.1}
```

Tables:

```sql
app_birdtunes_tracks(id TEXT PK, path TEXT UNIQUE, source TEXT, title TEXT,
  artist TEXT, album TEXT, duration REAL, tags TEXT, added_at TEXT, missing INTEGER DEFAULT 0)
app_birdtunes_feedback(track_id TEXT PK, likes INTEGER DEFAULT 0, dislikes INTEGER DEFAULT 0,
  less INTEGER DEFAULT 0, plays INTEGER DEFAULT 0, skips INTEGER DEFAULT 0,
  completions INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0, last_played TEXT)
app_birdtunes_history(id INTEGER PK AUTOINCREMENT, track_id TEXT, started_at TEXT,
  ended_at TEXT, completed INTEGER, device TEXT, reason TEXT)
```

API under `/api/apps/birdtunes`:

```
GET  /status            {state, track, position, output, volume, queue_len, quiet_hours_active, next_session}
POST /play              {track_id?}          POST /pause  /resume  /stop  /next
POST /volume            {value}
GET  /library?search=&tag=&sort=             POST /library/scan     POST /library/add-url
DELETE /library/{track_id}
POST /tracks/{id}/feedback  {action: like|dislike|less|more|block|reset}
GET  /queue                                  POST /queue/reorder
GET  /outputs                                PUT  /output {type, device_id}
POST /outputs/{device_id}/pair {pin?}        # AirPlay pairing flow
GET  /stats                                  GET  /history?limit=
GET  /media/{track_id}?t=<signed>            # LAN media stream for Chromecast
```

**Recommender** (`recommender.py`), pure functions, unit-tested. A `track` here is a plain
dict `{"id": str, "tags": list, "feedback": dict}` — the feedback row as stored, so the
scorer never touches the DB:

```python
score_track(track, cfg, now, recent_ids) -> float
pick_next(tracks, cfg, now, recent_ids, rng) -> Optional[track]   # rng: random.Random
explain(track, cfg, now, recent_ids) -> dict                      # factor name -> value
```

These three signatures are normative and supersede any earlier variant: the recency
penalty needs the track's own id, so the scorer takes the whole track, not just its
feedback row.

Scoring:

```
w  = 1.0
w *= (1 + like_boost * likes)
w *= dislike_decay ** dislikes
w *= less_decay ** less
w *= 0.12 if played within recency_window else 1.0
w *= 1 + novelty_bonus * (0 if plays else 1)
w *= completion_rate_factor        # completions/plays, clamped [0.6, 1.3]
w *= time_of_day_fit               # tag-based; "calm"/"lullaby" boosted near quiet hours
w  = 0 if blocked
```
Weighted random over non-zero weights, plus `explore_rate` chance of a uniform pick.
Deterministic when a `random.Random(seed)` is injected — tests depend on this.

**Safety** (`safety.py`) — this is a device pointed at a live animal, so these are
enforced server-side, not just in the UI: volume clamped to `max_volume`; quiet
hours block playback and stop anything running; `max_session_minutes` auto-stops;
fade-in over `fade_seconds`; refuse to start if output device is unreachable.

**Players** (`players/base.py`):

```python
class Player(abc.ABC):
    name: str
    @classmethod
    def available(cls) -> tuple[bool, str]      # (installed?, hint if not)
    async def connect(self, device: Device) -> None
    async def play(self, track: Track, url: str) -> None
    async def stop(self) -> None
    async def pause(self) -> None
    async def resume(self) -> None
    async def set_volume(self, level: float) -> None   # 0.0-1.0
    async def status(self) -> dict
    async def disconnect(self) -> None
```

- `airplay.py` — **pyatv 0.18**. `pyatv.scan(loop, timeout=, protocol={Protocol.RAOP, Protocol.AirPlay})`
  → `list[BaseConfig]`; `pyatv.connect(config, loop, storage=)` → `AppleTV`;
  `atv.stream.stream_file(path, metadata=MediaMetadata(...))` (blocks until the track
  ends — run it as a task and cancel to stop); `atv.audio.set_volume(level*100)`
  (pyatv volume is **0-100**, our API is 0-1 — convert); `atv.close()`.
  Credentials from pairing are persisted with `pyatv.storage.file_storage.FileStorage`
  at `data_dir/pyatv.json`.
- `chromecast.py` — **PyChromecast 13.1** (sync lib → always `run_in_executor`).
  `pychromecast.discovery.CastBrowser(SimpleCastListener(...), zconf)`;
  `pychromecast.get_chromecast_from_cast_info(info, zconf)`; `cast.wait()`;
  `cast.media_controller.play_media(url, content_type, title=..., stream_type="BUFFERED")`;
  `cast.media_controller.block_until_active()`; `cast.set_volume(level)`;
  `cast.media_controller.stop()`. Needs `url` reachable on the LAN → uses
  `/api/apps/birdtunes/media/{id}?t=<hmac>`.
- `ha_player.py` — calls Home Assistant `media_player.play_media` via `core/ha.py`.
- `local.py` — fallback: `mpv`/`ffplay`/`aplay`, whichever exists; used for the Pi's
  own 3.5 mm jack and for CI.

Signed media URLs: `hmac_sha256(secret, f"{track_id}:{exp}")`, secret generated at
first boot into `kv('birdtunes','media_secret')`. Chromecast can fetch without a
session cookie; nobody else can enumerate the library.

`scripts/make_test_tracks.py` synthesizes short WAV files from **public-domain**
melodies (Twinkle Twinkle, Brahms' Lullaby, Ode to Joy, Greensleeves) with the
stdlib `wave` + `math` modules — no downloads, no copyright, and it gives the test
suite and a fresh install something to play.

---

## 11. Frontend

Zero-build ES modules. `web/lib/dom.js` exports `h()` (hyperscript producing real
DOM nodes), `mount()`, `text()`, `frag()`. `store.js` exports `createStore(initial)`
→ `{get, set, update, subscribe}`. `router.js` is hash-based (`#/dashboard`,
`#/apps/birdtunes`). `api.js` wraps fetch: JSON in/out, throws `ApiError`, redirects
to `#/login` on 401 and `#/setup` on 428. `ws.js` auto-reconnects with backoff and
re-subscribes.

Shell layout: fixed sidebar (collapses to a bottom bar under 720 px), topbar with
mode switch + system pills (CPU/RAM/temp, live from `system.stats`), content area.

- **Simple mode** nav: Dashboard · Apps · Devices · Settings
- **Advanced mode** adds: System · Services · Files · Terminal · Logs

Mode is per-browser (`localStorage`), defaulting to `ui.default_mode`. Advanced is a
visible toggle, not a hidden flag — switching is instant, no reload.

Dashboard = card grid: system card, per-app status cards (from `AppInstance.status()`),
device cards, and **suggestion cards** with one-click actions.

Theming via CSS custom properties on `:root` + `[data-theme]`; dark is the default,
`prefers-color-scheme` respected when `ui.theme = auto`. Accent colour comes from
config. Must be usable on a phone.

Accessibility: real `<button>`s, labels tied to inputs, visible focus rings, no
colour-only state, `aria-live` for the toast region.

---

## 12. Testing

`pytest` + `fastapi.testclient`. Every module ships tests. No test may require real
hardware, a real network, or a real Apple TV — mDNS, pyatv and pychromecast are
faked at the seam. Required coverage:

- config layering & env override & secret redaction & atomic save
- auth: setup gate, login, bad password, session expiry, bearer, disabled-auth
- db migrations idempotent, app schema registration
- discovery: TXT-record parsing → kind/capabilities, dedupe, lost detection
- suggestions: each rule fires on its trigger and only then
- plugins: load, bad manifest isolation, config get/set round-trip, UI file serving
- files: **path traversal is rejected** (`../`, symlink escape, absolute outside roots)
- shell: disabled by default returns 403
- birdtunes: recommender determinism + like/dislike/less actually move selection
  probability, quiet-hours blocks playback, volume clamp, signed URL verify/expiry,
  library scan, feedback endpoints
- an end-to-end smoke test that boots the app and walks health → setup → login →
  dashboard data → birdtunes status
