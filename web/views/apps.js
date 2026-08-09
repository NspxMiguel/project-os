// views/apps.js — apps ProjectOS itself is running, plus whatever the machine
// already had lying around.
//
// Two independent sections, two independent API calls, two independent
// failure modes — one going down must never take the other with it.
//
//   1. Installed apps (GET /apps): ProjectOS ships with nothing at all, the
//      same way Home Assistant does, so an empty list here is the normal
//      first-run state. The empty state points at #/store instead of
//      apologising for it.
//
//   2. Detected software (GET /installed):
//
//        "adiciona tbm apps, detectados no proprio sistema, sla, o cara foi
//        la, foi pro modo advanced, instalo um firefox, um flatpack store, e
//        dps voltau pro modo simple. ai aparece em apps."
//
//      Read-only on the server on purpose — this section can point at the
//      store for something it recognises, but it never installs or removes
//      anything itself.
//
// `#/apps/:id` (an app's own panel) is a different screen entirely, wired up
// directly in app.js as `showAppPanel` — this view only links to it.

import {h, mount, clear} from '../lib/dom.js';
import {icon, hasIcon, appIcon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';

setStrings('en', {
  'apps.lead': 'What ProjectOS itself is running on this machine.',
  'apps.refresh': 'Refresh',
  'apps.empty.title': 'Nothing installed yet',
  'apps.empty.text': 'ProjectOS ships empty, on purpose — pick something from the store and it will show up here with its own card.',
  'apps.empty.action': 'Open the store',
  'apps.error.load': 'Could not load apps.',
  'apps.action.retry': 'Retry',
  'apps.action.start': 'Start',
  'apps.action.stop': 'Stop',
  'apps.action.restart': 'Restart',
  'apps.action.open': 'Open',
  'apps.action.uninstall': 'Uninstall',
  'apps.uninstall.confirm': 'Uninstall {name}? Its settings and data are kept, in case you install it again.',
  'apps.toast.uninstalled': '{name} uninstalled',
  'apps.toast.error': 'That did not work.',
  'apps.toast.action': '{action} · {name}',
  'apps.state.running': 'running',
  'apps.state.stopped': 'stopped',
  'apps.state.starting': 'starting',
  'apps.state.error': 'error',
  'apps.state.disabled': 'disabled',
  'apps.error.detail': 'This app failed to start. Check its logs from its own panel.',
  'detected.title': 'Also on this machine',
  'detected.lead': 'Software ProjectOS found already installed, outside of its own apps — apt, Flatpak, Snap, systemd services and containers.',
  'detected.refresh': 'Check again',
  'detected.refreshing': 'Checking…',
  'detected.cached': 'as of {time}',
  'detected.filter.all': 'All',
  'detected.filter.package': 'Packages',
  'detected.filter.app': 'Apps',
  'detected.filter.service': 'Services',
  'detected.filter.container': 'Containers',
  'detected.empty.title': 'Nothing else found',
  'detected.empty.text': 'ProjectOS checked apt, Flatpak, Snap, desktop entries, systemd services and containers, and this machine has nothing outside its own apps.',
  'detected.empty.filtered.title': 'Nothing matches this filter',
  'detected.empty.filtered.text': 'Try a different category, or clear the filter to see everything found.',
  'detected.error.load': 'Could not check what is installed on this machine.',
  'detected.col.name': 'Name',
  'detected.col.version': 'Version',
  'detected.col.source': 'Found via',
  'detected.col.store': 'Store',
  'detected.store.yes': 'In the store',
  'detected.also': '+{count} more',
  'detected.notChecked': 'Not checked: {list}',
}, {activate: false});

/* -------------------------------------------------------------- helpers */

function card(options) {
  const {title, sub, mark, iconName, tools, body, footer, variant = ''} = options;
  const markNode = mark || (iconName ? icon(iconName, {size: 18}) : null);
  return h('div', {class: 'card' + (variant ? ' card--' + variant : '')},
    h('div', {class: 'card__header'},
      markNode ? h('span', {class: 'card__icon'}, markNode) : null,
      h('div', {class: 'grow'},
        h('div', {class: 'card__title'}, title),
        sub ? h('div', {class: 'card__sub'}, sub) : null,
      ),
      tools ? h('div', {class: 'card__tools'}, tools) : null,
    ),
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
              icon('refresh', {size: 14}), t('apps.action.retry')),
          )
        : null,
    ),
  );
}

function emptyState(title, message, action) {
  return h('div', {class: 'empty'},
    h('span', {class: 'empty__icon'}, icon('apps', {size: 22})),
    h('p', {class: 'empty__title'}, title),
    message ? h('p', {class: 'empty__text'}, message) : null,
    action || null,
  );
}

const STATE_LEVEL = {running: 'ok', starting: 'info', error: 'danger', stopped: '', disabled: ''};

function stateBadge(app) {
  const appState = app.state || 'stopped';
  const level = STATE_LEVEL[appState] || '';
  return h('span', {class: 'badge' + (level ? ' badge--' + level : '')}, t('apps.state.' + appState));
}

/* ---------------------------------------------------------------- view */

export default {
  id: 'apps',
  title: t('nav.apps'),

  async mount(root, ctx) {
    const store = ctx.store;
    let disposed = false;

    const state = {
      appsPhase: store.get('apps') ? 'ready' : 'loading',
      appsError: null,
      busy: new Set(),
      installed: null,
      installedPhase: 'loading',
      installedError: null,
      detectedFilter: 'all',
      refreshingInstalled: false,
    };

    const appsSlot = h('div', {class: 'slot'});
    const detectedFilterSlot = h('div', {class: 'slot'});
    const detectedSlot = h('div', {class: 'slot'});

    const refreshBtn = h('button', {class: 'btn btn--sm', type: 'button', onClick: () => loadApps()},
      icon('refresh', {size: 15}), t('apps.refresh'));

    const detectedRefreshBtn = h('button', {class: 'btn btn--sm', type: 'button', onClick: () => loadDetected(true)},
      icon('refresh', {size: 15}), t('detected.refresh'));

    mount(root, [
      h('div', {class: 'page__header'},
        h('div', null,
          h('h2', null, t('nav.apps')),
          h('p', {class: 'page__lead'}, t('apps.lead')),
        ),
        h('div', {class: 'page__actions'}, refreshBtn),
      ),
      appsSlot,
      h('div', {class: 'page__header'},
        h('div', null,
          h('h3', null, t('detected.title')),
          h('p', {class: 'page__lead'}, t('detected.lead')),
        ),
        h('div', {class: 'page__actions'}, detectedRefreshBtn),
      ),
      detectedFilterSlot,
      detectedSlot,
    ]);

    /* --------------------------------------------------- installed apps */

    async function runAction(app, action, label) {
      const key = app.id + ':' + action;
      if (state.busy.has(key)) return;
      state.busy.add(key);
      renderApps();
      try {
        await api.post('/apps/' + encodeURIComponent(app.id) + '/action', {action});
        toast(t('apps.toast.action', {action: label, name: app.name || app.id}), {type: 'success', timeout: 2500});
      } catch (err) {
        toast((err && err.message) || t('apps.toast.error'), {type: 'error'});
      } finally {
        state.busy.delete(key);
        await loadApps();
      }
    }

    async function uninstall(app) {
      const ok = await confirm(t('apps.uninstall.confirm', {name: app.name || app.id}), {
        title: t('apps.action.uninstall'), danger: true,
      });
      if (!ok) return;
      const key = app.id + ':uninstall';
      state.busy.add(key);
      renderApps();
      try {
        await api.del('/store/' + encodeURIComponent(app.id));
        toast(t('apps.toast.uninstalled', {name: app.name || app.id}), {type: 'success'});
      } catch (err) {
        toast((err && err.message) || t('apps.toast.error'), {type: 'error'});
      } finally {
        state.busy.delete(key);
        await loadApps();
      }
    }

    function actionButton(app, action, label, iconName, variant) {
      const key = app.id + ':' + action;
      const busy = state.busy.has(key);
      return h('button', {
        class: 'btn btn--sm' + (variant ? ' btn--' + variant : ' btn--ghost') + (busy ? ' is-busy' : ''),
        type: 'button', disabled: busy,
        onClick: () => (action === 'uninstall' ? uninstall(app) : runAction(app, action, label)),
      }, busy ? h('span', {class: 'spinner spinner--sm'}) : icon(iconName, {size: 14}), label);
    }

    function appCard(app) {
      const state_ = app.state || 'stopped';
      const status = app.status && typeof app.status === 'object' ? app.status : {};
      const href = '#/apps/' + encodeURIComponent(app.id);
      const footer = [];

      if (state_ === 'stopped' || state_ === 'error' || state_ === 'disabled') {
        footer.push(actionButton(app, 'start', t('apps.action.start'), 'play', 'primary'));
      }
      if (state_ === 'running' || state_ === 'starting') {
        footer.push(actionButton(app, 'stop', t('apps.action.stop'), 'stop'));
        footer.push(actionButton(app, 'restart', t('apps.action.restart'), 'refresh'));
      }
      footer.push(actionButton(app, 'uninstall', t('apps.action.uninstall'), 'trash', 'danger'));
      if (app.has_ui) {
        footer.push(h('a', {class: 'btn btn--ghost btn--sm', href, style: {marginLeft: 'auto'}},
          t('apps.action.open'), icon('chevron', {size: 14})));
      }

      return card({
        title: app.name || app.id,
        sub: status.summary || app.description || null,
        mark: appIcon(app.icon, {fallback: 'apps', size: 18}),
        tools: stateBadge(app),
        variant: state_ === 'error' ? 'danger' : '',
        body: state_ === 'error'
          ? h('p', {class: 'small muted'}, app.error || t('apps.error.detail'))
          : (app.description ? h('p', {class: 'small muted'}, app.description) : null),
        footer,
      });
    }

    function renderApps() {
      if (state.appsPhase === 'loading') {
        mount(appsSlot, h('div', {class: 'grid'}, [skeletonCard(), skeletonCard()]));
        return;
      }
      if (state.appsPhase === 'error') {
        mount(appsSlot, card({
          title: t('nav.apps'),
          iconName: 'apps',
          variant: 'danger',
          body: errorState(state.appsError || t('apps.error.load'), loadApps),
        }));
        return;
      }
      const apps = store.get('apps') || [];
      if (!apps.length) {
        mount(appsSlot, emptyState(t('apps.empty.title'), t('apps.empty.text'),
          h('a', {class: 'btn btn--primary btn--sm', href: '#/store'}, icon('download', {size: 15}), t('apps.empty.action'))));
        return;
      }
      mount(appsSlot, h('div', {class: 'grid'}, apps.map(appCard)));
    }

    async function loadApps() {
      state.appsPhase = state.appsPhase === 'ready' ? 'ready' : 'loading';
      renderApps();
      try {
        const raw = await api.get('/apps');
        if (disposed) return;
        const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.apps) ? raw.apps : []);
        state.appsPhase = 'ready';
        state.appsError = null;
        store.set({apps: list});
        renderApps();
      } catch (err) {
        if (disposed) return;
        state.appsPhase = 'error';
        state.appsError = (err && err.message) || t('apps.error.load');
        renderApps();
      }
    }

    /* --------------------------------------------------- detected section */

    const DETECTED_KIND_ICON = {package: 'box', app: 'apps', service: 'services', container: 'server'};

    function detectedFilters() {
      const kinds = ['all', 'package', 'app', 'service', 'container'];
      return h('div', {class: 'segmented', role: 'tablist'},
        kinds.map((kind) => h('button', {
          class: 'segmented__btn', type: 'button',
          'aria-pressed': String(state.detectedFilter === kind),
          onClick: () => { state.detectedFilter = kind; renderDetectedFilters(); renderDetected(); },
        }, t('detected.filter.' + kind))),
      );
    }

    function renderDetectedFilters() {
      mount(detectedFilterSlot, detectedFilters());
    }

    function detectedRow(item) {
      const catalogHref = item.catalog_id ? '#/store?q=' + encodeURIComponent(item.catalog_id) : null;
      const also = Array.isArray(item.also) && item.also.length
        ? h('span', {class: 'tiny faint'}, ' ' + t('detected.also', {count: item.also.length}))
        : null;
      return h('div', {class: 'list__row'},
        icon(DETECTED_KIND_ICON[item.kind] || 'box', {size: 18}),
        h('div', {class: 'list__main'},
          h('span', {class: 'list__title'}, item.name),
          h('span', {class: 'list__sub'},
            [item.version || '', fmt.humanize(item.kind || '')].filter(Boolean).join(' · ')),
        ),
        h('span', {class: 'list__aside row'},
          h('span', {class: 'tag'}, item.source), also,
          catalogHref
            ? h('a', {class: 'badge badge--accent', href: catalogHref}, t('detected.store.yes'))
            : null,
        ),
      );
    }

    function filteredDetected() {
      const items = (state.installed && state.installed.items) || [];
      if (state.detectedFilter === 'all') return items;
      return items.filter((item) => item && item.kind === state.detectedFilter);
    }

    function renderDetected() {
      if (state.installedPhase === 'loading') {
        mount(detectedSlot, h('div', {class: 'list'}, [0, 1, 2].map(() => h('div', {class: 'list__row'},
          h('div', {class: 'skeleton', style: {width: '18px', height: '18px', borderRadius: '5px'}}),
          h('div', {class: 'grow'}, h('div', {class: 'skeleton skeleton--text skeleton--line-sm'})),
        ))));
        return;
      }
      if (state.installedPhase === 'error') {
        mount(detectedSlot, errorState(state.installedError || t('detected.error.load'), () => loadDetected(false)));
        return;
      }
      const items = filteredDetected();
      const total = (state.installed && state.installed.items || []).length;
      const parts = [];

      const errors = (state.installed && state.installed.errors) || {};
      const errorNames = Object.keys(errors);
      if (errorNames.length) {
        parts.push(h('p', {class: 'small faint'}, t('detected.notChecked', {list: errorNames.join(', ')})));
      }
      if (state.installed && isFinite(state.installed.age_seconds) && state.installed.age_seconds > 1) {
        const ts = new Date(Date.now() - state.installed.age_seconds * 1000).toISOString();
        parts.push(h('p', {class: 'tiny faint'}, t('detected.cached', {time: fmt.relativeTime(ts)})));
      }

      if (!items.length) {
        parts.push(total
          ? emptyState(t('detected.empty.filtered.title'), t('detected.empty.filtered.text'))
          : emptyState(t('detected.empty.title'), t('detected.empty.text')));
      } else {
        parts.push(h('div', {class: 'list'}, items.map(detectedRow)));
      }
      mount(detectedSlot, parts);
    }

    async function loadDetected(refresh) {
      state.installedPhase = state.installedPhase === 'ready' && !refresh ? 'ready' : 'loading';
      if (refresh) {
        state.refreshingInstalled = true;
        detectedRefreshBtn.disabled = true;
        mount(detectedRefreshBtn, [icon('refresh', {size: 15}), t('detected.refreshing')]);
      }
      renderDetected();
      try {
        const raw = await api.get('/installed', {query: refresh ? {refresh: true} : null});
        if (disposed) return;
        state.installed = raw || {items: [], errors: {}, counts: {}};
        state.installedPhase = 'ready';
        state.installedError = null;
        renderDetected();
      } catch (err) {
        if (disposed) return;
        state.installedPhase = 'error';
        state.installedError = (err && err.message) || t('detected.error.load');
        renderDetected();
      } finally {
        if (refresh) {
          state.refreshingInstalled = false;
          detectedRefreshBtn.disabled = false;
          mount(detectedRefreshBtn, [icon('refresh', {size: 15}), t('detected.refresh')]);
        }
      }
    }

    /* ------------------------------------------------------------ wiring */

    renderApps();
    renderDetectedFilters();
    renderDetected();

    const offStore = store.subscribe((next, prev) => {
      if (disposed) return;
      if (!Object.is(next.apps, prev.apps) && state.appsPhase !== 'loading') renderApps();
    });

    await Promise.all([loadApps(), loadDetected(false)]);

    return () => {
      disposed = true;
      offStore();
    };
  },
};
