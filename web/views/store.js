// views/store.js — the catalog. A clean project-os boots with nothing running;
// this is the screen that fills it in, so it carries the weight of a real
// app store rather than a settings sub-page.
//
// Two backend decisions shape this file:
//
// 1. Nothing is hidden for not fitting the board. `GET /store` annotates every
//    entry with `fits`/`fit_reason` computed against this machine's measured
//    RAM, not a vendor spec sheet, and install still works if the caller sends
//    `accept_oversize: true`. The check is advice, not a gate — so cards for
//    oversize entries stay clickable, with the numbers shown before the click.
// 2. Not everything that's listed can install today. Built-ins install for
//    real. Container apps install for real *only when the catalog entry has a
//    `container:` block* — the API says so per item via `installable`
//    (present only on kind === 'container'). Everything else — services,
//    recipes, and container entries without a spec — still returns from the
//    browse endpoint (so the catalog reads complete) but 501s as
//    "installer_pending" if clicked. This view treats that case as a plain
//    disabled state on the card instead of letting the click round-trip fail.
//
// Install is a container pull that can take minutes, so it is never a frozen
// button: the card enters a busy state immediately and then rides `app.state`
// events off the websocket bus for live progress, falling back to nothing more
// than "still working" if the socket is down — there is no polling loop here
// because a stuck install is rare enough that a manual refresh is an
// acceptable fallback, and a 5s poll against a Pi 3 doing a docker pull is not
// free.

import {h, mount, clear} from '../lib/dom.js';
import {icon, hasIcon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';
import {ApiError} from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';

setStrings('en', {
  'store.lead': 'project-os ships empty. Everything below is something you can add.',
  'store.search.placeholder': 'Search apps, categories, tags…',
  'store.filter.all': 'All',
  'store.board.title': 'This machine',
  'store.board.budget': '{free} free for apps',
  'store.board.reserved': '{mb} reserved for project-os itself',
  'store.board.tier.roomy': 'Room to spare',
  'store.board.tier.comfortable': 'Comfortable',
  'store.board.tier.minimal': 'Tight — pick carefully',
  'store.empty.title': 'Nothing matches',
  'store.empty.text': 'Try a different search or category.',
  'store.error.load': 'Could not load the store.',
  'store.action.retry': 'Retry',
  'store.action.install': 'Install',
  'store.action.installing': 'Installing…',
  'store.action.remove': 'Remove',
  'store.action.removing': 'Removing…',
  'store.action.removed': '{name} removed. Its data was left in place.',
  'store.action.installed': '{name} is installed.',
  'store.installed.badge': 'Installed',
  'store.kind.builtin': 'Built in',
  'store.kind.container': 'Container',
  'store.kind.service': 'System service',
  'store.kind.recipe': 'Recipe',
  'store.needs.ram': '{mb} MB RAM',
  'store.needs.disk': '{mb} MB disk',
  'store.needs.docker': 'Needs a container runtime',
  'store.needs.usb': 'Needs a USB device',
  'store.needs.node': 'Needs Node.js',
  'store.notice.noRuntime': 'No Docker or Podman found on this machine yet — install one before this can run.',
  'store.notice.pending': 'This installs as a {kind}, and that installer is not built yet. Nothing happens if you click Install.',
  'store.notice.oversize': '{reason}',
  'store.confirm.oversize.title': 'This may be too much for this board',
  'store.confirm.oversize.body': 'Install anyway?',
  'store.confirm.oversize.confirm': 'Install anyway',
  'store.confirm.remove.title': 'Remove {name}?',
  'store.confirm.remove.body': 'It stops running. Its settings and data stay on disk — reinstalling brings them back.',
  'store.confirm.remove.confirm': 'Remove',
  'store.state.starting': 'starting…',
  'store.state.running': 'running',
  'store.state.error': 'failed to start',
  'store.state.stopped': 'stopped',
  'store.toast.error': 'That did not work.',
}, {activate: false});

/* -------------------------------------------------------------- helpers */

// Every `category` the catalog can produce, mapped onto an icon that already
// exists in lib/icons.js. An unmapped category falls back to `box` rather than
// growing the icon set for a label nobody has shipped yet.
const CATEGORY_ICONS = {
  home: 'home',
  smart_home: 'home',
  media: 'film',
  network: 'wifi',
  storage: 'folder',
  automation: 'flow',
  camera: 'camera',
  security: 'shield',
  productivity: 'chat',
  dev: 'code',
  development: 'code',
  bots: 'chat',
  server: 'server',
  music: 'music',
};

function categoryIcon(name) {
  const mapped = CATEGORY_ICONS[name];
  return mapped && hasIcon(mapped) ? mapped : 'box';
}

function categoryLabel(name) {
  return fmt.humanize(name || '');
}

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
              icon('refresh', {size: 14}), t('store.action.retry')),
          )
        : null,
    ),
  );
}

function emptyState(title, message) {
  return h('div', {class: 'empty'},
    h('span', {class: 'empty__icon'}, icon('search', {size: 22})),
    h('p', {class: 'empty__title'}, title),
    message ? h('p', {class: 'empty__text'}, message) : null,
  );
}

/** Whether the store can actually install this entry today, independent of
 *  whether it fits in RAM (fit is a warning, not a gate — this is a hard one). */
function isInstallable(item) {
  if (item.kind === 'builtin') return true;
  if (item.kind === 'container') return item.installable === true;
  return false;
}

const TIER_KEYS = {roomy: 'store.board.tier.roomy', comfortable: 'store.board.tier.comfortable', minimal: 'store.board.tier.minimal'};

export default {
  id: 'store',
  title: 'Store',

  async mount(root, ctx) {
    let disposed = false;

    const state = {
      status: 'loading', // loading | ready | error
      error: null,
      items: [],
      categories: [],
      board: null,
      category: 'all',
      query: '',
      busy: new Map(), // app id -> 'install' | 'remove'
      liveState: new Map(), // app id -> latest app.state payload seen mid-install
    };

    async function load() {
      state.status = 'loading';
      render();
      try {
        const query = {};
        if (state.category !== 'all') query.category = state.category;
        if (state.query.trim()) query.q = state.query.trim();
        const data = await api.get('/store', {query});
        if (disposed) return;
        state.items = Array.isArray(data.items) ? data.items : [];
        state.categories = Array.isArray(data.categories) ? data.categories : [];
        state.board = data.board || null;
        state.status = 'ready';
      } catch (err) {
        if (disposed) return;
        state.status = 'error';
        state.error = err instanceof ApiError ? err.message : t('store.error.load');
      }
      render();
    }

    async function installApp(item, {acceptOversize = false} = {}) {
      if (state.busy.has(item.id)) return;
      state.busy.set(item.id, 'install');
      render();
      try {
        const result = await api.post('/store/' + encodeURIComponent(item.id) + '/install', {
          accept_oversize: acceptOversize,
        });
        if (disposed) return;
        toast(t('store.action.installed', {name: item.name}), {type: 'success'});
        void result;
        await load();
      } catch (err) {
        if (disposed) return;
        if (err instanceof ApiError && err.code === 'does_not_fit') {
          state.busy.delete(item.id);
          render();
          const ok = await confirm(t('store.confirm.oversize.body'), {
            title: t('store.confirm.oversize.title'),
            detail: err.message,
            confirmLabel: t('store.confirm.oversize.confirm'),
          });
          if (ok && !disposed) await installApp(item, {acceptOversize: true});
          return;
        }
        // 501 installer_pending, 503 no_container_runtime, 409 already_installed —
        // every one of these already carries a sentence written for a human
        // (see api/store.py), so it goes straight to a toast, not a stack trace.
        toast((err instanceof ApiError && err.message) || t('store.toast.error'), {type: 'error'});
      } finally {
        if (!disposed) {
          state.busy.delete(item.id);
          render();
        }
      }
    }

    async function removeApp(item) {
      if (state.busy.has(item.id)) return;
      const ok = await confirm(t('store.confirm.remove.body'), {
        title: t('store.confirm.remove.title', {name: item.name}),
        confirmLabel: t('store.confirm.remove.confirm'),
        danger: true,
      });
      if (!ok || disposed) return;
      state.busy.set(item.id, 'remove');
      render();
      try {
        await api.del('/store/' + encodeURIComponent(item.id));
        if (disposed) return;
        toast(t('store.action.removed', {name: item.name}));
        await load();
      } catch (err) {
        if (disposed) return;
        toast((err instanceof ApiError && err.message) || t('store.toast.error'), {type: 'error'});
      } finally {
        if (!disposed) {
          state.busy.delete(item.id);
          render();
        }
      }
    }

    function needList(item) {
      const needs = [
        h('span', {class: 'tag'}, t('store.needs.ram', {mb: item.ram_mb})),
      ];
      if (item.disk_mb) needs.push(h('span', {class: 'tag'}, t('store.needs.disk', {mb: item.disk_mb})));
      if (item.kind === 'container') needs.push(h('span', {class: 'tag'}, t('store.needs.docker')));
      if (item.requires_usb) needs.push(h('span', {class: 'tag'}, t('store.needs.usb')));
      if (item.requires_node) needs.push(h('span', {class: 'tag'}, t('store.needs.node')));
      return h('div', {class: 'pills'}, needs);
    }

    function statusBadges(item) {
      const badges = [];
      if (item.installed) badges.push(h('span', {class: 'badge badge--ok'}, t('store.installed.badge')));
      badges.push(h('span', {class: 'badge badge--plain'}, t('store.kind.' + item.kind) || fmt.humanize(item.kind)));
      if (!item.fits) badges.push(h('span', {class: 'badge badge--warn'}, fmt.humanize(item.tier_required || '')));
      return h('div', {class: 'row'}, badges);
    }

    function appCard(item) {
      const busyKind = state.busy.get(item.id);
      const live = state.liveState.get(item.id);
      const installable = isInstallable(item);
      const runtimeDown = item.kind === 'container' && item.container_runtime && !item.container_runtime.available;

      const notices = [];
      if (!item.fits) {
        notices.push(h('div', {class: 'notice notice--warn'},
          icon('warning', {size: 16}),
          h('div', {class: 'notice__body'}, h('span', null, t('store.notice.oversize', {reason: item.fit_reason || ''})))));
      }
      if (item.kind === 'container' && !item.installable) {
        notices.push(h('div', {class: 'notice notice--info'},
          icon('info', {size: 16}),
          h('div', {class: 'notice__body'}, h('span', null, t('store.notice.pending', {kind: item.kind})))));
      } else if (!installable && item.kind !== 'container') {
        notices.push(h('div', {class: 'notice notice--info'},
          icon('info', {size: 16}),
          h('div', {class: 'notice__body'}, h('span', null, t('store.notice.pending', {kind: item.kind})))));
      } else if (runtimeDown) {
        notices.push(h('div', {class: 'notice notice--warn'},
          icon('warning', {size: 16}),
          h('div', {class: 'notice__body'}, h('span', null, item.container_runtime.reason || t('store.notice.noRuntime')))));
      }

      let footer;
      if (item.installed) {
        footer = h('button', {
          class: 'btn btn--outline btn--danger btn--sm',
          type: 'button',
          disabled: !!busyKind,
          onClick: () => removeApp(item),
        }, busyKind === 'remove' ? h('span', {class: 'spinner spinner--sm'}) : icon('trash', {size: 14}),
           busyKind === 'remove' ? t('store.action.removing') : t('store.action.remove'));
      } else {
        const disabledReason = !installable || runtimeDown;
        footer = h('button', {
          class: 'btn btn--primary btn--sm',
          type: 'button',
          disabled: !!busyKind || disabledReason,
          onClick: () => installApp(item),
        }, busyKind === 'install' ? h('span', {class: 'spinner spinner--sm'}) : icon('download', {size: 14}),
           busyKind === 'install' ? (live ? t('store.state.' + live.state) || t('store.action.installing') : t('store.action.installing')) : t('store.action.install'));
      }

      return card({
        title: item.name,
        sub: item.summary,
        iconName: hasIcon(item.icon) ? item.icon : categoryIcon(item.category),
        tools: statusBadges(item),
        body: h('div', {class: 'stack stack--sm'},
          h('p', {class: 'muted small'}, item.description || item.summary || ''),
          needList(item),
          notices.length ? h('div', {class: 'stack stack--sm'}, notices) : null,
        ),
        footer,
      });
    }

    function categoryPills() {
      const all = h('a', {
        class: 'segmented__btn' + (state.category === 'all' ? ' is-active' : ''),
        href: '#', 'aria-current': state.category === 'all' ? 'true' : null,
        onClick: (e) => { e.preventDefault(); if (state.category !== 'all') { state.category = 'all'; load(); } },
      }, t('store.filter.all'));
      const rest = state.categories.map((c) => h('a', {
        class: 'segmented__btn' + (state.category === c ? ' is-active' : ''),
        href: '#', 'aria-current': state.category === c ? 'true' : null,
        onClick: (e) => { e.preventDefault(); if (state.category !== c) { state.category = c; load(); } },
      }, icon(categoryIcon(c), {size: 14}), categoryLabel(c)));
      return h('div', {class: 'segmented'}, all, rest);
    }

    function boardCard() {
      if (!state.board) return null;
      const b = state.board;
      const tierKey = TIER_KEYS[b.tier] || null;
      return card({
        title: t('store.board.title'),
        sub: b.model,
        iconName: 'chip',
        body: h('div', {class: 'kv'},
          h('div', {class: 'kv__key'}, t('unit.ram')),
          h('div', {class: 'kv__value'}, fmt.bytes((b.ram_total_mb || 0) * 1024 * 1024)),
          h('div', {class: 'kv__key'}, t('store.board.budget', {free: fmt.bytes((b.budget_mb || 0) * 1024 * 1024)})),
          h('div', {class: 'kv__value'}, tierKey ? t(tierKey) : (b.tier || '—')),
          h('div', {class: 'kv__key'}, t('store.board.reserved', {mb: b.reserved_mb})),
          h('div', {class: 'kv__value'}, ''),
        ),
      });
    }

    function render() {
      const nodes = [
        h('div', {class: 'page__header'},
          h('h1', null, t('nav.store') || 'Store'),
          h('p', {class: 'page__lead'}, t('store.lead')),
        ),
        h('div', {class: 'input-group'},
          icon('search', {size: 16}),
          h('input', {
            type: 'search',
            placeholder: t('store.search.placeholder'),
            value: state.query,
            onInput: (e) => { state.query = e.target.value; },
            onKeydown: (e) => { if (e.key === 'Enter') load(); },
          }),
        ),
        categoryPills(),
      ];

      const board = boardCard();
      if (board) nodes.push(board);

      if (state.status === 'loading') {
        nodes.push(h('div', {class: 'grid grid--wide'}, [0, 1, 2, 3, 4, 5].map(() => skeletonCard())));
      } else if (state.status === 'error') {
        nodes.push(errorState(state.error, load));
      } else if (!state.items.length) {
        nodes.push(emptyState(t('store.empty.title'), t('store.empty.text')));
      } else {
        nodes.push(h('div', {class: 'grid grid--wide'}, state.items.map(appCard)));
      }

      clear(root);
      mount(root, h('div', {class: 'stack stack--lg'}, nodes));
    }

    // Live install progress: the busy button label upgrades from a generic
    // "Installing…" to the actual lifecycle word as `app.state` events arrive,
    // and a full reload — not just badge-flipping — runs once an app we were
    // installing lands on `running` or `error`, since a fresh `installed`
    // flag and `container_runtime` reading only come from the catalog again.
    let offWs = null;
    if (ctx.ws && typeof ctx.ws.on === 'function') {
      offWs = ctx.ws.on('app.state', (data) => {
        if (disposed || !data || !data.id) return;
        if (!state.busy.has(data.id)) return;
        state.liveState.set(data.id, data);
        render();
        if (data.state === 'running' || data.state === 'error') {
          load();
        }
      });
    }

    await load();

    return () => {
      disposed = true;
      if (typeof offWs === 'function') offWs();
      clear(root);
    };
  },
};
