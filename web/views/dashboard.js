// views/dashboard.js — Simple-mode home. A card grid: system health,
// suggestion cards, one status card per running app, and a compact device list.
//
// ---------------------------------------------------------------------------
// APP STATUS CARD CONVENTION
// ---------------------------------------------------------------------------
// `AppInstance.status()` (ARCHITECTURE §7) is rendered generically, so a new app
// gets a dashboard card without touching this file. The dict may contain:
//
//   {
//     "state":   "playing",              // short word -> badge in the header
//     "level":   "ok" | "warn" | "danger" | "info",   // badge colour (optional)
//     "summary": "Brahms' Lullaby",      // one line under the title
//     "fields": [                        // the body; a list OR a plain dict
//       {"label": "Output", "value": "Living room", "kind": "text"},
//       {"label": "Volume", "value": 0.35, "kind": "percent"},
//       {"label": "Next session", "value": "2026-08-08T09:00:00Z", "kind": "relative"}
//     ],
//     "actions": [                       // buttons in the card footer
//       {"id": "pause", "label": "Pause", "icon": "pause",
//        "method": "POST", "path": "/pause", "body": {},
//        "variant": "primary" | "ghost" | "danger", "confirm": "Are you sure?"}
//     ]
//   }
//
// `kind` is one of: text, number, bytes, duration, percent, temperature,
// datetime, time, relative, boolean, badge, progress, link, list, code.
// It is optional — when it is missing the value's type decides, an ISO-8601
// string renders as relative time, and a number whose key looks like a unit
// (*_bytes, *_percent, *_seconds, temp*) is formatted accordingly. A status
// dict with no "fields" key at all is treated as a flat {label: value} mapping,
// which is why *any* app renders something sensible on day one.
//
// `path` is relative to /api/apps/<id>, i.e. exactly what appApi uses.

import {h, mount, clear} from '../lib/dom.js';
import {icon, hasIcon, appIcon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import * as api from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';
import {navigate} from '../lib/router.js';

/* --------------------------------------------------------------- helpers */

function card(options) {
  const {
    title, sub, mark, iconName, tools, body, footer,
    variant = '', href = null,
  } = options;
  // `mark` is a ready-made node (an app's emoji or named icon); `iconName` is
  // the shell's own icon set.
  const markNode = mark || (iconName ? icon(iconName, {size: 18}) : null);
  const head = h('div', {class: 'card__header'},
    markNode ? h('span', {class: 'card__icon'}, markNode) : null,
    h('div', {class: 'grow'},
      href
        ? h('a', {class: 'card__title', href}, title)
        : h('div', {class: 'card__title'}, title),
      sub ? h('div', {class: 'card__sub'}, sub) : null,
    ),
    tools ? h('div', {class: 'card__tools'}, tools) : null,
  );
  return h('div', {class: 'card' + (variant ? ' card--' + variant : '')},
    head,
    body ? h('div', {class: 'card__body'}, body) : null,
    footer ? h('div', {class: 'card__footer'}, footer) : null,
  );
}

function skeletonCard() {
  return h('div', {class: 'card'},
    h('div', {class: 'card__header'},
      h('div', {class: 'skeleton', style: {width: '32px', height: '32px', borderRadius: '9px'}}),
      h('div', {class: 'skeleton skeleton--title', style: {flex: '1'}}),
    ),
    h('div', {class: 'card__body'},
      h('div', {class: 'skeleton skeleton--text'}),
      h('div', {class: 'skeleton skeleton--text skeleton--line-sm'}),
      h('div', {class: 'skeleton skeleton--block'}),
    ),
  );
}

function errorState(message, onRetry) {
  return h('div', {class: 'notice notice--error'},
    icon('warning', {size: 18}),
    h('div', {class: 'notice__body'},
      h('span', null, message),
      onRetry
        ? h('div', {class: 'notice__actions'},
            h('button', {class: 'btn btn--sm', type: 'button', onClick: onRetry},
              icon('refresh', {size: 14}), 'Retry'),
          )
        : null,
    ),
  );
}

function emptyState(title, message, action) {
  return h('div', {class: 'empty empty--sm'},
    h('span', {class: 'empty__icon'}, icon('info', {size: 18})),
    h('p', {class: 'empty__title'}, title),
    message ? h('p', {class: 'empty__text'}, message) : null,
    action || null,
  );
}

function meterRow(label, value, unitText, thresholds) {
  const known = value !== null && value !== undefined && isFinite(value);
  const pct = known ? Math.max(0, Math.min(100, Number(value))) : 0;
  const limits = thresholds || {warn: 75, danger: 90};
  const level = !known ? '' : pct >= limits.danger ? ' meter--danger' : pct >= limits.warn ? ' meter--warn' : '';
  return h('div', {class: 'stack stack--sm'},
    h('div', {class: 'field-row'},
      h('span', {class: 'field-row__label'}, label),
      h('span', {class: 'field-row__value'}, known ? (unitText || fmt.percent(pct)) : '—'),
    ),
    h('div', {
      class: 'meter' + level,
      role: 'progressbar',
      'aria-label': label,
      'aria-valuenow': known ? Math.round(pct) : null,
      'aria-valuemin': '0',
      'aria-valuemax': '100',
    },
      h('div', {class: 'meter__fill', style: {width: pct + '%'}}),
    ),
  );
}

/* ------------------------------------------------- generic field renderer */

const RESERVED_STATUS_KEYS = new Set([
  'fields', 'actions', 'state', 'level', 'summary', 'icon', 'title', 'id',
  'name', 'error', 'message',
]);

// "2026-08-08T03:12:00Z", "2026-08-08 03:12:00", "2026-08-08"
const ISO_LIKE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$/;

function inferKind(value) {
  if (typeof value === 'boolean') return 'boolean';
  if (typeof value === 'number') return 'number';
  if (Array.isArray(value)) return 'list';
  if (value && typeof value === 'object') return 'code';
  if (typeof value === 'string' && ISO_LIKE.test(value)) return 'relative';
  return 'text';
}

// Numbers carry no unit, so when an app did not declare a `kind` we read one
// from the key name. Only applied to the zero-config path — an explicit kind
// always wins.
const KEY_KINDS = [
  [/(^|_)(bytes|size|disk|memory|ram)(_|$)/i, 'bytes'],
  [/(_percent|_pct|_ratio)$/i, 'percent'],
  [/(^|_)(temp|temperature)(_c)?$/i, 'temperature'],
  [/(_seconds|_secs|_duration|_elapsed|^uptime$|^elapsed$|^duration$)$/i, 'duration'],
];

function kindFromKey(key) {
  if (!key) return null;
  for (const [pattern, kind] of KEY_KINDS) {
    if (pattern.test(key)) return kind;
  }
  return null;
}

function renderValue(field) {
  const value = field.value;
  const kind = field.kind || inferKind(value);

  if (value === null || value === undefined || value === '') {
    return h('span', {class: 'field-row__value faint'}, '—');
  }

  switch (kind) {
    case 'bytes':
      return h('span', {class: 'field-row__value'}, fmt.bytes(value));
    case 'duration':
      return h('span', {class: 'field-row__value'}, fmt.duration(value));
    case 'percent': {
      const num = Number(value);
      return h('span', {class: 'field-row__value'}, fmt.percent(num, {fraction: Math.abs(num) <= 1}));
    }
    case 'temperature':
      return h('span', {class: 'field-row__value'}, fmt.temperature(value));
    case 'number':
      return h('span', {class: 'field-row__value'},
        fmt.number(value, {digits: Number.isInteger(Number(value)) ? 0 : 2}));
    case 'datetime':
      return h('span', {class: 'field-row__value'}, fmt.dateTime(value));
    case 'time':
      return h('span', {class: 'field-row__value'}, fmt.clockTime(value));
    case 'relative':
      return h('span', {class: 'field-row__value'}, fmt.relativeTime(value));
    case 'boolean':
      return h('span', {class: 'badge ' + (value ? 'badge--ok' : 'badge--plain')}, value ? 'Yes' : 'No');
    case 'badge':
      return h('span', {class: 'badge' + (field.level ? ' badge--' + field.level : '')}, String(value));
    case 'progress': {
      const num = Number(value);
      const pct = Math.abs(num) <= 1 ? num * 100 : num;
      return h('div', {class: 'meter', style: {width: '90px'}},
        h('div', {class: 'meter__fill', style: {width: Math.max(0, Math.min(100, pct)) + '%'}}));
    }
    case 'link':
      return h('a', {
        class: 'field-row__value',
        href: field.href || String(value),
        target: /^https?:/i.test(field.href || String(value)) ? '_blank' : null,
        rel: /^https?:/i.test(field.href || String(value)) ? 'noopener noreferrer' : null,
      }, field.text || String(value));
    case 'list': {
      const items = Array.isArray(value) ? value : [value];
      if (!items.length) return h('span', {class: 'field-row__value faint'}, '—');
      return h('span', {class: 'row', style: {justifyContent: 'flex-end'}},
        items.slice(0, 4).map((item) => h('span', {class: 'tag'}, String(item))),
        items.length > 4 ? h('span', {class: 'tiny faint'}, '+' + (items.length - 4)) : null);
    }
    case 'code':
      return h('span', {class: 'field-row__value mono tiny'},
        fmt.truncate(typeof value === 'string' ? value : JSON.stringify(value), 42));
    default:
      return h('span', {class: 'field-row__value', title: String(value)}, fmt.truncate(String(value), 48));
  }
}

function normalizeField(entry, key) {
  if (entry === null || entry === undefined) return null;
  if (typeof entry === 'object' && !Array.isArray(entry) && ('value' in entry || 'label' in entry)) {
    const field = Object.assign({label: key ? fmt.humanize(key) : ''}, entry);
    if (!field.kind && typeof entry.value === 'number') {
      const guess = kindFromKey(key || entry.key);
      if (guess) field.kind = guess;
    }
    return field;
  }
  const field = {label: key ? fmt.humanize(key) : '', value: entry};
  if (typeof entry === 'number') {
    const guess = kindFromKey(key);
    if (guess) field.kind = guess;
  }
  return field;
}

/** Turn any status dict into a list of renderable fields. */
export function statusFields(status) {
  if (!status || typeof status !== 'object') return [];
  let source = status.fields;
  if (source === undefined || source === null) {
    source = {};
    for (const key of Object.keys(status)) {
      if (RESERVED_STATUS_KEYS.has(key)) continue;
      source[key] = status[key];
    }
  }
  let list;
  if (Array.isArray(source)) {
    list = source.map((entry) => normalizeField(entry, entry && entry.key));
  } else if (typeof source === 'object') {
    list = Object.keys(source).map((key) => normalizeField(source[key], key));
  } else {
    list = [];
  }
  return list.filter((field) => field && (field.label || field.value !== undefined)).slice(0, 8);
}

function fieldsBlock(fields) {
  if (!fields.length) return null;
  return h('div', {class: 'fields'},
    fields.map((field) => h('div', {class: 'field-row'},
      h('span', {class: 'field-row__label'}, field.label || ''),
      renderValue(field),
    )),
  );
}

/* ------------------------------------------------------------- device map */

const DEVICE_ICONS = {
  apple_tv: 'tv',
  tv: 'tv',
  homepod: 'speaker',
  airplay_speaker: 'speaker',
  speaker: 'speaker',
  chromecast: 'cast',
  cast_group: 'cast',
  cast_audio: 'cast',
  home_assistant: 'home',
  homekit: 'home',
  printer: 'file',
  computer: 'terminal',
  web_service: 'link',
  mqtt_broker: 'chip',
};

function deviceIcon(kind) {
  const name = DEVICE_ICONS[kind];
  return icon(name && hasIcon(name) ? name : 'devices', {size: 18});
}

/* ------------------------------------------------------------------ view */

export default {
  id: 'dashboard',
  title: 'Dashboard',

  async mount(root, ctx) {
    const store = ctx.store;
    const readStats = ctx.readStats || ((raw) => raw || {});
    let disposed = false;

    const state = {
      suggestions: 'loading',
      apps: 'loading',
      devices: 'loading',
      system: store.get('stats') ? 'ready' : 'loading',
      errors: {},
      pendingSuggestions: new Set(),
      busyActions: new Set(),
    };

    const systemSlot = h('div', {class: 'slot'});
    const suggestionSlot = h('div', {class: 'slot'});
    const appSlot = h('div', {class: 'slot'});
    const deviceSlot = h('div', {class: 'slot'});

    const refreshBtn = h('button', {
      class: 'btn btn--sm', type: 'button',
      onClick: () => loadAll(true),
    }, icon('refresh', {size: 15}), 'Refresh');

    mount(root, [
      h('div', {class: 'page__header'},
        h('div', null,
          h('h2', null, greeting(ctx)),
          h('p', {class: 'page__lead'}, 'Everything running on this machine, at a glance.'),
        ),
        h('div', {class: 'page__actions'}, refreshBtn),
      ),
      h('div', {class: 'grid'}, systemSlot, suggestionSlot, appSlot, deviceSlot),
    ]);

    /* ------------------------------------------------------ system card */

    function renderSystem() {
      const raw = store.get('stats');
      if (!raw && state.system === 'loading') {
        mount(systemSlot, skeletonCard());
        return;
      }
      if (!raw && state.errors.system) {
        mount(systemSlot, card({
          title: 'System',
          iconName: 'cpu',
          variant: 'danger',
          body: errorState(state.errors.system, () => loadStats()),
        }));
        return;
      }

      const s = readStats(raw);
      const diskLabel = s.disk
        ? (s.disk.mountpoint || s.disk.mount || '/') +
          (isFinite(s.disk.used) && isFinite(s.disk.total)
            ? ' · ' + fmt.bytes(s.disk.used) + ' / ' + fmt.bytes(s.disk.total)
            : '')
        : null;

      mount(systemSlot, card({
        title: 'System',
        sub: isFinite(s.uptime) ? 'Up ' + fmt.uptime(s.uptime) : null,
        iconName: 'cpu',
        tools: h('a', {class: 'btn btn--icon btn--sm', href: '#/system', title: 'System details', 'aria-label': 'System details'},
          icon('chevron', {size: 16})),
        body: [
          meterRow('CPU', s.cpu, s.cpu === null ? null : fmt.percent(s.cpu)),
          meterRow('Memory', s.memPercent,
            isFinite(s.memUsed) && isFinite(s.memTotal)
              ? fmt.bytes(s.memUsed) + ' / ' + fmt.bytes(s.memTotal)
              : null),
          s.diskPercent !== null
            ? meterRow('Disk', s.diskPercent, diskLabel, {warn: 85, danger: 93})
            : null,
          h('div', {class: 'stats'},
            h('div', {class: 'stat'},
              h('span', {class: 'stat__label'}, 'Temp'),
              h('span', {class: 'stat__value' + (s.temp !== null && s.temp >= 80 ? ' danger' : s.temp !== null && s.temp >= 70 ? ' warn' : '')},
                s.temp === null ? '—' : fmt.temperature(s.temp, {digits: 0})),
            ),
            h('div', {class: 'stat'},
              h('span', {class: 'stat__label'}, 'Load'),
              h('span', {class: 'stat__value'},
                Array.isArray(s.load) && s.load.length ? Number(s.load[0]).toFixed(2) : '—'),
            ),
            h('div', {class: 'stat'},
              h('span', {class: 'stat__label'}, 'Uptime'),
              h('span', {class: 'stat__value'}, isFinite(s.uptime) ? fmt.uptime(s.uptime) : '—'),
            ),
          ),
        ],
      }));
    }

    /* ------------------------------------------------- suggestion cards */

    function suggestionCard(suggestion) {
      const action = suggestion.action || null;
      const href = action && action.href ? String(action.href) : '';
      const external = /^https?:/i.test(href);
      const busy = state.pendingSuggestions.has(suggestion.id);

      const applyLabel = !action
        ? 'Got it'
        : action.type === 'install_app'
          ? 'Install'
          : action.type === 'open'
            ? 'Open'
            : 'Apply';

      const applyBtn = external
        ? h('a', {
            class: 'btn btn--primary btn--sm',
            href, target: '_blank', rel: 'noopener noreferrer',
            onClick: () => applySuggestion(suggestion, {navigateTo: null}),
          }, applyLabel, icon('link', {size: 14}))
        : h('button', {
            class: 'btn btn--primary btn--sm' + (busy ? ' is-busy' : ''),
            type: 'button',
            disabled: busy,
            onClick: () => applySuggestion(suggestion, {navigateTo: href && href.startsWith('#') ? href : null}),
          }, busy ? h('span', {class: 'spinner spinner--sm'}) : null, applyLabel);

      return card({
        title: suggestion.title || 'Suggestion',
        mark: appIcon(suggestion.icon, {fallback: 'info', size: 18}),
        variant: 'accent',
        body: [
          suggestion.body ? h('p', {class: 'suggestion__body'}, suggestion.body) : null,
          Array.isArray(suggestion.tags) && suggestion.tags.length
            ? h('div', {class: 'suggestion__tags'}, suggestion.tags.slice(0, 4).map((tag) => h('span', {class: 'tag'}, String(tag))))
            : null,
        ],
        footer: [
          applyBtn,
          h('button', {
            class: 'btn btn--ghost btn--sm', type: 'button', disabled: busy,
            onClick: () => dismissSuggestion(suggestion),
          }, 'Dismiss'),
        ],
      });
    }

    function renderSuggestions() {
      const list = store.get('suggestions') || [];
      if (state.suggestions === 'loading') {
        mount(suggestionSlot, skeletonCard());
        return;
      }
      if (state.suggestions === 'error') {
        mount(suggestionSlot, card({
          title: 'Suggestions',
          iconName: 'info',
          body: errorState(state.errors.suggestions || 'Could not load suggestions.', () => loadSuggestions()),
        }));
        return;
      }
      if (!list.length) {
        clear(suggestionSlot);
        return;
      }
      const sorted = list.slice().sort((a, b) => (Number(b.priority) || 0) - (Number(a.priority) || 0));
      mount(suggestionSlot, sorted.map(suggestionCard));
    }

    async function applySuggestion(suggestion, {navigateTo} = {}) {
      if (state.pendingSuggestions.has(suggestion.id)) return;
      state.pendingSuggestions.add(suggestion.id);
      const before = store.get('suggestions') || [];
      // Optimistic: the card disappears immediately.
      store.set({suggestions: before.filter((s) => s.id !== suggestion.id)});
      renderSuggestions();
      try {
        await api.post('/suggestions/' + encodeURIComponent(suggestion.id) + '/apply', {});
        toast(suggestion.title ? suggestion.title + ' applied' : 'Applied', {type: 'success'});
        if (navigateTo) navigate(navigateTo);
        loadApps();
        loadSuggestions();
      } catch (err) {
        store.set({suggestions: before});
        renderSuggestions();
        toast((err && err.message) || 'Could not apply that suggestion.', {type: 'error'});
      } finally {
        state.pendingSuggestions.delete(suggestion.id);
      }
    }

    async function dismissSuggestion(suggestion) {
      if (state.pendingSuggestions.has(suggestion.id)) return;
      state.pendingSuggestions.add(suggestion.id);
      const before = store.get('suggestions') || [];
      store.set({suggestions: before.filter((s) => s.id !== suggestion.id)});
      renderSuggestions();
      try {
        await api.post('/suggestions/' + encodeURIComponent(suggestion.id) + '/dismiss', {});
      } catch (err) {
        store.set({suggestions: before});
        renderSuggestions();
        toast((err && err.message) || 'Could not dismiss that suggestion.', {type: 'error'});
      } finally {
        state.pendingSuggestions.delete(suggestion.id);
      }
    }

    /* -------------------------------------------------------- app cards */

    function appStateBadge(app) {
      const status = app.status && typeof app.status === 'object' ? app.status : {};
      const label = status.state || app.state || (app.enabled === false ? 'disabled' : 'idle');
      const level = status.level || (
        app.state === 'error' ? 'danger'
          : app.state === 'running' || app.state === 'started' ? 'ok'
            : app.enabled === false ? '' : 'info');
      return h('span', {class: 'badge' + (level ? ' badge--' + level : '')}, String(label));
    }

    async function runAppAction(app, action) {
      const key = app.id + ':' + (action.id || action.path || action.label);
      if (state.busyActions.has(key)) return;
      if (action.confirm) {
        const ok = await confirm(String(action.confirm), {
          title: action.label || 'Confirm',
          danger: action.variant === 'danger',
        });
        if (!ok) return;
      }
      state.busyActions.add(key);
      renderApps();
      try {
        const appApi = api.apiFor('/api/apps/' + encodeURIComponent(app.id));
        const method = String(action.method || 'POST').toUpperCase();
        const path = action.path || ('/' + (action.id || ''));
        await appApi.request(method, path, action.body === undefined ? {} : action.body);
        toast((action.label || 'Done') + ' · ' + (app.name || app.id), {type: 'success', timeout: 2500});
      } catch (err) {
        toast((err && err.message) || 'That action failed.', {type: 'error'});
      } finally {
        state.busyActions.delete(key);
        await loadApps();
      }
    }

    function appCard(app) {
      const status = app.status && typeof app.status === 'object' ? app.status : {};
      const fields = statusFields(status);
      const actions = Array.isArray(status.actions) ? status.actions.slice(0, 4) : [];
      const href = '#/apps/' + encodeURIComponent(app.id);

      const footer = [];
      for (const action of actions) {
        const key = app.id + ':' + (action.id || action.path || action.label);
        const busy = state.busyActions.has(key);
        footer.push(h('button', {
          class: 'btn btn--sm' + (action.variant === 'primary' ? ' btn--primary' : action.variant === 'danger' ? ' btn--danger' : ' btn--ghost') + (busy ? ' is-busy' : ''),
          type: 'button',
          disabled: busy,
          onClick: () => runAppAction(app, action),
        },
          busy
            ? h('span', {class: 'spinner spinner--sm'})
            : (action.icon && hasIcon(action.icon) ? icon(action.icon, {size: 15}) : null),
          action.label || fmt.humanize(action.id || 'Run'),
        ));
      }
      footer.push(h('a', {class: 'btn btn--ghost btn--sm', href, style: {marginLeft: 'auto'}},
        'Open', icon('chevron', {size: 14})));

      return card({
        title: app.name || app.id,
        sub: status.summary || app.description || null,
        mark: appIcon(app.icon, {fallback: 'apps', size: 18}),
        href,
        tools: appStateBadge(app),
        variant: app.state === 'error' ? 'danger' : '',
        body: app.state === 'error'
          ? h('p', {class: 'small muted'}, app.error || app.message || 'This app failed to start. Check the logs.')
          : (fieldsBlock(fields) || h('p', {class: 'small faint'}, 'No status reported.')),
        footer,
      });
    }

    function renderApps() {
      if (state.apps === 'loading') {
        mount(appSlot, skeletonCard());
        return;
      }
      if (state.apps === 'error') {
        mount(appSlot, card({
          title: 'Apps',
          iconName: 'apps',
          body: errorState(state.errors.apps || 'Could not load apps.', () => loadApps()),
        }));
        return;
      }
      const apps = (store.get('apps') || []).filter((app) => app && app.id && app.enabled !== false);
      if (!apps.length) {
        mount(appSlot, card({
          title: 'Apps',
          iconName: 'apps',
          body: emptyState('No apps running yet',
            'Install one from the store and it will show up here with its own card.',
            h('a', {class: 'btn btn--primary btn--sm', href: '#/store'}, icon('download', {size: 15}), 'Open the store')),
        }));
        return;
      }
      mount(appSlot, apps.map(appCard));
    }

    /* ----------------------------------------------------- devices card */

    function renderDevices() {
      if (state.devices === 'loading') {
        mount(deviceSlot, skeletonCard());
        return;
      }
      if (state.devices === 'error') {
        mount(deviceSlot, card({
          title: 'Devices',
          iconName: 'devices',
          body: errorState(state.errors.devices || 'Could not load devices.', () => loadDevices()),
        }));
        return;
      }
      const devices = (store.get('devices') || []).filter((d) => d && !d.ignored);
      const visible = devices.slice(0, 5);

      mount(deviceSlot, card({
        title: 'Devices',
        sub: devices.length ? devices.length + ' found on your network' : null,
        iconName: 'devices',
        tools: h('a', {class: 'btn btn--icon btn--sm', href: '#/devices', title: 'All devices', 'aria-label': 'All devices'},
          icon('chevron', {size: 16})),
        body: visible.length
          ? h('div', {class: 'list'}, visible.map((device) => h('div', {class: 'list__row'},
              deviceIcon(device.kind),
              h('div', {class: 'list__main'},
                h('span', {class: 'list__title'}, device.custom_name || device.name || device.id),
                h('span', {class: 'list__sub'},
                  [fmt.humanize(device.kind || 'device'), device.address || ''].filter(Boolean).join(' · ')),
              ),
              h('span', {class: 'list__aside'},
                device.lost || device.online === false
                  ? h('span', {class: 'badge badge--plain'}, 'offline')
                  : h('span', {class: 'badge badge--ok'}, 'online'),
              ),
            )))
          : emptyState('Nothing discovered yet',
              'project-os looks for AirPlay speakers, Chromecasts and hubs with mDNS.',
              h('button', {class: 'btn btn--sm', type: 'button', onClick: scanDevices},
                icon('search', {size: 15}), 'Scan now')),
        footer: visible.length
          ? [
              h('button', {class: 'btn btn--ghost btn--sm', type: 'button', onClick: scanDevices},
                icon('refresh', {size: 15}), 'Scan'),
              devices.length > visible.length
                ? h('a', {class: 'btn btn--ghost btn--sm', href: '#/devices', style: {marginLeft: 'auto'}},
                    'All ' + devices.length, icon('chevron', {size: 14}))
                : null,
            ]
          : null,
      }));
    }

    let scanning = false;
    async function scanDevices() {
      if (scanning) return;
      scanning = true;
      toast('Scanning the network…', {type: 'info', timeout: 2500});
      try {
        const raw = await api.post('/devices/scan', {});
        const devices = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.devices) ? raw.devices : null);
        if (devices) store.set({devices});
        else await loadDevices();
        renderDevices();
      } catch (err) {
        toast((err && err.message) || 'The scan failed.', {type: 'error'});
      } finally {
        scanning = false;
      }
    }

    /* ------------------------------------------------------------ loads */

    async function loadStats() {
      try {
        const raw = await api.get('/system/stats');
        if (disposed) return;
        state.system = 'ready';
        state.errors.system = null;
        store.set({stats: raw});
        renderSystem();
      } catch (err) {
        if (disposed) return;
        state.system = 'error';
        state.errors.system = (err && err.message) || 'Could not read system stats.';
        renderSystem();
      }
    }

    async function loadSuggestions() {
      state.suggestions = 'loading';
      renderSuggestions();
      try {
        const raw = await api.get('/suggestions');
        if (disposed) return;
        const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.suggestions) ? raw.suggestions : []);
        state.suggestions = 'ready';
        store.set({suggestions: list});
        renderSuggestions();
      } catch (err) {
        if (disposed) return;
        state.suggestions = 'error';
        state.errors.suggestions = (err && err.message) || 'Could not load suggestions.';
        renderSuggestions();
      }
    }

    async function loadApps() {
      try {
        const raw = await api.get('/apps');
        if (disposed) return;
        const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.apps) ? raw.apps : []);
        state.apps = 'ready';
        store.set({apps: list});
        renderApps();
      } catch (err) {
        if (disposed) return;
        state.apps = 'error';
        state.errors.apps = (err && err.message) || 'Could not load apps.';
        renderApps();
      }
    }

    async function loadDevices() {
      try {
        const raw = await api.get('/devices');
        if (disposed) return;
        const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.devices) ? raw.devices : []);
        state.devices = 'ready';
        store.set({devices: list});
        renderDevices();
      } catch (err) {
        if (disposed) return;
        state.devices = 'error';
        state.errors.devices = (err && err.message) || 'Could not load devices.';
        renderDevices();
      }
    }

    function loadAll(manual) {
      if (manual) {
        refreshBtn.disabled = true;
        setTimeout(() => { refreshBtn.disabled = false; }, 800);
      }
      return Promise.all([loadStats(), loadSuggestions(), loadApps(), loadDevices()]);
    }

    /* ------------------------------------------------------ live wiring */

    renderSystem();
    renderSuggestions();
    renderApps();
    renderDevices();

    const offStore = store.subscribe((next, prev) => {
      if (disposed) return;
      if (!Object.is(next.stats, prev.stats)) {
        state.system = 'ready';
        renderSystem();
      }
      if (!Object.is(next.apps, prev.apps) && state.apps !== 'loading') renderApps();
      if (!Object.is(next.devices, prev.devices) && state.devices !== 'loading') renderDevices();
    });

    // Apps and devices also change through ws events the shell folds into the
    // store, so nothing else needs polling here.
    loadAll(false);

    return () => {
      disposed = true;
      offStore();
    };
  },
};

function greeting(ctx) {
  const name = ctx.user && ctx.user.username ? ctx.user.username : '';
  const hour = new Date().getHours();
  const part = hour < 5 ? 'Good night' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  return name ? part + ', ' + name : part;
}
