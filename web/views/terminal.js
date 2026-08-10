// views/terminal.js — Advanced mode: a real shell on the box, in the browser.
//
// This exists because of one request: a corner button that "serve pra
// qualquer coisa, baixa apps e etc" -- something that can install a package
// or poke at a file without SSH. project_os/api/shell.py backs it with a real
// pty (TERM=xterm-256color), which means the byte stream can contain full
// ANSI escape sequences (cursor moves, colors, alternate screens). There is
// no xterm.js here by policy (zero build step, no bundler), so this view
// does NOT try to be a real terminal emulator: it strips escape sequences
// down to their printable text and renders that as a scrolling log. Curses
// UIs (top, vim, less -R with color) will look wrong or unreadable -- that
// is a known, documented limitation, not a bug to chase.
//
// createTerminal() below is the reusable engine (connection, history,
// scrollback, key handling) with no assumption about where its DOM node
// lives, so a future docked corner widget in app.js can reuse it without
// duplicating the pty protocol handling. The default view export is a thin
// full-screen wrapper around it.

import {h, mount, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';

setStrings('en', {
  'terminal.title': 'Terminal',
  'terminal.lead': 'A real shell on this machine. Anything you can type at an SSH prompt works here.',
  'terminal.disabled': 'The terminal is turned off',
  'terminal.disabled.detail': 'Turn on security.allow_shell in Settings to use it.',
  'terminal.noPty': 'No pty is available on this machine',
  'terminal.connecting': 'Connecting…',
  'terminal.connected': 'Connected',
  'terminal.disconnected': 'Disconnected',
  'terminal.reconnect': 'Reconnect',
  'terminal.clear': 'Clear',
  'terminal.interrupt': 'Send Ctrl-C',
  'terminal.placeholder': 'Type a command and press Enter',
  'terminal.limitation': 'Full-screen programs (top, vim, less) will render as garbled text -- there is no terminal emulator here, only a plain scrollback.',
  'terminal.exited': 'The shell process exited.',
  'terminal.ready': '{shell} (pid {pid})',
  'terminal.unauthenticated': 'Not signed in.',
}, {activate: false});

/* ------------------------------------------------------------- ANSI/text */

// Strips CSI/OSC/other escape sequences down to printable text. This is not
// a terminal emulator -- it throws away cursor positioning and color, which
// is exactly why curses UIs look wrong here. It keeps \n so multi-line
// output still wraps sanely.
const ESCAPE_RE = new RegExp(
  '\\x1b(' +
    '\\][^\\x07\\x1b]*(\\x07|\\x1b\\\\)' + // OSC ... BEL or ST
    '|\\[[0-9;?]*[ -/]*[@-~]' + // CSI ... final byte
    '|[@-Z\\\\-_]' + // two-byte escapes
  ')',
  'g'
);

function stripAnsi(text) {
  return text
    .replace(ESCAPE_RE, '')
    // eslint-disable-next-line no-control-regex
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n');
}

/* --------------------------------------------------------- terminal core */

/**
 * createTerminal(options) -> {
 *   element: HTMLElement,           // mount this wherever you like
 *   connect(): void,
 *   disconnect(): void,
 *   clear(): void,
 *   focus(): void,
 *   sendInterrupt(): void,
 *   destroy(): void,                // disconnects, clears timers, drops DOM refs
 *   get connected(): boolean,
 * }
 *
 * options:
 *   maxLines    (number, default 3000) -- scrollback cap, see comment below
 *   historySize (number, default 100)  -- command history cap
 *   autoConnect (boolean, default true)
 *   onState(state)  -- called with 'connecting'|'open'|'closed'|'disabled'|'error'
 */
export function createTerminal(options = {}) {
  const {
    maxLines = 3000,
    historySize = 100,
    autoConnect = true,
    onState = null,
  } = options;

  let socket = null;
  let state = 'closed';
  let destroyed = false;
  let lineCount = 0;
  let pendingLine = null; // text node of the in-progress (unterminated) line
  let history = [];
  let historyIndex = -1;
  let draft = '';

  const screen = h('div', {class: 'log', role: 'log', 'aria-live': 'polite', tabIndex: -1});
  const input = h('input', {
    class: 'input mono', type: 'text', autocomplete: 'off', autocapitalize: 'off',
    spellcheck: false, placeholder: t('terminal.placeholder'),
    onKeydown: handleKeydown,
  });

  const element = h('div', {class: 'stack', style: {height: '100%'}},
    h('div', {class: 'log-shell', style: {flex: '1', minHeight: 0, overflow: 'auto'}}, screen),
    h('div', {class: 'input-group'}, input),
  );

  function setState(next) {
    state = next;
    if (typeof onState === 'function') onState(next);
  }

  function writeLine(text, cls) {
    lineCount += 1;
    screen.appendChild(h('div', {class: cls ? 'small mono ' + cls : 'small mono'}, text));
    trimScrollback();
    scrollToEnd();
  }

  // Output arrives in arbitrary chunks, not line-aligned, so a chunk without
  // a trailing newline keeps appending to the same DOM text node instead of
  // starting a new <div> per chunk -- otherwise a slow `cat` would fragment
  // one logical line into dozens of rows.
  function writeChunk(raw) {
    const text = stripAnsi(raw);
    if (!text) return;
    const parts = text.split('\n');
    parts.forEach((part, i) => {
      if (i === 0 && pendingLine) {
        pendingLine.textContent += part;
      } else {
        pendingLine = document.createElement('div');
        pendingLine.className = 'small mono';
        pendingLine.textContent = part;
        screen.appendChild(pendingLine);
        lineCount += 1;
      }
      if (i < parts.length - 1) pendingLine = null; // that line is now terminated
    });
    trimScrollback();
    scrollToEnd();
  }

  // DOM nodes are not free, and a long-lived session (someone tailing a
  // build for an hour) would otherwise grow the log div without bound and
  // eventually stall the tab. Trimming from the front keeps memory flat
  // while leaving the visible/scrollback-relevant tail intact.
  function trimScrollback() {
    while (lineCount > maxLines && screen.firstChild) {
      if (screen.firstChild === pendingLine) break;
      screen.removeChild(screen.firstChild);
      lineCount -= 1;
    }
  }

  function scrollToEnd() {
    const shell = element.querySelector('.log-shell');
    if (shell) shell.scrollTop = shell.scrollHeight;
  }

  function wsUrl() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + window.location.host + api.url('/shell/ws');
  }

  function connect() {
    if (destroyed || socket) return;
    setState('connecting');
    writeLine(t('terminal.connecting'), 'faint');
    let ws;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      setState('error');
      writeLine(String((err && err.message) || err), 'log__line--error');
      return;
    }
    socket = ws;

    ws.addEventListener('open', () => {
      setState('open');
    });

    ws.addEventListener('message', (event) => {
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      if (frame.type === 'ready') {
        writeLine(t('terminal.ready', {shell: frame.shell || 'shell', pid: frame.pid}), 'faint');
      } else if (frame.type === 'output') {
        writeChunk(String(frame.data || ''));
      } else if (frame.type === 'exit') {
        pendingLine = null;
        writeLine(t('terminal.exited'), 'faint');
      } else if (frame.type === 'error') {
        writeLine(String(frame.message || 'error'), 'log__line--error');
      }
    });

    ws.addEventListener('close', (event) => {
      socket = null;
      pendingLine = null;
      if (destroyed) return;
      setState('closed');
      const reason = closeReason(event.code);
      writeLine(reason || t('terminal.disconnected'), 'faint');
    });

    ws.addEventListener('error', () => {
      // The 'close' event that follows carries the code/reason; nothing
      // useful to add here beyond letting that handler report it.
    });
  }

  function closeReason(code) {
    if (code === 4401) return t('terminal.unauthenticated');
    if (code === 4403) return t('terminal.disabled');
    if (code === 4501) return t('terminal.noPty');
    return null;
  }

  function disconnect() {
    if (socket) {
      try { socket.close(); } catch (err) { /* already closing */ }
      socket = null;
    }
  }

  function send(obj) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(obj));
    }
  }

  function runCommand(command) {
    if (!command) return;
    history.push(command);
    if (history.length > historySize) history.shift();
    historyIndex = history.length;
    draft = '';
    send({type: 'input', data: command + '\n'});
  }

  function handleKeydown(event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      const value = input.value;
      input.value = '';
      runCommand(value);
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      if (!history.length) return;
      if (historyIndex === history.length) draft = input.value;
      historyIndex = Math.max(0, historyIndex - 1);
      input.value = history[historyIndex] || '';
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      if (historyIndex >= history.length) return;
      historyIndex += 1;
      input.value = historyIndex === history.length ? draft : history[historyIndex];
      return;
    }
    if (event.key === 'c' && event.ctrlKey) {
      event.preventDefault();
      sendInterrupt();
      return;
    }
    if (event.key === 'l' && event.ctrlKey) {
      event.preventDefault();
      clearScreen();
    }
  }

  function sendInterrupt() {
    send({type: 'input', data: '\x03'});
  }

  function clearScreen() {
    clear(screen);
    lineCount = 0;
    pendingLine = null;
  }

  if (autoConnect) connect();

  return {
    element,
    connect,
    disconnect,
    clear: clearScreen,
    focus: () => input.focus(),
    sendInterrupt,
    get connected() { return state === 'open'; },
    get state() { return state; },
    destroy() {
      destroyed = true;
      disconnect();
    },
  };
}

/* ------------------------------------------------------------------ view */

export default {
  id: 'terminal',
  title: t('nav.terminal'),

  async mount(root, ctx) {
    let disposed = false;
    let term = null;
    let status = null;

    const headerHost = h('div', {class: 'page__header'});
    const bodyHost = h('div', {class: 'card', style: {display: 'flex', flexDirection: 'column', minHeight: '60vh', height: '70vh'}});

    mount(root, [headerHost, bodyHost]);

    function renderHeader(connState) {
      const label = connState === 'open' ? t('terminal.connected')
        : connState === 'connecting' ? t('terminal.connecting')
        : t('terminal.disconnected');
      const dotClass = connState === 'open' ? 'badge--ok' : connState === 'connecting' ? 'badge--warn' : 'badge--plain';
      mount(headerHost, [
        h('div', null,
          h('h2', null, t('terminal.title')),
          h('p', {class: 'page__lead'}, t('terminal.lead')),
        ),
        h('div', {class: 'page__actions'},
          h('span', {class: 'badge ' + dotClass}, label),
          h('button', {
            class: 'btn btn--ghost btn--sm', type: 'button',
            onClick: () => term && term.sendInterrupt(),
          }, icon('warning', {size: 14}), t('terminal.interrupt')),
          h('button', {
            class: 'btn btn--ghost btn--sm', type: 'button',
            onClick: () => term && term.clear(),
          }, icon('x', {size: 14}), t('terminal.clear')),
          h('button', {
            class: 'btn btn--sm', type: 'button',
            onClick: () => term && term.connect(),
          }, icon('refresh', {size: 14}), t('terminal.reconnect')),
        ),
      ]);
    }

    function renderDisabled(message, detail) {
      mount(bodyHost, h('div', {class: 'notice notice--info'},
        icon('lock', {size: 18}),
        h('div', {class: 'notice__body'},
          h('strong', null, message),
          detail ? h('p', {class: 'small muted'}, detail) : null,
        ),
      ));
    }

    try {
      status = await api.get('/shell/status');
    } catch (err) {
      status = null;
      if (!disposed) {
        renderHeader('closed');
        renderDisabled((err && err.message) || t('state.error'));
      }
      return () => { disposed = true; };
    }

    if (disposed) return () => {};

    if (!status.enabled) {
      renderHeader('closed');
      renderDisabled(t('terminal.disabled'), status.reason || t('terminal.disabled.detail'));
      return () => { disposed = true; };
    }
    if (!status.pty_available) {
      renderHeader('closed');
      renderDisabled(t('terminal.noPty'), status.reason || null);
      return () => { disposed = true; };
    }

    term = createTerminal({
      onState: (next) => { if (!disposed) renderHeader(next); },
    });
    renderHeader(term.state);
    mount(bodyHost, [
      h('div', {class: 'tiny faint', style: {padding: '0 var(--space-2)'}}, t('terminal.limitation')),
      term.element,
    ]);
    term.focus();

    return () => {
      disposed = true;
      if (term) term.destroy();
    };
  },
};
