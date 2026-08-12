// views/services.js — Advanced mode: everything project-os can start, stop and
// restart on this machine, in two halves.
//
// "Managed services" is GET /api/system/services -- systemd units under one of
// the prefixes project_os/api/system.py is willing to talk about. Control is
// gated server-side by security.allow_service_control, and the response says
// so (`can_control: false`) rather than making the client guess from a 403;
// buttons are simply not offered when it is off.
//
// "Apps" is GET /api/apps -- project-os's own plugin manager, a different
// system entirely (in-process, not systemd). They read the same on this page
// on purpose: an app and a systemd unit are both "a thing that can be
// running", and the person looking at this screen should not have to know
// which subsystem owns which.
//
// Neither endpoint reports memory or uptime for a *systemd* unit -- list-units
// only gives load/active/sub/description, and nothing here shells out to
// `systemctl show` for resource numbers. Apps at least carry `started_at`, so
// their uptime is real; a unit's "up" column stays honest and says so.

import {h, mount} from '../lib/dom.js';
import {icon, hasIcon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';
import {navigate} from '../lib/router.js';

setStrings('en', {
  'services.title': 'Services',
  'services.lead': 'Everything project-os can start, stop and restart on this machine.',
  'services.units.title': 'Managed services',
  'services.units.sub': 'systemd units project-os knows about',
  'services.units.none': 'No managed units found',
  'services.units.none.detail': 'project-os only lists units under a known prefix (project_os, home-assistant, mosquitto, and the like).',
  'services.units.unavailable': 'No systemd on this machine',
  'services.units.unavailable.detail': 'This looks like a developer machine rather than a Pi -- there is no systemctl to ask.',
  'services.units.locked': 'Control is turned off',
  'services.units.locked.detail': 'Turn on security.allow_service_control in Settings to start, stop or restart units from here.',
  'services.units.locked.link': 'Open Settings > Developer',
  'services.apps.title': 'Apps',
  'services.apps.sub': 'Running under project-os’ own plugin manager',
  'services.apps.none': 'No apps installed',
  'services.apps.none.detail': 'Install one from the store and it will show up here.',
  'services.action.start': 'Start',
  'services.action.stop': 'Stop',
  'services.action.restart': 'Restart',
  'services.action.logs': 'Logs',
  'services.confirm.stop': 'Stop {name}? Anything depending on it will lose the connection.',
  'services.confirm.restart': 'Restart {name}? It will be briefly unavailable.',
  'services.up': 'up {time}',
}, {activate: false});

/* --------------------------------------------------------------- pieces */

function card(options) {
  const {title, sub, iconName, tools, body, footer, variant = ''} = options;
  return h('div', {class: 'card' + (variant ? ' card--' + variant : '')},
    h('div', {class: 'card__header'},
      iconName ? h('span', {class: 'card__icon'}, icon(iconName, {size: 18})) : null,
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
    h('div', {class: 'card__body stack'},
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
              icon('refresh', {size: 14}), t('action.retry')))
        : null,
    ),
  );
}

function emptyState(title, message) {
  return h('div', {class: 'empty empty--sm'},
    h('span', {class: 'empty__icon'}, icon('info', {size: 18})),
    h('p', {class: 'empty__title'}, title),
    message ? h('p', {class: 'empty__text'}, message) : null,
  );
}

function activeBadge(active, sub) {
  const level = active === 'active' ? 'ok' : active === 'failed' ? 'danger' : active === 'activating' ? 'info' : '';
  return h('span', {class: 'badge' + (level ? ' badge--' + level : '')}, sub || active || 'unknown');
}

function appBadge(app) {
  const state = app.state || 'unknown';
  const level = state === 'error' ? 'danger' : state === 'running' || state === 'started' ? 'ok'
    : state === 'disabled' ? '' : 'info';
  return h('span', {class: 'badge' + (level ? ' badge--' + level : '')}, state);
}

/* ------------------------------------------------------------------ view */

export default {
  id: 'services',
  title: t('nav.services'),

  async mount(root, ctx) {
    let disposed = false;
    const busy = new Set(); // "unit:action" or "app:action" keys in flight

    let unitsPayload = null; // {available, services, can_control}
    let unitsError = null;
    let unitsLoading = true;

    let apps = null;
    let appsError = null;
    let appsLoading = true;

    const unitsSlot = h('div', {class: 'slot'});
    const appsSlot = h('div', {class: 'slot'});

    const refreshBtn = h('button', {
      class: 'btn btn--sm', type: 'button',
      onClick: () => loadAll(true),
    }, icon('refresh', {size: 15}), t('action.refresh'));

    mount(root, [
      h('div', {class: 'page__header'},
        h('div', null,
          h('h2', null, t('services.title')),
          h('p', {class: 'page__lead'}, t('services.lead')),
        ),
        h('div', {class: 'page__actions'}, refreshBtn),
      ),
      h('div', {class: 'grid grid--wide'}, unitsSlot, appsSlot),
    ]);

    /* --------------------------------------------------------- unit rows */

    async function runUnitAction(unit, action) {
      const key = unit.name + ':' + action;
      if (busy.has(key)) return;
      if (action === 'stop' || action === 'restart') {
        const ok = await confirm(t('services.confirm.' + action, {name: unit.name}), {
          title: t('services.action.' + action),
          danger: action === 'stop',
        });
        if (!ok) return;
      }
      busy.add(key);
      renderUnits();
      try {
        await api.post('/system/services/' + encodeURIComponent(unit.name) + '/' + action, {});
        toast(unit.name + ' · ' + t('services.action.' + action), {type: 'success', timeout: 2500});
      } catch (err) {
        toast((err && err.message) || t('state.error'), {type: 'error'});
      } finally {
        busy.delete(key);
        await loadUnits();
      }
    }

    function unitRow(unit, canControl) {
      const key = (action) => unit.name + ':' + action;
      const controls = canControl ? h('div', {class: 'row', style: {gap: 'var(--space-1)'}},
        h('button', {
          class: 'btn btn--icon btn--sm', type: 'button', title: t('services.action.start'),
          'aria-label': t('services.action.start'), disabled: busy.has(key('start')),
          onClick: () => runUnitAction(unit, 'start'),
        }, icon('play', {size: 14})),
        h('button', {
          class: 'btn btn--icon btn--sm', type: 'button', title: t('services.action.restart'),
          'aria-label': t('services.action.restart'), disabled: busy.has(key('restart')),
          onClick: () => runUnitAction(unit, 'restart'),
        }, icon('refresh', {size: 14})),
        h('button', {
          class: 'btn btn--icon btn--sm', type: 'button', title: t('services.action.stop'),
          'aria-label': t('services.action.stop'), disabled: busy.has(key('stop')),
          onClick: () => runUnitAction(unit, 'stop'),
        }, icon('stop', {size: 14})),
      ) : null;

      return h('div', {class: 'list__row'},
        h('span', {class: 'card__icon'}, icon('services', {size: 16})),
        h('div', {class: 'list__main'},
          h('span', {class: 'list__title'}, unit.name),
          h('span', {class: 'list__sub truncate'}, unit.description || unit.unit),
        ),
        h('span', {class: 'list__aside'},
          activeBadge(unit.active, unit.sub),
          h('a', {
            class: 'btn btn--ghost btn--sm', href: '#/logs?unit=' + encodeURIComponent(unit.name),
            title: t('services.action.logs'),
          }, icon('logs', {size: 14})),
          controls,
        ),
      );
    }

    function renderUnits() {
      if (unitsLoading && !unitsPayload) {
        mount(unitsSlot, skeletonCard());
        return;
      }
      if (unitsError && !unitsPayload) {
        mount(unitsSlot, card({
          title: t('services.units.title'), iconName: 'services',
          body: errorState(unitsError, () => loadUnits()),
        }));
        return;
      }
      const payload = unitsPayload || {available: false, services: [], can_control: false};
      if (!payload.available) {
        mount(unitsSlot, card({
          title: t('services.units.title'), iconName: 'services',
          body: emptyState(t('services.units.unavailable'), t('services.units.unavailable.detail')),
        }));
        return;
      }
      const units = payload.services || [];
      const body = [];
      if (!payload.can_control) {
        body.push(h('div', {class: 'notice notice--info'},
          icon('lock', {size: 18}),
          h('div', {class: 'notice__body'},
            h('span', {class: 'notice__title'}, t('services.units.locked')),
            h('span', {class: 'small muted'}, t('services.units.locked.detail')),
            h('a', {class: 'btn btn--sm btn--outline', href: '#/settings/developer'},
              t('services.units.locked.link')))));
      }
      body.push(units.length
        ? h('div', {class: 'list'}, units.map((unit) => unitRow(unit, payload.can_control)))
        : emptyState(t('services.units.none'), t('services.units.none.detail')));
      mount(unitsSlot, card({
        title: t('services.units.title'), sub: t('services.units.sub'), iconName: 'services', body,
      }));
    }

    /* ---------------------------------------------------------- app rows */

    async function runAppAction(app, action) {
      const key = app.id + ':' + action;
      if (busy.has(key)) return;
      if (action === 'stop' || action === 'restart') {
        const ok = await confirm(t('services.confirm.' + action, {name: app.name || app.id}), {
          title: t('services.action.' + action),
          danger: action === 'stop',
        });
        if (!ok) return;
      }
      busy.add(key);
      renderApps();
      try {
        await api.post('/apps/' + encodeURIComponent(app.id) + '/action', {action});
        toast((app.name || app.id) + ' · ' + t('services.action.' + action), {type: 'success', timeout: 2500});
      } catch (err) {
        toast((err && err.message) || t('state.error'), {type: 'error'});
      } finally {
        busy.delete(key);
        await loadApps();
      }
    }

    function appRow(app) {
      const key = (action) => app.id + ':' + action;
      const running = app.state === 'running' || app.state === 'started';
      const up = app.started_at ? t('services.up', {time: fmt.relativeTime(app.started_at).replace(/^in |ago$/g, '').trim()}) : null;
      return h('div', {class: 'list__row'},
        h('span', {class: 'card__icon'},
          icon(hasIcon(app.icon) ? app.icon : 'apps', {size: 16})),
        h('div', {class: 'list__main'},
          h('span', {class: 'list__title'}, app.name || app.id),
          h('span', {class: 'list__sub truncate'}, app.state === 'error' && app.error ? app.error : (up || app.description || '—')),
        ),
        h('span', {class: 'list__aside'},
          appBadge(app),
          h('a', {
            class: 'btn btn--ghost btn--sm', href: '#/logs?source=' + encodeURIComponent('app.' + app.id),
            title: t('services.action.logs'),
          }, icon('logs', {size: 14})),
          h('div', {class: 'row', style: {gap: 'var(--space-1)'}},
            h('button', {
              class: 'btn btn--icon btn--sm', type: 'button', title: t('services.action.start'),
              'aria-label': t('services.action.start'), disabled: running || busy.has(key('start')),
              onClick: () => runAppAction(app, 'start'),
            }, icon('play', {size: 14})),
            h('button', {
              class: 'btn btn--icon btn--sm', type: 'button', title: t('services.action.restart'),
              'aria-label': t('services.action.restart'), disabled: !running || busy.has(key('restart')),
              onClick: () => runAppAction(app, 'restart'),
            }, icon('refresh', {size: 14})),
            h('button', {
              class: 'btn btn--icon btn--sm', type: 'button', title: t('services.action.stop'),
              'aria-label': t('services.action.stop'), disabled: !running || busy.has(key('stop')),
              onClick: () => runAppAction(app, 'stop'),
            }, icon('stop', {size: 14})),
          ),
        ),
      );
    }

    function renderApps() {
      if (appsLoading && !apps) {
        mount(appsSlot, skeletonCard());
        return;
      }
      if (appsError && !apps) {
        mount(appsSlot, card({
          title: t('services.apps.title'), iconName: 'apps',
          body: errorState(appsError, () => loadApps()),
        }));
        return;
      }
      const list = apps || [];
      mount(appsSlot, card({
        title: t('services.apps.title'), sub: t('services.apps.sub'), iconName: 'apps',
        body: list.length
          ? h('div', {class: 'list'}, list.map(appRow))
          : emptyState(t('services.apps.none'), t('services.apps.none.detail')),
      }));
    }

    /* ------------------------------------------------------------ loads */

    async function loadUnits() {
      unitsLoading = true;
      try {
        const raw = await api.get('/system/services');
        if (disposed) return;
        unitsPayload = raw;
        unitsError = null;
      } catch (err) {
        if (disposed) return;
        unitsError = (err && err.message) || t('services.units.title') + ': ' + t('state.error');
      } finally {
        unitsLoading = false;
      }
      renderUnits();
    }

    async function loadApps() {
      appsLoading = true;
      try {
        const raw = await api.get('/apps');
        if (disposed) return;
        apps = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.apps) ? raw.apps : []);
        appsError = null;
      } catch (err) {
        if (disposed) return;
        appsError = (err && err.message) || t('services.apps.title') + ': ' + t('state.error');
      } finally {
        appsLoading = false;
      }
      renderApps();
    }

    function loadAll(manual) {
      if (manual) {
        refreshBtn.disabled = true;
        setTimeout(() => { if (!disposed) refreshBtn.disabled = false; }, 800);
      }
      return Promise.all([loadUnits(), loadApps()]);
    }

    renderUnits();
    renderApps();
    loadAll(false);

    // Systemd state has no push feed, so a short poll is the only way to see a
    // unit someone restarted from an SSH session; the tab is Advanced-only, so
    // the cost is paid only by people already looking at it.
    const poll = setInterval(() => {
      if (!disposed) loadAll(false);
    }, 10000);

    return () => {
      disposed = true;
      clearInterval(poll);
    };
  },
};
