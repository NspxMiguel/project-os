// views/logs.js — Advanced mode: a live tail over system and app logs.
//
// Two sources feed the same list: the history from GET /system/logs (sqlite,
// via project_os/db.py's `log` table) and the live "log" websocket topic that
// project_os/main.py's EventLogHandler publishes for every log record as it
// happens. The websocket payload is {level, source, message} with no
// timestamp of its own -- the outer ws frame carries `ts` -- so a live line
// is stamped from the frame, not the payload.
//
// services.js links here as `#/logs?source=<unit or app source prefix>` so a
// click on "Logs" next to a service lands pre-filtered; that is why the
// source filter reads its initial value from ctx.query.source on mount.

import {h, mount, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';
import {toast} from '../lib/toast.js';

setStrings('en', {
  'logs.title': 'Logs',
  'logs.lead': 'System and app log lines, live.',
  'logs.level.all': 'All levels',
  'logs.source.placeholder': 'Filter by source (e.g. app.birdtunes)',
  'logs.search.placeholder': 'Search text',
  'logs.pause': 'Pause',
  'logs.resume': 'Resume',
  'logs.copy': 'Copy',
  'logs.copied': 'Copied',
  'logs.clear': 'Clear view',
  'logs.paused.notice': 'Live updates paused -- new lines are being held back.',
  'logs.empty.title': 'No log lines match this filter',
  'logs.empty.detail': 'Try a broader level or source filter.',
  'logs.error.title': 'Could not load logs',
  'logs.live': 'Live',
  'logs.offline': 'Not receiving live updates',
}, {activate: false});

// Cap on DOM rows kept at once. Logs can arrive fast (a noisy app in a crash
// loop), and every row is a real DOM node with a text node inside it -- an
// unbounded list would eventually make the tab itself the thing that is
// broken. Older rows are dropped from the top, oldest-first, same as a
// terminal scrollback would.
const MAX_LINES = 1500;

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

function levelClass(level) {
  const up = String(level || '').toUpperCase();
  if (up === 'ERROR' || up === 'CRITICAL') return 'log__line--error';
  if (up === 'WARNING') return 'log__line--warn';
  return '';
}

function errorNotice(message, onRetry) {
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

export default {
  id: 'logs',
  title: t('nav.logs'),

  async mount(root, ctx) {
    let disposed = false;
    let paused = false;
    let level = '';
    let source = (ctx.query && ctx.query.source) || '';
    let search = '';
    let lineCount = 0;
    let liveConnected = false;
    let loadError = null;
    let unsubscribe = null;
    const queue = []; // frames held back while paused

    const headerHost = h('div', {class: 'page__header'});
    const toolbarHost = h('div', {class: 'row', style: {flexWrap: 'wrap', gap: 'var(--space-2)'}});
    const noticeHost = h('div');
    const screen = h('div', {class: 'log', role: 'log', 'aria-live': paused ? 'off' : 'polite'});
    const bodyHost = h('div', {class: 'card'},
      h('div', {class: 'card__body', style: {maxHeight: '65vh', overflow: 'auto'}}, screen));

    const levelSelect = h('select', {
      class: 'input', value: level,
      onChange: (event) => { level = event.target.value; reload(); },
    },
      h('option', {value: ''}, t('logs.level.all')),
      LEVELS.map((lvl) => h('option', {value: lvl}, lvl)),
    );

    const sourceInput = h('input', {
      class: 'input', type: 'text', value: source, placeholder: t('logs.source.placeholder'),
      onChange: (event) => { source = event.target.value.trim(); reload(); },
    });

    const searchInput = h('input', {
      class: 'input', type: 'text', placeholder: t('logs.search.placeholder'),
      onInput: (event) => { search = event.target.value.toLowerCase(); applySearchFilter(); },
    });

    const pauseBtn = h('button', {class: 'btn btn--sm', type: 'button', onClick: togglePause},
      icon('pause', {size: 14}), t('logs.pause'));
    const copyBtn = h('button', {class: 'btn btn--ghost btn--sm', type: 'button', onClick: copyVisible},
      t('logs.copy'));
    const clearBtn = h('button', {class: 'btn btn--ghost btn--sm', type: 'button', onClick: clearView},
      icon('x', {size: 14}), t('logs.clear'));

    mount(root, [
      headerHost,
      h('div', {class: 'stack'}, toolbarHost, noticeHost, bodyHost),
    ]);

    function renderHeader() {
      mount(headerHost, [
        h('div', null,
          h('h2', null, t('logs.title')),
          h('p', {class: 'page__lead'}, t('logs.lead')),
        ),
        h('div', {class: 'page__actions'},
          h('span', {class: 'badge ' + (liveConnected ? 'badge--ok' : 'badge--plain')},
            liveConnected ? t('logs.live') : t('logs.offline')),
        ),
      ]);
    }

    function renderToolbar() {
      mount(toolbarHost, [
        levelSelect,
        sourceInput,
        searchInput,
        h('div', {class: 'row', style: {marginLeft: 'auto', gap: 'var(--space-1)'}},
          pauseBtn, copyBtn, clearBtn),
      ]);
    }

    function renderNotice() {
      mount(noticeHost, paused
        ? h('div', {class: 'notice notice--info'},
            icon('pause', {size: 16}),
            h('div', {class: 'notice__body'}, h('span', {class: 'small muted'}, t('logs.paused.notice') + (queue.length ? ' (' + queue.length + ')' : ''))))
        : null);
    }

    function togglePause() {
      paused = !paused;
      mount(pauseBtn, [icon(paused ? 'play' : 'pause', {size: 14}), paused ? t('logs.resume') : t('logs.pause')]);
      screen.setAttribute('aria-live', paused ? 'off' : 'polite');
      if (!paused) {
        while (queue.length) appendLine(queue.shift());
      }
      renderNotice();
    }

    function clearView() {
      clear(screen);
      lineCount = 0;
    }

    async function copyVisible() {
      const text = Array.from(screen.children).map((node) => node.textContent).join('\n');
      try {
        await navigator.clipboard.writeText(text);
        toast(t('logs.copied'), {type: 'success', timeout: 1500});
      } catch (err) {
        toast((err && err.message) || t('state.error'), {type: 'error'});
      }
    }

    function matchesSearch(node) {
      if (!search) return true;
      return node.dataset.text.includes(search);
    }

    function applySearchFilter() {
      for (const node of screen.children) {
        node.hidden = !matchesSearch(node);
      }
    }

    function formatLine(entry) {
      const ts = entry.ts ? fmt.clockTime(entry.ts) : '';
      return '[' + ts + '] ' + (entry.level || '') + ' ' + (entry.source || '') + ' -- ' + (entry.message || '');
    }

    function appendLine(entry) {
      const text = formatLine(entry);
      const node = h('div', {class: 'small mono ' + levelClass(entry.level)}, text);
      node.dataset.text = text.toLowerCase();
      node.hidden = !matchesSearch(node);
      screen.appendChild(node);
      lineCount += 1;
      trimScrollback();
      screen.scrollTop = screen.scrollHeight;
    }

    function trimScrollback() {
      while (lineCount > MAX_LINES && screen.firstChild) {
        screen.removeChild(screen.firstChild);
        lineCount -= 1;
      }
    }

    function matchesFilters(payload) {
      if (level && String(payload.level || '').toUpperCase() !== level) return false;
      if (source && !String(payload.source || '').startsWith(source)) return false;
      return true;
    }

    function onLiveFrame(frame) {
      liveConnected = true;
      const data = frame.data || {};
      if (!matchesFilters(data)) return;
      const entry = {level: data.level, source: data.source, message: data.message, ts: frame.ts};
      if (paused) {
        queue.push(entry);
        if (queue.length > MAX_LINES) queue.shift();
        renderNotice();
      } else {
        appendLine(entry);
      }
    }

    async function reload() {
      clearView();
      loadError = null;
      renderBody('loading');
      try {
        const raw = await api.get('/system/logs', {
          query: {limit: 200, level: level || null, source: source || null},
        });
        if (disposed) return;
        const lines = Array.isArray(raw.lines) ? raw.lines.slice().reverse() : [];
        if (!lines.length) {
          renderBody('empty');
          return;
        }
        lines.forEach((line) => appendLine({
          level: line.level, source: line.source, message: line.message, ts: line.ts,
        }));
        renderBody('ready');
      } catch (err) {
        if (disposed) return;
        loadError = (err && err.message) || t('logs.error.title');
        renderBody('error');
      }
    }

    function renderBody(state) {
      if (state === 'error') {
        mount(bodyHost, errorNotice(loadError, reload));
        return;
      }
      if (state === 'empty') {
        mount(bodyHost, h('div', {class: 'empty'},
          h('span', {class: 'empty__icon'}, icon('logs', {size: 22})),
          h('p', {class: 'empty__title'}, t('logs.empty.title')),
          h('p', {class: 'empty__text'}, t('logs.empty.detail'))));
        return;
      }
      // 'loading' and 'ready' both show the live-updating card; appendLine()
      // has already populated it for 'ready'.
      mount(bodyHost, h('div', {class: 'card__body', style: {maxHeight: '65vh', overflow: 'auto'}}, screen));
    }

    renderHeader();
    renderToolbar();
    renderNotice();
    sourceInput.value = source;

    if (ctx.ws && typeof ctx.ws.on === 'function') {
      unsubscribe = ctx.ws.on('log', onLiveFrame);
      liveConnected = Boolean(ctx.ws.isOpen && ctx.ws.isOpen());
      renderHeader();
    }

    await reload();

    return () => {
      disposed = true;
      if (typeof unsubscribe === 'function') unsubscribe();
    };
  },
};
