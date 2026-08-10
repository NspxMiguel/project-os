// views/devices.js — everything project-os has found on the network, and what
// to do about it.
//
// One view handles two routes: `#/devices` (the list) and `#/devices/:id`
// (one device, with its recipes). Both are registered to this module in
// app.js's router, so `ctx.params.id` is what decides which screen renders.
//
// The device list is shared with dashboard.js through `ctx.store` (key
// `devices`) so a rename or a pin made here shows up there without a refetch,
// and vice versa. Unlike the dashboard's trimmed preview, this view always
// fetches with `include_ignored=true` — ignored devices still need a screen
// they can be un-ignored from.
//
// Recipe steps: `install` / `config` / `open` are one button, calling
// POST /api/recipes/{recipe_id}/run. `manual` steps are text only and are
// never rendered as a button — the box cannot press them, and pretending
// otherwise is the thing this screen exists to avoid.

import {h, mount, clear} from '../lib/dom.js';
import {icon, hasIcon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';
import {navigate} from '../lib/router.js';

setStrings('en', {
  'devices.lead': 'Everything project-os has found on this network.',
  'devices.rescan': 'Scan now',
  'devices.rescanning': 'Scanning…',
  'devices.scan.started': 'Scanning the network…',
  'devices.scan.failed': 'The scan failed.',
  'devices.filter.all': 'All',
  'devices.filter.online': 'Online',
  'devices.filter.pinned': 'Pinned',
  'devices.filter.ignored': 'Ignored',
  'devices.filter.allKinds': 'All kinds',
  'devices.empty.title': 'Nothing discovered yet',
  'devices.empty.text': 'project-os looks for AirPlay speakers, Chromecasts, smart hubs and a few other things over mDNS and local probes. Turn something on, then scan again.',
  'devices.empty.filtered.title': 'Nothing matches this filter',
  'devices.empty.filtered.text': 'Clear the filter to see the rest of what was found.',
  'devices.unavailable.title': 'Discovery is unavailable',
  'devices.error.load': 'Could not load devices.',
  'devices.action.retry': 'Retry',
  'devices.action.pin': 'Pin',
  'devices.action.unpin': 'Unpin',
  'devices.action.ignore': 'Ignore',
  'devices.action.unignore': 'Unignore',
  'devices.action.rename': 'Rename',
  'devices.action.forget': 'Forget',
  'devices.action.save': 'Save',
  'devices.action.cancel': 'Cancel',
  'devices.action.open': 'Open',
  'devices.rename.placeholder': 'Device name',
  'devices.forget.confirm': 'Forget {name}? It comes back on the next scan, minus its custom name.',
  'devices.toast.pinned': '{name} pinned',
  'devices.toast.unpinned': '{name} unpinned',
  'devices.toast.ignored': '{name} ignored',
  'devices.toast.unignored': '{name} no longer ignored',
  'devices.toast.renamed': '{name} renamed',
  'devices.toast.forgot': '{name} forgotten',
  'devices.toast.error': 'That did not work.',
  'devices.detail.back': 'All devices',
  'devices.detail.notfound.title': 'Device not found',
  'devices.detail.notfound.text': 'It may have been forgotten, or the address bar has the wrong id.',
  'devices.detail.error.load': 'Could not load this device.',
  'devices.detail.info.title': 'Details',
  'devices.detail.info.address': 'Address',
  'devices.detail.info.kind': 'Kind',
  'devices.detail.info.firstSeen': 'First seen',
  'devices.detail.info.lastSeen': 'Last seen',
  'devices.detail.info.capabilities': 'Capabilities',
  'devices.detail.info.none': 'None reported',
  'devices.detail.info.properties': 'Raw properties',
  'devices.detail.recipes.title': 'What you can do with this device',
  'devices.detail.recipes.automatic': '{done} of {total} steps are one click',
  'devices.detail.recipes.manualOnly': 'Every step here is manual — nothing yet can be done for you.',
  'devices.detail.recipes.empty.title': 'No recipes for this device yet',
  'devices.detail.recipes.empty.text': 'project-os does not know a specific routine for this kind of device yet.',
  'devices.detail.recipes.error': 'Could not load recipes for this device.',
  'devices.detail.step.manual': 'Manual',
  'devices.detail.step.run': 'Run',
  'devices.detail.step.done': 'Done',
  'devices.detail.step.open': 'Open',
  'devices.detail.step.apply': 'Apply',
  'devices.detail.step.install': 'Install',
  'devices.detail.step.installed': 'Installed',
  'devices.status.online': 'online',
  'devices.status.offline': 'offline',
  'devices.status.pinned': 'pinned',
  'devices.status.ignored': 'ignored',
}, {activate: false});

/* -------------------------------------------------------------- helpers */

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

function skeletonList(count) {
  const rows = [];
  for (let i = 0; i < count; i += 1) {
    rows.push(h('div', {class: 'list__row'},
      h('div', {class: 'skeleton', style: {width: '22px', height: '22px', borderRadius: '6px'}}),
      h('div', {class: 'grow'},
        h('div', {class: 'skeleton skeleton--text skeleton--line-sm'}),
      ),
    ));
  }
  return h('div', {class: 'list'}, rows);
}

function errorState(message, onRetry) {
  return h('div', {class: 'notice notice--error'},
    icon('warning', {size: 18}),
    h('div', {class: 'notice__body'},
      h('span', null, message),
      onRetry
        ? h('div', {class: 'notice__actions'},
            h('button', {class: 'btn btn--sm', type: 'button', onClick: onRetry},
              icon('refresh', {size: 14}), t('devices.action.retry')),
          )
        : null,
    ),
  );
}

function emptyState(title, message, action) {
  return h('div', {class: 'empty'},
    h('span', {class: 'empty__icon'}, icon('devices', {size: 22})),
    h('p', {class: 'empty__title'}, title),
    message ? h('p', {class: 'empty__text'}, message) : null,
    action || null,
  );
}

// Kinds discovery/lan.py can produce, mapped onto icons that already exist —
// nothing here should ever need a new icon; unmapped kinds fall back to the
// generic 'devices' glyph, same as the dashboard's compact list.
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
  mqtt_broker: 'chip',
  esphome: 'chip',
  microcontroller: 'chip',
  serial_device: 'terminal',
  printer: 'printer',
  printer_3d: 'printer',
  nas: 'disk',
  router: 'broadcast',
  media_server: 'film',
  phone: 'user',
  smart_plug: 'plug',
  smart_light: 'bulb',
  tuya_device: 'plug',
  computer: 'terminal',
  web_service: 'link',
};

function deviceIcon(kind, size) {
  const name = DEVICE_ICONS[kind];
  return icon(name && hasIcon(name) ? name : 'devices', {size: size || 18});
}

function deviceName(device) {
  return device.custom_name || device.name || device.id;
}

function statusBadges(device) {
  const badges = [];
  if (device.online === false) badges.push(h('span', {class: 'badge badge--plain'}, t('devices.status.offline')));
  else badges.push(h('span', {class: 'badge badge--ok'}, t('devices.status.online')));
  if (device.pinned) badges.push(h('span', {class: 'badge badge--accent'}, t('devices.status.pinned')));
  if (device.ignored) badges.push(h('span', {class: 'badge badge--plain'}, t('devices.status.ignored')));
  return badges;
}

/* ---------------------------------------------------------------- view */

export default {
  id: 'devices',
  title: t('nav.devices'),

  async mount(root, ctx) {
    const store = ctx.store;
    let disposed = false;
    const deviceId = ctx.params && ctx.params.id ? String(ctx.params.id) : null;

    /** Patch one device in the shared store list, in place. */
    function patchDevice(id, patch) {
      const list = store.get('devices') || [];
      const next = list.map((d) => (d && d.id === id ? Object.assign({}, d, patch) : d));
      store.set({devices: next});
      return next.find((d) => d && d.id === id) || null;
    }

    function removeDevice(id) {
      const list = store.get('devices') || [];
      store.set({devices: list.filter((d) => !d || d.id !== id)});
    }

    async function updateDevice(device, patch, {successKey, undoLabel} = {}) {
      const before = store.get('devices') || [];
      patchDevice(device.id, patch);
      try {
        const updated = await api.patch('/devices/' + encodeURIComponent(device.id), patch);
        patchDevice(device.id, updated || patch);
        if (successKey) toast(t(successKey, {name: deviceName(device)}), {type: 'success', timeout: 2500});
        return updated;
      } catch (err) {
        store.set({devices: before});
        toast((err && err.message) || t('devices.toast.error'), {type: 'error'});
        throw err;
      }
    }

    if (deviceId) return mountDetail(root, ctx, deviceId, {updateDevice, patchDevice, removeDevice});
    return mountList(root, ctx, {updateDevice, removeDevice});
  },
};

/* ----------------------------------------------------------- list view */

async function mountList(root, ctx, {updateDevice, removeDevice}) {
  const store = ctx.store;
  let disposed = false;

  const state = {
    phase: store.get('devices') ? 'ready' : 'loading',
    error: null,
    scanning: false,
    renamingId: null,
    filter: 'all', // all | online | pinned | ignored
    kind: null,
    summary: null,
  };

  const listSlot = h('div', {class: 'slot'});
  const filterSlot = h('div', {class: 'slot'});
  const noticeSlot = h('div', {class: 'slot'});

  const scanBtn = h('button', {class: 'btn btn--sm', type: 'button', onClick: () => scan()},
    icon('search', {size: 15}), t('devices.rescan'));

  mount(root, [
    h('div', {class: 'page__header'},
      h('div', null,
        h('h2', null, t('nav.devices')),
        h('p', {class: 'page__lead'}, t('devices.lead')),
      ),
      h('div', {class: 'page__actions'}, scanBtn),
    ),
    noticeSlot,
    filterSlot,
    listSlot,
  ]);

  function segmented() {
    const options = [
      ['all', t('devices.filter.all')],
      ['online', t('devices.filter.online')],
      ['pinned', t('devices.filter.pinned')],
      ['ignored', t('devices.filter.ignored')],
    ];
    const seg = h('div', {class: 'segmented', role: 'tablist'},
      options.map(([key, label]) => h('button', {
        class: 'segmented__btn', type: 'button',
        'aria-pressed': String(state.filter === key),
        onClick: () => { state.filter = key; renderFilters(); renderList(); },
      }, label)),
    );

    const devices = store.get('devices') || [];
    const kinds = Array.from(new Set(devices.map((d) => d && d.kind).filter(Boolean))).sort();
    const pills = kinds.length > 1
      ? h('div', {class: 'pills'},
          h('button', {
            class: 'pill' + (state.kind === null ? ' pill--warn' : ''), type: 'button',
            onClick: () => { state.kind = null; renderFilters(); renderList(); },
          }, t('devices.filter.allKinds')),
          kinds.map((kind) => h('button', {
            class: 'pill' + (state.kind === kind ? ' pill--warn' : ''), type: 'button',
            onClick: () => { state.kind = state.kind === kind ? null : kind; renderFilters(); renderList(); },
          }, h('span', {class: 'pill__value'}, fmt.humanize(kind)))),
        )
      : null;

    return h('div', {class: 'stack stack--sm'}, seg, pills);
  }

  function renderFilters() {
    mount(filterSlot, segmented());
  }

  function renderNotice() {
    const s = state.summary;
    if (s && s.available === false) {
      mount(noticeSlot, h('div', {class: 'notice notice--warn'},
        icon('info', {size: 18}),
        h('div', {class: 'notice__body'},
          h('span', null, t('devices.unavailable.title') + (s.reason ? ' — ' + s.reason : '')),
          s.install_hint ? h('span', {class: 'small muted'}, s.install_hint) : null,
        ),
      ));
    } else {
      clear(noticeSlot);
    }
  }

  function filteredDevices() {
    let list = store.get('devices') || [];
    if (state.filter === 'online') list = list.filter((d) => d && d.online !== false && !d.ignored);
    else if (state.filter === 'pinned') list = list.filter((d) => d && d.pinned);
    else if (state.filter === 'ignored') list = list.filter((d) => d && d.ignored);
    else list = list.filter((d) => d && !d.ignored);
    if (state.kind) list = list.filter((d) => d && d.kind === state.kind);
    return list;
  }

  function renameField(device) {
    let value = deviceName(device);
    const input = h('input', {
      type: 'text', value, placeholder: t('devices.rename.placeholder'),
      onKeydown: (event) => { if (event.key === 'Enter') save(); if (event.key === 'Escape') cancel(); },
      onInput: (event) => { value = event.target.value; },
    });
    function cancel() {
      state.renamingId = null;
      renderList();
    }
    async function save() {
      const name = value.trim();
      state.renamingId = null;
      if (!name || name === deviceName(device)) { renderList(); return; }
      try {
        await updateDevice(device, {name}, {successKey: 'devices.toast.renamed'});
      } finally {
        renderList();
      }
    }
    return h('div', {class: 'input-group'},
      input,
      h('button', {class: 'btn btn--icon btn--sm', type: 'button', title: t('devices.action.save'), onClick: save}, icon('check', {size: 14})),
      h('button', {class: 'btn btn--icon btn--sm', type: 'button', title: t('devices.action.cancel'), onClick: cancel}, icon('x', {size: 14})),
    );
  }

  // One box, one row. The scanners are protocol-shaped, so a single television
  // legitimately answers as a Chromecast, as an AirPlay speaker and as a bare IP
  // on the LAN sweep -- three true findings, three rows, and a list where "TV
  // Suíte" appeared four times. Grouping happens here, in the screen, and not in
  // the merge: discovery is right to keep those identities apart (a Cast group
  // shares an address with the speaker leading it), the reader is the one who
  // does not care.
  function groupByAddress(list) {
    const groups = [];
    const byAddress = {};
    list.forEach((device) => {
      const address = (device && device.address) || '';
      if (!address) { groups.push({primary: device, others: []}); return; }
      if (!byAddress[address]) {
        byAddress[address] = {primary: device, others: []};
        groups.push(byAddress[address]);
        return;
      }
      const group = byAddress[address];
      if (isBetterPrimary(device, group.primary)) {
        group.others.push(group.primary);
        group.primary = device;
      } else {
        group.others.push(device);
      }
    });
    return groups;
  }

  // A named device beats an unnamed one; past that, whichever the backend
  // ordered first stays -- it already sorts by kind priority.
  function isBetterPrimary(candidate, current) {
    const named = (d) => Boolean(d && d.name && d.name !== d.address);
    if (named(candidate) !== named(current)) return named(candidate);
    return false;
  }

  function groupKinds(group) {
    const kinds = [];
    [group.primary].concat(group.others).forEach((device) => {
      const label = fmt.humanize((device && device.kind) || '');
      if (label && label.toLowerCase() !== 'unknown' && kinds.indexOf(label) === -1) kinds.push(label);
    });
    return kinds.length ? kinds : [fmt.humanize('unknown')];
  }

  function deviceRow(group) {
    const device = group.primary;
    const busyKey = 'row:' + device.id;
    const isRenaming = state.renamingId === device.id;

    return h('div', {class: 'list__row'},
      deviceIcon(device.kind, 20),
      h('div', {class: 'list__main'},
        isRenaming
          ? renameField(device)
          : h('a', {class: 'list__title', href: '#/devices/' + encodeURIComponent(device.id)}, deviceName(device)),
        h('span', {class: 'list__sub'},
          [groupKinds(group).join(' · '), device.address || '',
           device.last_seen ? fmt.relativeTime(device.last_seen) : '']
            .filter(Boolean).join(' · ')),
      ),
      h('span', {class: 'list__aside row'},
        statusBadges(device),
        h('button', {
          class: 'btn btn--icon btn--sm', type: 'button', title: t('devices.action.rename'),
          onClick: () => { state.renamingId = isRenaming ? null : device.id; renderList(); },
        }, icon('edit', {size: 14})),
        h('button', {
          class: 'btn btn--icon btn--sm' + (device.pinned ? ' is-active' : ''), type: 'button',
          title: device.pinned ? t('devices.action.unpin') : t('devices.action.pin'),
          onClick: () => updateDevice(device, {pinned: !device.pinned}, {
            successKey: device.pinned ? 'devices.toast.unpinned' : 'devices.toast.pinned',
          }).then(renderList).catch(() => {}),
        }, icon('key', {size: 14})),
        h('button', {
          class: 'btn btn--icon btn--sm', type: 'button',
          title: device.ignored ? t('devices.action.unignore') : t('devices.action.ignore'),
          onClick: () => updateDevice(device, {ignored: !device.ignored}, {
            successKey: device.ignored ? 'devices.toast.unignored' : 'devices.toast.ignored',
          }).then(renderList).catch(() => {}),
        }, icon(device.ignored ? 'eye' : 'x', {size: 14})),
      ),
    );
  }

  function renderList() {
    if (state.phase === 'loading') {
      mount(listSlot, skeletonList(4));
      return;
    }
    if (state.phase === 'error') {
      mount(listSlot, errorState(state.error || t('devices.error.load'), load));
      return;
    }
    const devices = filteredDevices();
    const total = (store.get('devices') || []).length;
    if (!devices.length) {
      mount(listSlot, total
        ? emptyState(t('devices.empty.filtered.title'), t('devices.empty.filtered.text'))
        : emptyState(t('devices.empty.title'), t('devices.empty.text'),
            h('button', {class: 'btn btn--sm', type: 'button', onClick: scan},
              icon('search', {size: 15}), t('devices.rescan'))));
      return;
    }
    mount(listSlot, h('div', {class: 'list'}, groupByAddress(devices).map(deviceRow)));
  }

  async function load() {
    state.phase = 'loading';
    renderList();
    try {
      const raw = await api.get('/devices', {query: {include_ignored: true}});
      if (disposed) return;
      state.phase = 'ready';
      state.summary = (raw && raw.summary) || null;
      store.set({devices: (raw && raw.devices) || []});
      renderFilters();
      renderNotice();
      renderList();
    } catch (err) {
      if (disposed) return;
      state.phase = 'error';
      state.error = (err && err.message) || t('devices.error.load');
      renderList();
    }
  }

  async function scan() {
    if (state.scanning) return;
    state.scanning = true;
    scanBtn.disabled = true;
    mount(scanBtn, [icon('search', {size: 15}), t('devices.rescanning')]);
    toast(t('devices.scan.started'), {type: 'info', timeout: 2500});
    try {
      const raw = await api.post('/devices/scan', {});
      if (disposed) return;
      state.summary = (raw && raw.summary) || state.summary;
      store.set({devices: (raw && raw.devices) || (store.get('devices') || [])});
      renderFilters();
      renderNotice();
      renderList();
    } catch (err) {
      if (!disposed) toast((err && err.message) || t('devices.scan.failed'), {type: 'error'});
    } finally {
      state.scanning = false;
      scanBtn.disabled = false;
      mount(scanBtn, [icon('search', {size: 15}), t('devices.rescan')]);
    }
  }

  renderFilters();
  renderList();

  const offStore = store.subscribe((next, prev) => {
    if (disposed) return;
    if (!Object.is(next.devices, prev.devices) && state.phase !== 'loading') {
      renderFilters();
      renderList();
    }
  });

  if (store.get('devices')) load();
  else await load();

  return () => {
    disposed = true;
    offStore();
  };
}

/* --------------------------------------------------------------- detail */

async function mountDetail(root, ctx, deviceId, {updateDevice, patchDevice, removeDevice}) {
  const store = ctx.store;
  let disposed = false;

  const state = {
    device: null,
    devicePhase: 'loading',
    deviceError: null,
    deviceNotFound: false,
    recipes: null,
    recipesPhase: 'loading',
    recipesError: null,
    renaming: false,
    busySteps: new Set(),
  };

  const headSlot = h('div', {class: 'slot'});
  const infoSlot = h('div', {class: 'slot'});
  const recipesSlot = h('div', {class: 'slot'});

  mount(root, [
    h('div', {class: 'page__header'},
      h('div', null,
        h('a', {class: 'small muted', href: '#/devices'}, icon('chevron', {size: 12}), ' ' + t('devices.detail.back')),
      ),
    ),
    headSlot,
    h('div', {class: 'grid grid--wide'}, infoSlot, recipesSlot),
  ]);

  function renderHead() {
    if (state.devicePhase === 'loading') {
      mount(headSlot, h('div', {class: 'card__header'},
        h('div', {class: 'skeleton', style: {width: '40px', height: '40px', borderRadius: '10px'}}),
        h('div', {class: 'skeleton skeleton--title', style: {flex: '1'}}),
      ));
      return;
    }
    if (state.devicePhase === 'error' || !state.device) {
      mount(headSlot, card({
        title: t('devices.detail.notfound.title'),
        iconName: 'devices',
        variant: state.deviceNotFound ? '' : 'danger',
        body: state.deviceNotFound
          ? h('p', {class: 'small muted'}, t('devices.detail.notfound.text'))
          : errorState(state.deviceError || t('devices.detail.error.load'), loadDevice),
      }));
      return;
    }
    const device = state.device;
    const title = state.renaming ? renameField(device) : h('h2', null, deviceName(device));
    mount(headSlot, h('div', {class: 'page__header'},
      h('div', {class: 'row'},
        deviceIcon(device.kind, 28),
        h('div', {class: 'grow'},
          title,
          h('p', {class: 'page__lead'},
            [fmt.humanize(device.kind || 'device'), device.address || ''].filter(Boolean).join(' · ')),
        ),
        h('div', {class: 'row'}, statusBadges(device)),
      ),
      h('div', {class: 'page__actions'},
        h('button', {class: 'btn btn--sm', type: 'button', onClick: () => { state.renaming = !state.renaming; renderHead(); }},
          icon('edit', {size: 14}), t('devices.action.rename')),
        h('button', {
          class: 'btn btn--sm' + (device.pinned ? ' is-active' : ''), type: 'button',
          onClick: () => updateDevice(device, {pinned: !device.pinned}, {
            successKey: device.pinned ? 'devices.toast.unpinned' : 'devices.toast.pinned',
          }).then((updated) => { if (updated) state.device = updated; renderHead(); }).catch(() => {}),
        }, icon('key', {size: 14}), device.pinned ? t('devices.action.unpin') : t('devices.action.pin')),
        h('button', {
          class: 'btn btn--sm', type: 'button',
          onClick: () => updateDevice(device, {ignored: !device.ignored}, {
            successKey: device.ignored ? 'devices.toast.unignored' : 'devices.toast.ignored',
          }).then((updated) => { if (updated) state.device = updated; renderHead(); }).catch(() => {}),
        }, icon(device.ignored ? 'eye' : 'x', {size: 14}), device.ignored ? t('devices.action.unignore') : t('devices.action.ignore')),
        h('button', {class: 'btn btn--sm btn--danger', type: 'button', onClick: forget},
          icon('trash', {size: 14}), t('devices.action.forget')),
      ),
    ));
  }

  function renameField(device) {
    let value = deviceName(device);
    const input = h('input', {
      type: 'text', value, placeholder: t('devices.rename.placeholder'),
      onKeydown: (event) => { if (event.key === 'Enter') save(); if (event.key === 'Escape') cancel(); },
      onInput: (event) => { value = event.target.value; },
    });
    function cancel() { state.renaming = false; renderHead(); }
    async function save() {
      const name = value.trim();
      state.renaming = false;
      if (!name || name === deviceName(device)) { renderHead(); return; }
      try {
        const updated = await updateDevice(device, {name}, {successKey: 'devices.toast.renamed'});
        if (updated) state.device = updated;
      } finally {
        renderHead();
      }
    }
    return h('div', {class: 'input-group'},
      input,
      h('button', {class: 'btn btn--icon btn--sm', type: 'button', onClick: save}, icon('check', {size: 14})),
      h('button', {class: 'btn btn--icon btn--sm', type: 'button', onClick: cancel}, icon('x', {size: 14})),
    );
  }

  async function forget() {
    if (!state.device) return;
    const ok = await confirm(t('devices.forget.confirm', {name: deviceName(state.device)}), {
      title: t('devices.action.forget'), danger: true,
    });
    if (!ok) return;
    try {
      await api.del('/devices/' + encodeURIComponent(deviceId));
      removeDevice(deviceId);
      toast(t('devices.toast.forgot', {name: deviceName(state.device)}), {type: 'success'});
      navigate('#/devices');
    } catch (err) {
      toast((err && err.message) || t('devices.toast.error'), {type: 'error'});
    }
  }

  function renderInfo() {
    if (state.devicePhase === 'loading') {
      mount(infoSlot, skeletonCard());
      return;
    }
    if (state.devicePhase !== 'ready' || !state.device) {
      clear(infoSlot);
      return;
    }
    const device = state.device;
    const caps = Array.isArray(device.capabilities) ? device.capabilities : [];
    const props = device.properties && typeof device.properties === 'object' ? device.properties : {};
    const propKeys = Object.keys(props);

    mount(infoSlot, card({
      title: t('devices.detail.info.title'),
      iconName: 'info',
      body: h('div', {class: 'stack'},
        h('div', {class: 'kv'},
          h('span', {class: 'kv__key'}, t('devices.detail.info.kind')),
          h('span', {class: 'kv__value'}, fmt.humanize(device.kind || '')),
          h('span', {class: 'kv__key'}, t('devices.detail.info.address')),
          h('span', {class: 'kv__value mono'}, device.address ? device.address + (device.port ? ':' + device.port : '') : '—'),
          h('span', {class: 'kv__key'}, t('devices.detail.info.firstSeen')),
          h('span', {class: 'kv__value'}, device.first_seen ? fmt.relativeTime(device.first_seen) : '—'),
          h('span', {class: 'kv__key'}, t('devices.detail.info.lastSeen')),
          h('span', {class: 'kv__value'}, device.last_seen ? fmt.relativeTime(device.last_seen) : '—'),
        ),
        h('div', null,
          h('div', {class: 'small muted'}, t('devices.detail.info.capabilities')),
          caps.length
            ? h('div', {class: 'row'}, caps.map((cap) => h('span', {class: 'tag'}, fmt.humanize(cap))))
            : h('p', {class: 'small faint'}, t('devices.detail.info.none')),
        ),
        propKeys.length
          ? h('div', null,
              h('div', {class: 'small muted'}, t('devices.detail.info.properties')),
              h('div', {class: 'kv'}, propKeys.map((key) => [
                h('span', {class: 'kv__key mono tiny'}, key),
                h('span', {class: 'kv__value mono tiny truncate'}, String(props[key])),
              ]).flat()),
            )
          : null,
      ),
    }));
  }

  /* -------------------------------------------------------------- steps */

  function stepBadgeIcon(step) {
    if (step.kind === 'install') return 'download';
    if (step.kind === 'config') return 'save';
    if (step.kind === 'open') return 'link';
    return 'info';
  }

  async function runStep(recipe, step) {
    const key = recipe.id + ':' + step.index;
    if (state.busySteps.has(key)) return;
    state.busySteps.add(key);
    renderRecipes();
    try {
      const res = await api.post('/recipes/' + encodeURIComponent(recipe.id) + '/run', {
        device_id: deviceId,
        step: step.index,
      });
      if (res && res.kind === 'open' && res.url) {
        window.open(res.url, '_blank', 'noopener,noreferrer');
        toast(t('devices.detail.step.open') + ' · ' + (recipe.title || recipe.id), {type: 'success', timeout: 2000});
      } else if (res && res.kind === 'config') {
        toast(t('devices.detail.step.apply') + ' · ' + (res.key || ''), {type: 'success', timeout: 2000});
      } else if (res && res.kind === 'install') {
        toast((res.app || step.app || t('devices.detail.step.install')) +
          (res.already_installed ? ' · ' + t('devices.detail.step.installed') : ''), {type: 'success'});
      } else {
        toast(t('devices.detail.step.done'), {type: 'success', timeout: 2000});
      }
      await loadRecipes();
    } catch (err) {
      const message = err && err.code === 'does_not_fit'
        ? (err.message || 'This does not fit the board.')
        : err && err.code === 'installer_pending'
          ? (err.message || 'project-os does not know how to install this kind of app yet.')
          : (err && err.message) || t('devices.toast.error');
      toast(message, {type: 'error'});
    } finally {
      state.busySteps.delete(key);
      renderRecipes();
    }
  }

  function stepRow(recipe, step) {
    const key = recipe.id + ':' + step.index;
    const busy = state.busySteps.has(key);

    if (step.kind === 'manual') {
      return h('div', {class: 'list__row'},
        icon('info', {size: 16}),
        h('div', {class: 'list__main'},
          h('span', {class: 'list__title'}, step.text),
          step.note ? h('span', {class: 'list__sub'}, step.note) : null,
          step.command ? h('code', {class: 'mono small'}, step.command) : null,
        ),
        h('span', {class: 'list__aside'}, h('span', {class: 'badge badge--plain'}, t('devices.detail.step.manual'))),
      );
    }

    const done = step.kind === 'install' && step.done;
    const label = step.kind === 'install'
      ? (done ? t('devices.detail.step.installed') : t('devices.detail.step.install'))
      : step.kind === 'open'
        ? t('devices.detail.step.open')
        : t('devices.detail.step.apply');

    return h('div', {class: 'list__row'},
      icon(stepBadgeIcon(step), {size: 16}),
      h('div', {class: 'list__main'},
        h('span', {class: 'list__title'}, step.text),
        step.note ? h('span', {class: 'list__sub'}, step.note) : null,
      ),
      h('span', {class: 'list__aside'},
        h('button', {
          class: 'btn btn--sm' + (done ? ' btn--ghost' : ' btn--primary') + (busy ? ' is-busy' : ''),
          type: 'button', disabled: busy || done,
          onClick: () => runStep(recipe, step),
        }, busy ? h('span', {class: 'spinner spinner--sm'}) : icon(done ? 'check' : 'chevron', {size: 13}), label),
      ),
    );
  }

  function recipeCard(recipe) {
    return card({
      title: recipe.title,
      sub: recipe.summary || null,
      iconName: hasIcon(recipe.icon) ? recipe.icon : 'bulb',
      tools: h('span', {class: 'small muted'}, t('devices.detail.recipes.automatic', {done: recipe.automatic, total: recipe.total})),
      variant: recipe.manual_only ? '' : 'accent',
      body: h('div', {class: 'list'}, recipe.steps.map((step) => stepRow(recipe, step))),
    });
  }

  function renderRecipes() {
    if (state.recipesPhase === 'loading') {
      mount(recipesSlot, [skeletonCard(), skeletonCard()]);
      return;
    }
    if (state.recipesPhase === 'error') {
      mount(recipesSlot, card({
        title: t('devices.detail.recipes.title'),
        iconName: 'flow',
        body: errorState(state.recipesError || t('devices.detail.recipes.error'), loadRecipes),
      }));
      return;
    }
    const recipes = state.recipes || [];
    if (!recipes.length) {
      mount(recipesSlot, card({
        title: t('devices.detail.recipes.title'),
        iconName: 'flow',
        body: emptyState(t('devices.detail.recipes.empty.title'), t('devices.detail.recipes.empty.text')),
      }));
      return;
    }
    mount(recipesSlot, [
      h('h3', null, t('devices.detail.recipes.title')),
      ...recipes.map(recipeCard),
    ]);
  }

  async function loadDevice() {
    state.devicePhase = 'loading';
    renderHead();
    try {
      const device = await api.get('/devices/' + encodeURIComponent(deviceId));
      if (disposed) return;
      state.device = device;
      state.devicePhase = 'ready';
      patchDevice(deviceId, device);
      renderHead();
      renderInfo();
    } catch (err) {
      if (disposed) return;
      state.devicePhase = 'error';
      state.deviceNotFound = Boolean(err && err.status === 404);
      state.deviceError = state.deviceNotFound ? null : ((err && err.message) || t('devices.detail.error.load'));
      state.device = null;
      renderHead();
      renderInfo();
    }
  }

  async function loadRecipes() {
    state.recipesPhase = 'loading';
    renderRecipes();
    try {
      const raw = await api.get('/devices/' + encodeURIComponent(deviceId) + '/recipes');
      if (disposed) return;
      state.recipes = (raw && raw.recipes) || [];
      state.recipesPhase = 'ready';
      renderRecipes();
    } catch (err) {
      if (disposed) return;
      state.recipesPhase = 'error';
      state.recipesError = (err && err.message) || t('devices.detail.recipes.error');
      renderRecipes();
    }
  }

  renderHead();
  renderInfo();
  renderRecipes();

  const offStore = store.subscribe((next, prev) => {
    if (disposed) return;
    if (!Object.is(next.devices, prev.devices) && state.devicePhase === 'ready') {
      const found = (next.devices || []).find((d) => d && d.id === deviceId);
      if (found) { state.device = found; renderHead(); renderInfo(); }
    }
  });

  await Promise.all([loadDevice(), loadRecipes()]);

  return () => {
    disposed = true;
    offStore();
  };
}
