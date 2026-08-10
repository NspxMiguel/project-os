// views/updates.js — updating project-os from project-os.
//
//     "n precisa fica tirando o pendrive, puxa pela rede, tlvz até um dominio
//      nosso"
//
// The one screen here that has to survive its own success: applying an update
// restarts the server, so the last poll before the restart never answers. That
// is not an error state — it is the expected one. After the install job says
// "restarting", this view stops treating failed requests as failures and starts
// waiting for /api/health to answer again, then compares the version it reports
// with the one it was installing.

import {h, mount, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';
import {ApiError} from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';

setStrings('en', {
  'updates.title': 'Updates',
  'updates.lead': 'Pull a new version over the network. No SD card, no laptop.',
  'updates.current': 'This box is running',
  'updates.method.git': 'Updates by git (installed from a clone)',
  'updates.method.tarball': 'Updates from a release manifest',
  'updates.check': 'Check for updates',
  'updates.checking': 'Checking…',
  'updates.uptodate': 'Up to date.',
  'updates.available': 'Version {version} is available.',
  'updates.install': 'Install {version}',
  'updates.installing': 'Installing…',
  'updates.restarting': 'Restarting into the new version…',
  'updates.back': 'Back on {version}.',
  'updates.confirm': 'Update to {version}? project-os will restart. Apps stop for a few seconds; your data is untouched.',
  'updates.notes': 'What changed',
  'updates.source': 'Update source',
  'updates.error.check': 'Could not check for updates.',
  'updates.rollback': 'Go back to the previous version',
  'updates.disabled': 'Updates are turned off in settings.',
  'updates.log': 'Log',
}, {activate: false});

const POLL_MS = 1200;
const RESTART_GRACE_MS = 120000;

function card(options) {
  const {title, sub, iconName, tools, body, footer} = options;
  return h('div', {class: 'card'},
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

export default {
  id: 'updates',
  title: 'Updates',

  async mount(root, ctx) {
    let disposed = false;
    let timer = null;

    const state = {
      status: 'loading',
      error: null,
      info: null,       // GET /updates
      check: null,      // POST /updates/check
      checking: false,
      job: null,
      waiting: false,   // the server is restarting
    };

    const slot = h('div', {class: 'stack stack--lg'});
    mount(root, [
      h('div', {class: 'page__header'},
        h('div', null,
          h('h2', null, t('updates.title')),
          h('p', {class: 'page__lead'}, t('updates.lead')),
        ),
      ),
      slot,
    ]);

    async function load() {
      try {
        state.info = await api.get('/updates');
        if (disposed) return;
        state.job = state.info.job && state.info.job.state !== 'idle' ? state.info.job : null;
        state.check = state.info.last_check || null;
        state.status = 'ready';
      } catch (err) {
        if (disposed) return;
        state.status = 'error';
        state.error = err instanceof ApiError ? err.message : String(err);
      }
      render();
    }

    async function check() {
      state.checking = true;
      render();
      try {
        state.check = await api.post('/updates/check', {});
        if (!state.check.update_available) toast(t('updates.uptodate'), {type: 'success'});
      } catch (err) {
        toast(err instanceof ApiError ? err.message : t('updates.error.check'), {type: 'error'});
      } finally {
        state.checking = false;
        render();
      }
    }

    async function install() {
      const version = state.check && state.check.latest;
      const ok = await confirm(t('updates.confirm', {version}));
      if (!ok) return;
      try {
        const data = await api.post('/updates/install', {version});
        state.job = data.job;
        render();
        follow();
      } catch (err) {
        toast(err instanceof ApiError ? err.message : String(err), {type: 'error'});
      }
    }

    function follow() {
      if (timer) clearTimeout(timer);
      if (disposed) return;
      timer = setTimeout(async () => {
        try {
          const data = await api.get('/updates');
          if (disposed) return;
          state.job = data.job;
          state.info = data;
          if (state.job.state === 'restarting') {
            state.waiting = true;
            render();
            waitForRestart();
            return;
          }
          render();
          if (state.job.state === 'running') follow();
          else if (state.job.state === 'error') toast(state.job.message, {type: 'error'});
        } catch (err) {
          // A dropped request mid-update usually means the restart began
          // before we saw the state change. Treat it as such rather than as
          // a failure the user has to interpret.
          if (state.job && state.job.state !== 'idle') {
            state.waiting = true;
            render();
            waitForRestart();
          } else if (!disposed) {
            follow();
          }
        }
      }, POLL_MS);
    }

    function waitForRestart() {
      const deadline = Date.now() + RESTART_GRACE_MS;
      const target = state.job && state.job.target;
      const tick = async () => {
        if (disposed) return;
        try {
          const health = await api.get('/health');
          if (!disposed && health && health.version) {
            state.waiting = false;
            toast(t('updates.back', {version: health.version}), {type: 'success'});
            await load();
            state.check = null;
            render();
            return;
          }
        } catch (err) {
          // still down; that is what "restarting" looks like from here
        }
        if (Date.now() > deadline) {
          state.waiting = false;
          toast('The box did not come back within two minutes. Check it directly.', {type: 'error'});
          render();
          return;
        }
        timer = setTimeout(tick, POLL_MS);
      };
      void target;
      timer = setTimeout(tick, POLL_MS * 2);
    }

    function statusCard() {
      const info = state.info || {};
      const methodLabel = info.method === 'git' ? t('updates.method.git') : t('updates.method.tarball');
      return card({
        title: t('updates.current') + ' ' + (info.version || '?'),
        sub: methodLabel,
        iconName: 'system',
        body: h('div', {class: 'stack stack--sm'},
          h('div', {class: 'field-row'},
            h('span', {class: 'field-row__label'}, t('updates.source')),
            h('span', {class: 'field-row__value muted small'},
              info.method === 'git' ? (info.branch || 'main') : (info.manifest_url || '—')),
          ),
          info.enabled === false ? h('div', {class: 'notice notice--warning'}, t('updates.disabled')) : null,
        ),
        footer: h('div', {class: 'row'},
          h('button', {
            class: 'btn', type: 'button', disabled: state.checking || state.waiting,
            onClick: () => check(),
          }, state.checking ? t('updates.checking') : t('updates.check')),
        ),
      });
    }

    function availableCard() {
      const check = state.check;
      if (!check) return null;
      if (!check.update_available) {
        return card({
          title: t('updates.uptodate'), iconName: 'info',
          body: h('p', {class: 'muted'}, check.notes || ''),
        });
      }
      return card({
        title: t('updates.available', {version: check.latest}),
        iconName: 'download',
        body: check.notes
          ? h('div', {class: 'stack stack--sm'},
              h('div', {class: 'field-row__label'}, t('updates.notes')),
              h('p', null, check.notes))
          : null,
        footer: h('div', {class: 'row'},
          h('button', {
            class: 'btn btn--primary', type: 'button',
            disabled: state.waiting || (state.job && state.job.state === 'running'),
            onClick: () => install(),
          }, t('updates.install', {version: check.latest})),
        ),
      });
    }

    function jobCard() {
      const job = state.job;
      if (!job || job.state === 'idle') return null;
      const label = state.waiting ? t('updates.restarting')
        : job.state === 'running' ? t('updates.installing')
        : job.message || job.state;
      return card({
        title: label, iconName: 'terminal',
        tools: h('span', {class: 'conn', dataset: {state:
          job.state === 'error' ? 'closed'
            : job.state === 'done' ? 'open'
            : 'connecting'}},
          h('span', {class: 'conn__dot'}), job.state),
        body: h('div', {class: 'stack stack--sm'},
          h('pre', {class: 'log log--compact'}, (job.log || []).join('\n') || ' '),
          job.previous
            ? h('button', {
                class: 'btn btn--sm btn--danger', type: 'button',
                onClick: async () => {
                  try {
                    await api.post('/updates/rollback', {});
                    toast('Rolled back.', {type: 'success'});
                  } catch (err) {
                    toast(err instanceof ApiError ? err.message : String(err), {type: 'error'});
                  }
                },
              }, t('updates.rollback'))
            : null,
        ),
      });
    }

    function render() {
      if (disposed) return;
      clear(slot);
      if (state.status === 'loading') {
        mount(slot, h('div', {class: 'card'},
          h('div', {class: 'card__body'}, h('div', {class: 'skeleton skeleton--text'}))));
        return;
      }
      if (state.status === 'error') {
        mount(slot, h('div', {class: 'notice notice--error'},
          icon('warning', {size: 18}),
          h('div', {class: 'notice__body'}, h('span', null, state.error))));
        return;
      }
      mount(slot, [statusCard(), jobCard(), availableCard()]);
    }

    render();
    await load();
    if (state.job && (state.job.state === 'running' || state.job.state === 'restarting')) follow();

    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  },
};
