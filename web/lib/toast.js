// toast.js — transient notifications + a real modal confirm().
//
//   toast('Library scanned', {type: 'success'});
//   if (await confirm('Delete this track?', {danger: true})) ...
//
// Toasts live in an aria-live="polite" region so a screen reader announces
// them without stealing focus. confirm() uses <dialog>.showModal(), which
// gives focus trapping and Escape-to-cancel from the platform, with a manual
// fallback for browsers without dialog support.

import {h, clear} from './dom.js';
import {icon} from './icons.js';
import {t} from './format.js';

const TYPE_ICON = {
  info: 'info',
  success: 'check',
  warn: 'warning',
  error: 'warning',
};

let region = null;

function ensureRegion() {
  if (region && region.isConnected) return region;
  region = document.getElementById('toast-region');
  if (!region) {
    region = h('div', {
      id: 'toast-region',
      class: 'toast-region',
      role: 'status',
      'aria-live': 'polite',
      'aria-atomic': 'false',
    });
    document.body.appendChild(region);
  }
  return region;
}

/** Show a toast. Returns a function that dismisses it early. */
export function toast(message, options = {}) {
  const {
    type = 'info',
    timeout = type === 'error' ? 8000 : 4000,
    title = '',
    action = null, // {label, onClick}
  } = options;

  const host = ensureRegion();
  let timer = null;
  let removed = false;

  const node = h('div', {class: 'toast toast--' + type, role: type === 'error' ? 'alert' : null},
    h('span', {class: 'toast__icon'}, icon(TYPE_ICON[type] || 'info', {size: 18})),
    h('div', {class: 'toast__body'},
      title ? h('strong', {class: 'toast__title'}, title) : null,
      h('span', {class: 'toast__message'}, String(message === null || message === undefined ? '' : message)),
    ),
    action && action.label
      ? h('button', {
          class: 'btn btn--ghost btn--sm toast__action',
          type: 'button',
          onClick: () => {
            try {
              if (typeof action.onClick === 'function') action.onClick();
            } finally {
              dismiss();
            }
          },
        }, action.label)
      : null,
    h('button', {
      class: 'btn btn--icon btn--sm toast__close',
      type: 'button',
      'aria-label': t('action.close'),
      onClick: () => dismiss(),
    }, icon('x', {size: 16})),
  );

  function dismiss() {
    if (removed) return;
    removed = true;
    if (timer) clearTimeout(timer);
    node.classList.add('toast--leaving');
    const drop = () => {
      if (node.parentNode) node.parentNode.removeChild(node);
    };
    // Respect reduced motion: no animation means no animationend event.
    const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) drop();
    else setTimeout(drop, 200);
  }

  node.addEventListener('mouseenter', () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  });
  node.addEventListener('mouseleave', () => {
    if (!removed && timeout > 0 && !timer) timer = setTimeout(dismiss, 1500);
  });

  host.appendChild(node);
  // Keep the stack bounded — a chatty app must not fill the screen.
  while (host.children.length > 5) host.removeChild(host.firstChild);

  if (timeout > 0) timer = setTimeout(dismiss, timeout);
  return dismiss;
}

export const notify = toast;

const FOCUSABLE = [
  'button:not([disabled])', '[href]', 'input:not([disabled])', 'select:not([disabled])',
  'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])',
].join(',');

/** Modal confirmation. Resolves true on confirm, false on cancel/Escape. */
export function confirm(message, options = {}) {
  const {
    title = '',
    confirmLabel = t('action.confirm'),
    cancelLabel = t('action.cancel'),
    danger = false,
    detail = '',
  } = options;

  return new Promise((resolve) => {
    const previous = document.activeElement;
    let settled = false;

    const finish = (value) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };

    const confirmBtn = h('button', {
      class: 'btn ' + (danger ? 'btn--danger' : 'btn--primary'),
      type: 'button',
      onClick: () => finish(true),
    }, confirmLabel);

    const cancelBtn = h('button', {
      class: 'btn btn--ghost',
      type: 'button',
      onClick: () => finish(false),
    }, cancelLabel);

    const panel = h('div', {class: 'modal__panel', role: 'document'},
      title ? h('h2', {class: 'modal__title'}, title) : null,
      h('p', {class: 'modal__message'}, String(message === null || message === undefined ? '' : message)),
      detail ? h('p', {class: 'modal__detail'}, detail) : null,
      h('div', {class: 'modal__actions'}, cancelBtn, confirmBtn),
    );

    const supportsDialog = typeof HTMLDialogElement !== 'undefined' &&
      typeof HTMLDialogElement.prototype.showModal === 'function';

    let host;
    let backdrop = null;

    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        finish(false);
        return;
      }
      if (event.key !== 'Tab' || supportsDialog) return;
      // Manual focus trap for the no-<dialog> fallback.
      const items = Array.from(panel.querySelectorAll(FOCUSABLE)).filter((el) => el.offsetParent !== null);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    function cleanup() {
      document.removeEventListener('keydown', onKeyDown, true);
      if (supportsDialog && host && typeof host.close === 'function' && host.open) {
        try {
          host.close();
        } catch (err) {
          /* ignore */
        }
      }
      if (host && host.parentNode) host.parentNode.removeChild(host);
      if (backdrop && backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
      document.body.classList.remove('has-modal');
      if (previous && typeof previous.focus === 'function') {
        try {
          previous.focus();
        } catch (err) {
          /* ignore */
        }
      }
    }

    if (supportsDialog) {
      host = h('dialog', {class: 'modal', 'aria-modal': 'true'}, panel);
      host.addEventListener('cancel', (event) => {
        event.preventDefault();
        finish(false);
      });
      host.addEventListener('click', (event) => {
        if (event.target === host) finish(false); // click on the backdrop
      });
      document.body.appendChild(host);
      document.body.classList.add('has-modal');
      host.showModal();
    } else {
      backdrop = h('div', {class: 'modal__backdrop', onClick: () => finish(false)});
      host = h('div', {class: 'modal modal--fallback', role: 'dialog', 'aria-modal': 'true'}, panel);
      document.body.appendChild(backdrop);
      document.body.appendChild(host);
      document.body.classList.add('has-modal');
    }

    document.addEventListener('keydown', onKeyDown, true);
    confirmBtn.focus();
  });
}

/** Remove every visible toast (used when the shell re-boots). */
export function clearToasts() {
  if (region) clear(region);
}

export default {toast, confirm, notify, clearToasts};
