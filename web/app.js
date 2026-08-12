// app.js — project-os shell.
//
// Boot order: /api/health decides whether we go to the setup wizard, the login
// screen, or the full shell. From there the hash router owns the content area
// and every view is loaded with a dynamic import(), so Advanced-only views cost
// nothing while the browser sits in Simple mode.
//
// View module contract (same shape as an app panel, per ARCHITECTURE §7):
//
//   export default {
//     id: 'devices', title: 'Devices',
//     async mount(root, ctx) { ...; return () => cleanup(); }
//   }
//
// A bare function export, a named `mount` export, or a returned DOM node are
// all accepted too — the shell normalises them.

import {h, mount, clear} from './lib/dom.js';
import {createStore} from './lib/store.js';
import {createRouter, navigate} from './lib/router.js';
import * as api from './lib/api.js';
import {apiFor, ApiError} from './lib/api.js';
import {connect} from './lib/ws.js';
import * as fmt from './lib/format.js';
import {t, setStrings} from './lib/format.js';
import {PT_BR} from './lib/strings-pt.js';
import {toast, confirm} from './lib/toast.js';
import {icon, appIcon} from './lib/icons.js';

// A interface fala português. As views registram o inglês delas ao serem
// importadas (e só como reserva, com activate:false), então esta linha tem de
// vir antes de qualquer uma: quem não tiver tradução aparece em inglês, e não
// quebrado.
// O inglês de reserva da própria casca. Estas frases apareciam cruas no meio
// de uma tela em português -- "This screen failed to load" é justamente o que
// alguém lê no pior momento possível.
setStrings('en', {
  'shell.stream': 'Event stream',
  'shell.theme': 'Theme',
  'shell.viewFailed': 'This screen failed to load',
  'shell.viewCrashed': 'This screen crashed while rendering',
  'shell.appFailed': '{name} failed to start',
  'shell.appError': 'The app reported an error.',
}, {activate: false});

setStrings('pt-BR', PT_BR);

const LS_MODE = 'project_os.mode';
const LS_THEME = 'project_os.theme';
const LS_ACCENT = 'project_os.accent';
const LS_DOCK = 'project_os.dockTerminal';

const store = createStore({
  ready: false,
  booting: true,
  health: null,
  user: null,
  authenticated: false,
  setupRequired: false,
  config: null,
  mode: 'simple',
  theme: 'auto',
  stats: null,
  apps: [],
  devices: [],
  suggestions: [],
  wsState: {status: 'idle'},
  title: 'project-os',
});

let ws = null;
let router = null;
let shellEl = null;
let contentEl = null;
let navEl = null;
let titleEl = null;
let pillsEl = null;
let currentCleanup = null;
let bootToken = 0;

const root = document.getElementById('app');

/* ------------------------------------------------------------ utilities */

function pick(obj, keys, fallback) {
  if (!obj || typeof obj !== 'object') return fallback;
  for (const key of keys) {
    if (obj[key] !== undefined && obj[key] !== null) return obj[key];
  }
  return fallback;
}

function dig(obj, path, fallback) {
  if (!obj) return fallback;
  const parts = String(path).split('.');
  let node = obj;
  for (const part of parts) {
    if (node === null || node === undefined || typeof node !== 'object') return fallback;
    node = node[part];
  }
  return node === undefined || node === null ? fallback : node;
}

function readLocal(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (err) {
    return fallback;
  }
}

function writeLocal(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (err) {
    /* private mode: preference simply does not persist */
  }
}

/** Normalise /api/system/stats (or a system.stats event) into what the UI
 *  renders. Everything is optional — the Pi may report no temperature. */
export function readStats(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  const mem = s.memory || s.mem || {};
  const swap = s.swap || {};
  let disks = s.disk || s.disks || [];
  if (disks && !Array.isArray(disks)) disks = [disks];
  const rootDisk = disks.find((d) => d && (d.mountpoint === '/' || d.mount === '/')) || disks[0] || null;

  const cpu = pick(s, ['cpu_percent', 'cpu_pct'], typeof s.cpu === 'number' ? s.cpu : dig(s, 'cpu.percent', null));
  const memPercent = pick(mem, ['percent', 'used_percent'],
    mem.total ? (Number(mem.used) / Number(mem.total)) * 100 : null);
  const diskPercent = rootDisk
    ? pick(rootDisk, ['percent', 'used_percent'],
      rootDisk.total ? (Number(rootDisk.used) / Number(rootDisk.total)) * 100 : null)
    : null;

  return {
    cpu: cpu === null || cpu === undefined ? null : Number(cpu),
    cores: s.per_core || s.cpu_per_core || (Array.isArray(s.cores) ? s.cores : null),
    memPercent: memPercent === null || memPercent === undefined ? null : Number(memPercent),
    memUsed: Number(pick(mem, ['used'], NaN)),
    memTotal: Number(pick(mem, ['total'], NaN)),
    swapPercent: swap && swap.total ? Number(pick(swap, ['percent'], (Number(swap.used) / Number(swap.total)) * 100)) : null,
    temp: (() => {
      const value = pick(s, ['temp_c', 'temperature_c', 'temp'], dig(s, 'temperature.cpu', null));
      return value === null || value === undefined ? null : Number(value);
    })(),
    disks,
    disk: rootDisk,
    diskPercent: diskPercent === null || diskPercent === undefined ? null : Number(diskPercent),
    load: Array.isArray(s.load) ? s.load : (s.load_avg || null),
    net: s.net || s.network || null,
    uptime: Number(pick(s, ['uptime', 'uptime_seconds'], NaN)),
  };
}

function levelClass(value, {warn = 75, danger = 90} = {}) {
  if (value === null || value === undefined || !isFinite(value)) return '';
  if (value >= danger) return 'danger';
  if (value >= warn) return 'warn';
  return '';
}

/* --------------------------------------------------------------- theming */

function resolveTheme(pref) {
  if (pref === 'light' || pref === 'dark') return pref;
  const light = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  return light ? 'light' : 'dark';
}

function applyTheme(pref) {
  const el = document.documentElement;
  el.setAttribute('data-theme', resolveTheme(pref));
  el.setAttribute('data-theme-pref', pref);
  store.set({theme: pref});
  writeLocal(LS_THEME, pref);
}

function applyAccent(accent) {
  if (!accent || typeof accent !== 'string') return;
  document.documentElement.style.setProperty('--accent', accent);
  writeLocal(LS_ACCENT, accent);
}

if (window.matchMedia) {
  const media = window.matchMedia('(prefers-color-scheme: light)');
  const onSchemeChange = () => {
    if (store.get('theme') === 'auto') applyTheme('auto');
  };
  if (media.addEventListener) media.addEventListener('change', onSchemeChange);
  else if (media.addListener) media.addListener(onSchemeChange);
}

function cycleTheme() {
  const order = ['auto', 'dark', 'light'];
  const next = order[(order.indexOf(store.get('theme')) + 1) % order.length];
  applyTheme(next);
  renderTopbarControls();
}

/* ------------------------------------------------------------------ mode */

function setMode(mode) {
  const next = mode === 'advanced' ? 'advanced' : 'simple';
  if (store.get('mode') === next) return;
  store.set({mode: next});
  writeLocal(LS_MODE, next);
  renderNav();
  renderTopbarControls();
  if (router) router.reload();
}

/* ------------------------------------------------------------------ nav */

const NAV_SIMPLE = [
  {key: 'dashboard', href: '#/', icon: 'dashboard', label: 'nav.dashboard'},
  {key: 'apps', href: '#/apps', icon: 'apps', label: 'nav.apps'},
  {key: 'devices', href: '#/devices', icon: 'devices', label: 'nav.devices'},
  {key: 'settings', href: '#/settings', icon: 'settings', label: 'nav.settings'},
];

const NAV_ADVANCED = [
  {key: 'system', href: '#/system', icon: 'system', label: 'nav.system'},
  // Right under System, not buried at the end: installing software is the
  // thing Advanced mode is *for*, not an extra panel.
  {key: 'packages', href: '#/packages', icon: 'apps', label: 'nav.packages'},
  {key: 'updates', href: '#/updates', icon: 'download', label: 'nav.updates'},
  {key: 'services', href: '#/services', icon: 'services', label: 'nav.services'},
  {key: 'files', href: '#/files', icon: 'files', label: 'nav.files'},
  {key: 'terminal', href: '#/terminal', icon: 'terminal', label: 'nav.terminal'},
  {key: 'logs', href: '#/logs', icon: 'logs', label: 'nav.logs'},
  {key: 'helpers', href: '#/helpers', icon: 'plug', label: 'nav.helpers'},
  {key: 'tuning', href: '#/tuning', icon: 'chip', label: 'nav.tuning'},
];

function appNavEntries() {
  const apps = store.get('apps') || [];
  return apps
    .filter((app) => app && app.id && app.enabled !== false && app.state !== 'disabled')
    .map((app) => ({
      key: 'app:' + app.id,
      href: '#/apps/' + encodeURIComponent(app.id),
      icon: app.icon,
      isApp: true,
      text: app.name || app.id,
      state: app.state,
    }));
}

function navItem(entry, activeKey) {
  const active = entry.key === activeKey;
  const dotClass = entry.state === 'error'
    ? 'nav__dot nav__dot--error'
    : entry.state === 'running' || entry.state === 'started'
      ? 'nav__dot nav__dot--on'
      : null;
  return h('a', {
    class: 'nav__item' + (active ? ' is-active' : '') + (entry.isApp ? ' nav__item--app' : ''),
    href: entry.href,
    'aria-current': active ? 'page' : null,
  },
    entry.isApp ? appIcon(entry.icon, {size: 19}) : icon(entry.icon, {size: 19}),
    h('span', {class: 'nav__text'}, entry.text || t(entry.label)),
    dotClass ? h('span', {class: dotClass, title: entry.state}) : null,
  );
}

function activeNavKey() {
  const current = router ? router.current() : {path: '/'};
  const path = current.path || '/';
  if (path === '/' || path === '/dashboard') return 'dashboard';
  const appMatch = path.match(/^\/apps\/([^/]+)/);
  if (appMatch) return 'app:' + decodeURIComponent(appMatch[1]);
  const first = path.split('/')[1];
  return first || 'dashboard';
}

function renderNav() {
  if (!navEl) return;
  const mode = store.get('mode');
  const activeKey = activeNavKey();
  const apps = appNavEntries();
  const children = [];

  for (const entry of NAV_SIMPLE) {
    children.push(navItem(entry, activeKey));
    if (entry.key === 'apps' && apps.length) {
      for (const app of apps) children.push(navItem(app, activeKey));
    }
  }
  if (mode === 'advanced') {
    children.push(h('div', {class: 'nav__label'}, t('nav.system')));
    for (const entry of NAV_ADVANCED) children.push(navItem(entry, activeKey));
  }
  mount(navEl, children);
}

/* --------------------------------------------------------------- topbar */

function pill(name, label, value, extraClass) {
  return h('span', {class: 'pill' + (extraClass ? ' pill--' + extraClass : '') + (name === 'thermometer' || name === 'disk' ? ' pill--optional' : '')},
    icon(name, {size: 14}),
    h('span', {class: 'sr-only'}, label + ': '),
    h('span', {class: 'pill__value'}, value),
  );
}

function renderPills() {
  if (!pillsEl) return;
  const stats = readStats(store.get('stats'));
  const items = [];
  if (stats.cpu !== null && isFinite(stats.cpu)) {
    items.push(pill('cpu', t('unit.cpu'), fmt.percent(stats.cpu), levelClass(stats.cpu)));
  }
  if (stats.memPercent !== null && isFinite(stats.memPercent)) {
    items.push(pill('chip', t('unit.ram'), fmt.percent(stats.memPercent), levelClass(stats.memPercent)));
  }
  if (stats.temp !== null && isFinite(stats.temp)) {
    items.push(pill('thermometer', t('unit.temp'), fmt.temperature(stats.temp, {digits: 0}), levelClass(stats.temp, {warn: 70, danger: 80})));
  }
  if (stats.diskPercent !== null && isFinite(stats.diskPercent)) {
    items.push(pill('disk', t('unit.disk'), fmt.percent(stats.diskPercent), levelClass(stats.diskPercent, {warn: 85, danger: 93})));
  }
  if (!items.length) {
    items.push(h('span', {class: 'pill'}, h('span', {class: 'pill__value faint'}, '—')));
  }
  mount(pillsEl, items);
}

let controlsEl = null;

function renderTopbarControls() {
  if (!controlsEl) return;
  const mode = store.get('mode');
  const theme = store.get('theme');
  const wsState = store.get('wsState') || {};
  const themeIcon = theme === 'light' ? 'sun' : theme === 'dark' ? 'moon' : 'system';

  mount(controlsEl, [
    h('div', {class: 'segmented', role: 'group', 'aria-label': t('mode.label')},
      h('button', {
        class: 'segmented__btn', type: 'button', 'aria-pressed': String(mode === 'simple'),
        onClick: () => setMode('simple'),
      }, t('mode.simple')),
      h('button', {
        class: 'segmented__btn', type: 'button', 'aria-pressed': String(mode === 'advanced'),
        onClick: () => setMode('advanced'),
      }, t('mode.advanced')),
    ),
    h('span', {
      class: 'conn', dataset: {state: wsState.status || 'idle'},
      title: t('shell.stream') + ': ' + t('state.' + (wsState.status === 'open' ? 'live' : wsState.status === 'connecting' ? 'connecting' : 'offline')),
    },
      h('span', {class: 'conn__dot'}),
      h('span', {class: 'sr-only'}, t('shell.stream') + ' ' + (wsState.status || 'idle')),
    ),
    h('button', {
      class: 'btn btn--icon', type: 'button',
      title: t('shell.theme') + ': ' + t('theme.' + theme),
      'aria-label': t('shell.theme') + ': ' + t('theme.' + theme),
      onClick: cycleTheme,
    }, icon(themeIcon, {size: 18})),
    h('button', {
      class: 'btn btn--icon', type: 'button',
      title: t('action.logout'), 'aria-label': t('action.logout'),
      onClick: logout,
    }, icon('power', {size: 18})),
  ]);
}

async function logout() {
  try {
    await api.post('/auth/logout', {}, {redirectOnAuth: false});
  } catch (err) {
    /* logging out locally is still the right outcome */
  }
  if (ws) ws.close();
  ws = null;
  // The dock outlives navigation by design, so signing out has to take it down
  // explicitly -- otherwise a live shell stays pinned over the login screen.
  closeDock();
  store.set({user: null, authenticated: false});
  navigate('#/login', {replace: true});
  boot();
}

// A session can end without anyone clicking anything: it expires, or the box
// restarts while the page is open. api.js sees the 401 first and says so here.
// Without this the store kept claiming the browser was signed in, the login
// route sent it back to the dashboard, and the dashboard's poll asked again --
// a request storm against a Pi, for as long as the tab stayed open.
window.addEventListener('project-os:unauthenticated', () => {
  if (!store.get('authenticated')) return;
  if (ws) { try { ws.close(); } catch (err) { /* already gone */ } }
  ws = null;
  closeDock();
  store.set({user: null, authenticated: false});
});

/* ---------------------------------------------------------------- shell */

function buildShell() {
  navEl = h('nav', {class: 'nav', 'aria-label': 'Main'});
  titleEl = h('h1', {class: 'topbar__title'}, 'project-os');
  pillsEl = h('div', {class: 'pills', 'aria-label': 'System load'});
  controlsEl = h('div', {class: 'topbar__group'});
  contentEl = h('main', {class: 'content', id: 'view', tabindex: '-1'});

  shellEl = h('div', {class: 'shell'},
    h('aside', {class: 'sidebar'},
      h('a', {class: 'brand', href: '#/'},
        h('span', {class: 'brand__mark'}, icon('chip', {size: 18})),
        h('span', {class: 'brand__text'},
          h('span', {class: 'brand__name'}, boxName()),
          h('span', {class: 'brand__sub'}, dig(store.get('health'), 'version', '') || ''),
        ),
      ),
      navEl,
      h('div', {class: 'sidebar__footer'},
        h('a', {class: 'nav__item', href: '#/store'},
          icon('download', {size: 19}),
          h('span', {class: 'nav__text'}, t('nav.store')),
        ),
      ),
    ),
    h('div', {class: 'main'},
      h('header', {class: 'topbar'},
        titleEl,
        pillsEl,
        controlsEl,
      ),
      contentEl,
    ),
  );

  mount(root, shellEl);
  root.className = '';
  renderNav();
  renderTopbarControls();
  renderPills();
}

function ensureShell() {
  if (shellEl && shellEl.isConnected) return;
  buildShell();
}

function useAuthLayout() {
  shellEl = null;
  navEl = null;
  titleEl = null;
  pillsEl = null;
  controlsEl = null;
  contentEl = h('main', {class: 'auth'});
  root.className = '';
  mount(root, contentEl);
}

/** What this box is called: the name someone gave it, or the product name.
 *
 * Settings has always had an "Instance name" field, and its own hint admitted
 * the truth -- "Shown nowhere else yet -- this is not read by any other
 * screen". A control that changes nothing is worse than no control. It belongs
 * exactly here: the sidebar and the tab title, the two places you look when you
 * have more than one of these on the network.
 */
function boxName() {
  const named = dig(store.get('config'), 'ui.instance_name', '');
  return (typeof named === 'string' && named.trim()) || 'project-os';
}

function setTitle(title) {
  const box = boxName();
  store.set({title: title || box});
  document.title = title && title !== box ? title + ' · ' + box : box;
  if (titleEl) mount(titleEl, title || box);
}

/* ----------------------------------------------------------- view states */

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

function loadingGrid(count = 3) {
  const cards = [];
  for (let i = 0; i < count; i += 1) cards.push(skeletonCard());
  return h('div', {class: 'grid'}, cards);
}

function errorCard(title, error, onRetry) {
  const message = error instanceof Error ? error.message : String(error || t('state.error'));
  const code = error && error.code ? error.code : null;
  return h('div', {class: 'card card--danger'},
    h('div', {class: 'card__header'},
      h('span', {class: 'card__icon danger'}, icon('warning', {size: 18})),
      h('div', {class: 'grow'},
        h('div', {class: 'card__title'}, title),
        code ? h('div', {class: 'card__sub mono'}, code) : null,
      ),
    ),
    h('div', {class: 'card__body'},
      h('p', {class: 'small muted'}, message),
      onRetry
        ? h('div', {class: 'row'},
            h('button', {class: 'btn btn--sm', type: 'button', onClick: onRetry},
              icon('refresh', {size: 15}), t('action.retry')),
          )
        : null,
    ),
  );
}

/* ------------------------------------------------------------- view host */

const VIEW_LOADERS = {
  dashboard: () => import('./views/dashboard.js'),
  apps: () => import('./views/apps.js'),
  devices: () => import('./views/devices.js'),
  settings: () => import('./views/settings.js'),
  store: () => import('./views/store.js'),
  system: () => import('./views/system.js'),
  services: () => import('./views/services.js'),
  files: () => import('./views/files.js'),
  terminal: () => import('./views/terminal.js'),
  logs: () => import('./views/logs.js'),
  packages: () => import('./views/packages.js'),
  updates: () => import('./views/updates.js'),
  helpers: () => import('./views/helpers.js'),
  tuning: () => import('./views/tuning.js'),
  login: () => import('./views/login.js'),
  setup: () => import('./views/setup.js'),
};

function normalizeView(mod) {
  const candidate = mod && mod.default !== undefined ? mod.default : mod;
  if (!candidate) return null;
  if (typeof candidate === 'function') return {mount: candidate};
  if (typeof candidate.mount === 'function') return candidate;
  if (typeof candidate.render === 'function') return {mount: candidate.render, title: candidate.title};
  if (mod && typeof mod.mount === 'function') return {mount: mod.mount, title: mod.title};
  return null;
}

async function runCleanup() {
  const fn = currentCleanup;
  currentCleanup = null;
  if (typeof fn !== 'function') return;
  try {
    await fn();
  } catch (err) {
    console.error('[shell] view cleanup failed', err);
  }
}

function baseContext(extra) {
  return Object.assign({
    api,
    apiFor,
    ApiError,
    ws,
    store,
    fmt,
    t,
    h,
    icon,
    toast,
    confirm,
    navigate,
    router,
    mode: store.get('mode'),
    user: store.get('user'),
    health: store.get('health'),
    config: store.get('config'),
    readStats,
    setTitle,
    reload: boot,
    ui: {loadingGrid, skeletonCard, errorCard},
  }, extra || {});
}

async function showView(name, routeCtx, {layout = 'shell', title = ''} = {}) {
  const token = ++bootToken;
  await runCleanup();
  if (layout === 'auth') useAuthLayout();
  else ensureShell();
  if (token !== bootToken) return;

  setTitle(title || (name ? fmt.humanize(name) : 'project-os'));
  renderNav();

  mount(contentEl, layout === 'auth'
    ? h('div', {class: 'auth__card'}, h('div', {class: 'spinner', role: 'status', 'aria-label': t('state.loading')}))
    : loadingGrid(3));

  let view = null;
  try {
    const loader = VIEW_LOADERS[name];
    if (!loader) throw new Error('No view registered for "' + name + '"');
    view = normalizeView(await loader());
    if (!view) throw new Error('views/' + name + '.js does not export a mount() function');
  } catch (err) {
    if (token !== bootToken) return;
    console.error('[shell] failed to load view', name, err);
    mount(contentEl, layout === 'auth'
      ? h('div', {class: 'auth__card'}, errorCard(t('shell.viewFailed'), err, () => router.reload()))
      : errorCard(t('shell.viewFailed'), err, () => router.reload()));
    return;
  }
  if (token !== bootToken) return;

  if (view.title) setTitle(view.title);

  const ctx = baseContext({
    params: routeCtx ? routeCtx.params : {},
    query: routeCtx ? routeCtx.query : {},
    route: routeCtx ? routeCtx.route : null,
    view: name,
  });

  clear(contentEl);
  try {
    const result = await view.mount(contentEl, ctx);
    if (token !== bootToken) {
      if (typeof result === 'function') result();
      return;
    }
    if (typeof result === 'function') currentCleanup = result;
    else if (result instanceof Node) contentEl.appendChild(result);
  } catch (err) {
    if (token !== bootToken) return;
    console.error('[shell] view crashed', name, err);
    mount(contentEl, errorCard(t('shell.viewCrashed'), err, () => router.reload()));
  }
  window.scrollTo(0, 0);
}

/* ---------------------------------------------------------- app  panels */

function appConfigHelper(appId) {
  let cache = {};
  const helper = {
    all() {
      return cache;
    },
    get(path, fallback) {
      return dig(cache, path, fallback);
    },
    async reload() {
      const raw = await api.get('/apps/' + encodeURIComponent(appId) + '/config');
      cache = raw && raw.config && typeof raw.config === 'object' ? raw.config : (raw || {});
      return cache;
    },
    /** set('output.volume', 0.4) -> PUT {"output.volume": 0.4} (same shape as
     *  PUT /api/settings). Returns the server's new config. */
    async set(path, value) {
      const body = {};
      body[path] = value;
      return helper.save(body);
    },
    async save(patch) {
      const raw = await api.put('/apps/' + encodeURIComponent(appId) + '/config', patch);
      if (raw && typeof raw === 'object') {
        cache = raw.config && typeof raw.config === 'object' ? raw.config : raw;
      }
      return cache;
    },
  };
  return helper;
}

function ensurePanelStyles(appId, app) {
  const id = 'app-style-' + appId;
  const existing = document.getElementById(id);
  const stylePath = dig(app, 'manifest.ui.styles', dig(app, 'ui.styles', null));
  if (!stylePath) {
    if (existing) existing.remove();
    return null;
  }
  const file = String(stylePath).split('/').pop();
  const href = '/api/apps/' + encodeURIComponent(appId) + '/ui/' + file +
    (app && app.version ? '?v=' + encodeURIComponent(app.version) : '');
  if (existing) {
    if (existing.getAttribute('href') !== href) existing.setAttribute('href', href);
    return existing;
  }
  const link = h('link', {rel: 'stylesheet', href, id});
  document.head.appendChild(link);
  return link;
}

async function showAppPanel(routeCtx) {
  const appId = routeCtx.params.id;
  const token = ++bootToken;
  await runCleanup();
  ensureShell();
  if (token !== bootToken) return;

  setTitle(appId);
  renderNav();
  mount(contentEl, loadingGrid(2));

  let app = null;
  try {
    app = await api.get('/apps/' + encodeURIComponent(appId));
    if (app && app.app && typeof app.app === 'object') app = app.app;
  } catch (err) {
    if (token !== bootToken) return;
    mount(contentEl, errorCard('Could not load "' + appId + '"', err, () => router.reload()));
    return;
  }
  if (token !== bootToken) return;

  const name = (app && (app.name || dig(app, 'manifest.name', null))) || appId;
  setTitle(name);

  if (app && app.state === 'error') {
    const detail = app.error || dig(app, 'traceback', '');
    mount(contentEl, h('div', {class: 'stack'},
      errorCard(t('shell.appFailed', {name}), new Error(app.message || t('shell.appError')), () => router.reload()),
      detail ? h('pre', {class: 'code'}, String(detail)) : null,
    ));
    return;
  }

  const styleLink = ensurePanelStyles(appId, app);
  const version = app && (app.version || dig(app, 'manifest.version', '')) ? (app.version || dig(app, 'manifest.version', '')) : '';
  // manifest.ui.panel is "web/panel.js"; /api/apps/<id>/ui/ already maps to web/.
  const panelFile = String(dig(app, 'manifest.ui.panel', dig(app, 'ui.panel', 'panel.js'))).split('/').pop() || 'panel.js';
  const panelUrl = '/api/apps/' + encodeURIComponent(appId) + '/ui/' + panelFile + (version ? '?v=' + encodeURIComponent(version) : '');

  let panel = null;
  try {
    panel = normalizeView(await import(/* @vite-ignore */ panelUrl));
    if (!panel) throw new Error('panel.js must default-export an object with mount(root, ctx)');
  } catch (err) {
    if (token !== bootToken) return;
    console.error('[shell] app panel failed to load', appId, err);
    mount(contentEl, h('div', {class: 'stack'},
      errorCard('The ' + name + ' panel could not be loaded', err, () => router.reload()),
      h('p', {class: 'small faint'},
        'The app is still running — only its browser panel failed. Check ',
        h('a', {href: '#/logs'}, 'the logs'), ' for details.'),
    ));
    return;
  }
  if (token !== bootToken) return;

  const appApi = apiFor('/api/apps/' + encodeURIComponent(appId));
  const config = appConfigHelper(appId);
  try {
    await config.reload();
  } catch (err) {
    /* a panel can still render without its config */
  }
  if (token !== bootToken) return;

  const ctx = baseContext({
    app,
    appId,
    appApi,
    config,
    params: routeCtx.params,
    query: routeCtx.query,
    route: routeCtx.route,
    view: 'app:' + appId,
  });

  clear(contentEl);
  const host = h('div', {class: 'app-panel', dataset: {app: appId}});
  contentEl.appendChild(host);

  try {
    const result = await panel.mount(host, ctx);
    const cleanupPanel = () => {
      if (typeof result === 'function') {
        try {
          result();
        } catch (err) {
          console.error('[shell] panel cleanup failed', err);
        }
      }
      if (styleLink && styleLink.parentNode) styleLink.parentNode.removeChild(styleLink);
    };
    if (token !== bootToken) {
      cleanupPanel();
      return;
    }
    currentCleanup = cleanupPanel;
    if (result instanceof Node) host.appendChild(result);
  } catch (err) {
    if (token !== bootToken) return;
    console.error('[shell] app panel crashed', appId, err);
    mount(contentEl, errorCard('The ' + name + ' panel crashed', err, () => router.reload()));
  }
}

/* ----------------------------------------------------------------- data */

function upsertById(list, item) {
  if (!item || !item.id) return list;
  const next = list.slice();
  const index = next.findIndex((row) => row && row.id === item.id);
  if (index === -1) next.push(item);
  else next[index] = Object.assign({}, next[index], item);
  return next;
}

async function refreshApps() {
  try {
    const raw = await api.get('/apps', {redirectOnAuth: false});
    const apps = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.apps) ? raw.apps : []);
    store.set({apps});
    renderNav();
    return apps;
  } catch (err) {
    if (err && err.status !== 401) console.warn('[shell] could not list apps', err.message);
    return [];
  }
}

async function refreshStats() {
  try {
    const raw = await api.get('/system/stats', {redirectOnAuth: false});
    store.set({stats: raw});
  } catch (err) {
    /* the ws feed is the primary source; a failure here is not fatal */
  }
}

function wireEvents() {
  if (!ws) return;
  ws.state.subscribe((state) => {
    store.set({wsState: state});
    renderTopbarControls();
  });
  ws.on('system.stats', (data) => store.set({stats: data}));
  ws.on('device.found', (data) => store.set({devices: upsertById(store.get('devices') || [], data)}));
  ws.on('device.updated', (data) => store.set({devices: upsertById(store.get('devices') || [], data)}));
  ws.on('device.lost', (data) => {
    if (!data || !data.id) return;
    store.set({devices: (store.get('devices') || []).map((d) => (d.id === data.id ? Object.assign({}, d, {online: false, lost: true}) : d))});
  });
  ws.on('app.state', (data) => {
    if (!data || !data.id) return;
    store.set({apps: upsertById(store.get('apps') || [], data)});
    renderNav();
  });
  ws.on('suggestion.new', (data) => {
    if (!data || !data.id) return;
    const current = store.get('suggestions') || [];
    if (current.some((s) => s.id === data.id)) return;
    store.set({suggestions: [data].concat(current)});
  });
  ws.on('notify', (data) => {
    if (!data) return;
    const message = data.message || data.text || '';
    if (!message) return;
    const level = data.level || data.type || 'info';
    toast(message, {type: level === 'warning' ? 'warn' : level, title: data.title || ''});
  });
}

store.subscribe((next, prev) => {
  if (!Object.is(next.stats, prev.stats)) renderPills();
  // Renaming the box in Settings has to be visible without a reload.
  if (!Object.is(next.config, prev.config) && shellEl) {
    const brand = shellEl.querySelector('.brand__name');
    if (brand) mount(brand, boxName());
    setTitle(store.get('title'));
  }
});

/* --------------------------------------------------------------- routes */

function buildRouter() {
  const r = createRouter({base: '#/'});

  const shellRoute = (name, title) => (routeCtx) => {
    if (!guard(routeCtx)) return;
    return showView(name, routeCtx, {title});
  };

  r.addAll({
    '#/': shellRoute('dashboard', t('nav.dashboard')),
    '#/dashboard': shellRoute('dashboard', t('nav.dashboard')),
    '#/apps': shellRoute('apps', t('nav.apps')),
    '#/devices': shellRoute('devices', t('nav.devices')),
    '#/devices/:id': shellRoute('devices', t('nav.devices')),
    '#/settings': shellRoute('settings', t('nav.settings')),
    '#/settings/:section': shellRoute('settings', t('nav.settings')),
    '#/store': shellRoute('store', t('nav.store')),
    '#/store/:id': shellRoute('store', t('nav.store')),
    '#/system': shellRoute('system', t('nav.system')),
    '#/services': shellRoute('services', t('nav.services')),
    '#/files': shellRoute('files', t('nav.files')),
    '#/terminal': shellRoute('terminal', t('nav.terminal')),
    '#/logs': shellRoute('logs', t('nav.logs')),
    '#/helpers': shellRoute('helpers', t('nav.helpers')),
    '#/tuning': shellRoute('tuning', t('nav.tuning')),
    '#/packages': shellRoute('packages', t('nav.packages')),
    '#/updates': shellRoute('updates', t('nav.updates')),
  });

  r.add('#/apps/:id', (routeCtx) => {
    if (!guard(routeCtx)) return;
    return showAppPanel(routeCtx);
  });

  r.add('#/login', (routeCtx) => {
    if (store.get('setupRequired')) {
      navigate('#/setup', {replace: true});
      return;
    }
    if (store.get('authenticated')) {
      navigate('#/', {replace: true});
      return;
    }
    return showView('login', routeCtx, {layout: 'auth', title: t('login.title')});
  });

  r.add('#/setup', (routeCtx) => {
    if (!store.get('setupRequired')) {
      // The box already has its account. Being signed out is the normal way to
      // arrive here -- an old bookmark, a browser that kept #/setup in the URL
      // -- and it used to end at the wizard, which could only answer "already
      // has a user". A machine that is set up sends you to the door, not to the
      // form for building one.
      navigate(store.get('authenticated') ? '#/' : '#/login', {replace: true});
      return;
    }
    return showView('setup', routeCtx, {layout: 'auth', title: t('setup.title')});
  });

  r.notFound((routeCtx) => {
    if (!guard(routeCtx)) return;
    // Invalidate any view mount still in flight so it cannot paint over this.
    bootToken += 1;
    ensureShell();
    runCleanup();
    setTitle('Not found');
    renderNav();
    mount(contentEl, h('div', {class: 'empty'},
      h('span', {class: 'empty__icon'}, icon('search', {size: 20})),
      h('p', {class: 'empty__title'}, 'Nothing at ' + routeCtx.path),
      h('p', {class: 'empty__text'}, 'That page does not exist in this build of project-os.'),
      h('a', {class: 'btn btn--primary', href: '#/'}, t('nav.dashboard')),
    ));
  });

  r.onChange(() => {
    if (navEl) renderNav();
  });

  return r;
}

function guard(routeCtx) {
  if (store.get('setupRequired')) {
    navigate('#/setup', {replace: true});
    return false;
  }
  if (!store.get('authenticated')) {
    navigate('#/login', {replace: true});
    return false;
  }
  return true;
}

/* ----------------------------------------------------------------- boot */

function fatal(error) {
  root.className = '';
  const message = error instanceof Error ? error.message : String(error);
  mount(root, h('div', {class: 'auth'},
    h('div', {class: 'auth__card'},
      h('div', {class: 'auth__brand'},
        h('span', {class: 'auth__mark'}, icon('warning', {size: 22})),
        h('h1', {class: 'auth__title'}, 'project-os is not responding'),
      ),
      h('p', {class: 'auth__lead'}, message),
      h('button', {class: 'btn btn--primary btn--block', type: 'button', onClick: () => boot()},
        icon('refresh', {size: 16}), t('action.retry')),
    ),
  ));
}

function normalizeUser(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.user && typeof raw.user === 'object') return raw.user;
  if (raw.username || raw.id !== undefined) return raw;
  return null;
}

function normalizeConfig(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (raw.config && typeof raw.config === 'object') return raw.config;
  if (raw.settings && typeof raw.settings === 'object') return raw.settings;
  return raw;
}

export async function boot() {
  const token = ++bootToken;
  await runCleanup();

  if (ws) {
    ws.close();
    ws = null;
  }

  // Reset to the boot splash only on a cold start; a re-boot after login keeps
  // whatever is on screen until the new layout is ready.
  if (!store.get('ready')) {
    root.className = 'boot';
    mount(root, h('div', {class: 'boot__inner'},
      h('div', {class: 'spinner', role: 'status', 'aria-label': t('state.loading')}),
    ));
  }

  let health;
  try {
    health = await api.get('/health', {redirectOnAuth: false});
  } catch (err) {
    if (token !== bootToken) return;
    fatal(err instanceof ApiError ? err.message : String(err));
    return;
  }
  if (token !== bootToken) return;

  const setupRequired = Boolean(pick(health, ['setup_required', 'setupRequired', 'needs_setup'], false));
  const authEnabled = pick(health, ['auth_enabled', 'authEnabled'], true) !== false;
  store.set({health, setupRequired});

  let user = null;
  let authenticated = false;
  if (!setupRequired) {
    if (!authEnabled) {
      authenticated = true;
      user = {username: 'local', anonymous: true};
    } else {
      try {
        user = normalizeUser(await api.get('/auth/me', {redirectOnAuth: false}));
        authenticated = Boolean(user);
      } catch (err) {
        if (err && err.status && err.status !== 401 && err.status !== 428) {
          console.warn('[shell] /auth/me failed', err.message);
        }
        authenticated = false;
      }
    }
  }
  if (token !== bootToken) return;
  store.set({user, authenticated});

  let config = null;
  if (authenticated) {
    try {
      config = normalizeConfig(await api.get('/settings', {redirectOnAuth: false}));
    } catch (err) {
      /* settings are a nicety at boot; defaults keep the UI usable */
    }
  }
  if (token !== bootToken) return;
  store.set({config});

  // UI preferences: browser choice wins, server config is the default.
  const defaultMode = dig(config, 'ui.default_mode', 'simple');
  const storedMode = readLocal(LS_MODE, null);
  store.set({mode: storedMode === 'advanced' || storedMode === 'simple' ? storedMode : (defaultMode === 'advanced' ? 'advanced' : 'simple')});

  const storedTheme = readLocal(LS_THEME, null);
  applyTheme(storedTheme || dig(config, 'ui.theme', 'auto'));
  applyAccent(dig(config, 'ui.accent', readLocal(LS_ACCENT, null)));

  store.set({ready: true, booting: false});

  if (authenticated) {
    ws = connect();
    wireEvents();
    refreshApps();
    refreshStats();
  }
  restoreDock();

  // The route guards do the redirecting (unauthenticated -> #/login,
  // setup pending -> #/setup, already signed in -> away from #/login), so the
  // shell only has to (re)dispatch the current hash.
  if (!router) {
    router = buildRouter();
    router.start();
  } else {
    router.reload();
  }
}

/* ------------------------------------------------------- docked terminal */

// *"nas configuracoes, em configurações do dev, adiciona um botao pra adicioanr
// o terminal ali no canto, q serve pra qualquer coisa ne, baixa apps e etc"*
//
// The switch lives in Settings, which cannot reach into the shell, so it asks
// by dispatching `project_os:dock-terminal` on window. Keeping the dock here is
// what makes it survive navigation: it is a sibling of the router's content
// area, so changing screens never unmounts it -- which is the whole point of
// pinning a terminal to a corner.
//
// The terminal module is only imported when the dock is first opened. Somebody
// who never turns it on never downloads it.

let dock = null;

async function openDock() {
  if (dock) return dock;
  const pending = {closed: false};
  dock = pending;
  let createTerminal;
  try {
    ({createTerminal} = await import('./views/terminal.js'));
  } catch (err) {
    dock = null;
    toast('Could not load the terminal', {kind: 'error'});
    return null;
  }
  if (pending.closed) {  // switched off again while the module was loading
    dock = null;
    return null;
  }

  const term = createTerminal({maxLines: 500});
  const panel = h('aside', {class: 'dock', 'aria-label': t('nav.terminal')},
    h('div', {class: 'dock__bar'},
      icon('terminal', {size: 15}),
      h('span', {class: 'dock__title'}, t('nav.terminal')),
      h('span', {class: 'grow'}),
      h('button', {
        class: 'btn btn--icon btn--sm', type: 'button', title: t('nav.terminal'),
        onclick: () => navigate('#/terminal'),
      }, icon('link', {size: 14})),
      h('button', {
        class: 'btn btn--icon btn--sm', type: 'button', title: t('action.close'),
        onclick: () => setDock(false),
      }, icon('x', {size: 14})),
    ),
    h('div', {class: 'dock__body'}, term.element),
  );
  document.body.appendChild(panel);
  term.connect();
  dock = {panel, term, closed: false};
  return dock;
}

function closeDock() {
  const current = dock;
  dock = null;
  if (!current) return;
  current.closed = true;
  if (current.term) current.term.destroy();
  if (current.panel) current.panel.remove();
}

function setDock(on) {
  writeLocal(LS_DOCK, on ? '1' : '0');
  if (on) openDock();
  else closeDock();
}

window.addEventListener('project_os:dock-terminal', (event) => {
  setDock(!!(event.detail && event.detail.on));
});

/** Reopen the dock on load if it was left open, but never on the login screen:
 *  a terminal that renders before there is a session would only ever show a
 *  refusal. */
function restoreDock() {
  if (!store.get('authenticated')) {
    closeDock();
    return;
  }
  const stored = readLocal(LS_DOCK, null);
  const wanted = stored === null
    ? !!dig(store.get('config'), 'ui.dock_terminal', false)
    : stored === '1';
  if (wanted) openDock();
}

// Escape hatch for app panels and for debugging from the console.
window.projectOs = {
  store, api, apiFor, fmt, h, icon, toast, confirm, navigate, readStats,
  get ws() { return ws; },
  get router() { return router; },
  boot, setMode, applyTheme, refreshApps,
};

boot();

export {store, setMode, applyTheme, refreshApps, showView};
