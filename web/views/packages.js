// views/packages.js — Advanced mode's package manager. The screen that makes
// "advanced" mean a Linux box rather than a set of admin panels:
//
//     "vc tem um computador completo no seu navegador"
//     "o cara foi la, foi pro modo advanced, instalo um firefox, um flatpack
//      store, e dps voltau pro modo simple. ai aparece em apps."
//
// Search hits apt and flatpak; installing starts a job and this view follows its
// log until it ends. The log is shown by default rather than hidden behind a
// "details" link — an apt run on a Pi 3B takes minutes, and a progress bar that
// cannot say *what* it is doing is indistinguishable from a hung request.
//
// Two states this screen must never fake:
//   * a backend that is not on this machine (a Mac has no apt) says so, with
//     the reason, instead of returning zero results;
//   * an install that cannot become root is refused before it starts, and the
//     refusal names the fix.

import {h, mount, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';
import {ApiError} from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';

setStrings('en', {
  'packages.title': 'Software',
  'packages.lead': 'Install programs on this machine. What you install here shows up in Apps.',
  'packages.search.placeholder': 'Search for a program — firefox, htop, vlc…',
  'packages.search.action': 'Search',
  'packages.search.hint': 'At least two characters.',
  'packages.empty.title': 'Nothing found',
  'packages.empty.text': 'Try another name. apt matches package names, not descriptions.',
  'packages.start.title': 'What do you want to install?',
  'packages.start.text': 'Search above. Results come from this machine’s own package managers.',
  'packages.installed': 'Installed',
  'packages.action.install': 'Install',
  'packages.action.remove': 'Remove',
  'packages.action.working': 'Working…',
  'packages.remove.confirm': 'Remove {name}? Anything that depends on it may stop working.',
  'packages.disabled': 'Installing is turned off. Switch on security.allow_package_management in Settings › Developer.',
  'packages.disabled.link': 'Open Settings > Developer',
  'packages.backends.title': 'Package managers on this machine',
  'packages.backend.ready': 'ready',
  'packages.backend.unavailable': 'not usable',
  'packages.skipped': '{source} was skipped: {reason}',
  'packages.job.title': '{action} {package}',
  'packages.job.running': 'running',
  'packages.job.done': 'done',
  'packages.job.error': 'failed',
  'packages.job.queued': 'queued',
  'packages.jobs.recent': 'Recent',
  'packages.error.load': 'Could not read the package managers.',
  'packages.action.retry': 'Retry',
}, {activate: false});

const POLL_MS = 1500;

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

const JOB_STATE_LABEL = {
  queued: 'packages.job.queued',
  running: 'packages.job.running',
  done: 'packages.job.done',
  error: 'packages.job.error',
};

function jobConnState(state) {
  if (state === 'running') return 'connecting';
  if (state === 'error') return 'closed';
  if (state === 'done') return 'open';
  return 'neutral';
}

export default {
  id: 'packages',
  get title() { return t('nav.packages'); },

  async mount(root, ctx) {
    let disposed = false;
    let timer = null;

    const state = {
      status: 'loading',   // loading | ready | error
      error: null,
      backends: [],
      enabled: true,
      query: '',
      results: null,       // null until a search has run
      skipped: [],
      searching: false,
      job: null,           // the job being followed, with its log
      recent: [],
      busy: '',            // "source:package" while a request is in flight
    };

    const slot = h('div', {class: 'stack stack--lg'});
    mount(root, [
      h('div', {class: 'page__header'},
        h('div', null,
          h('h2', null, t('packages.title')),
          h('p', {class: 'page__lead'}, t('packages.lead')),
        ),
      ),
      slot,
    ]);

    /* ------------------------------------------------------------ data */

    async function loadOverview() {
      try {
        const data = await api.get('/packages');
        if (disposed) return;
        state.backends = Array.isArray(data.backends) ? data.backends : [];
        state.enabled = data.enabled !== false;
        state.recent = Array.isArray(data.jobs) ? data.jobs : [];
        state.status = 'ready';
      } catch (err) {
        if (disposed) return;
        state.status = 'error';
        state.error = err instanceof ApiError ? err.message : t('packages.error.load');
      }
      render();
    }

    async function runSearch() {
      const query = state.query.trim();
      if (query.length < 2) {
        toast(t('packages.search.hint'), {type: 'warning'});
        return;
      }
      state.searching = true;
      render();
      try {
        const data = await api.get('/packages/search', {query: {q: query}});
        if (disposed) return;
        state.results = Array.isArray(data.items) ? data.items : [];
        state.skipped = Array.isArray(data.skipped) ? data.skipped : [];
      } catch (err) {
        if (disposed) return;
        toast(err instanceof ApiError ? err.message : String(err), {type: 'error'});
      } finally {
        state.searching = false;
        render();
      }
    }

    async function act(action, item) {
      const key = item.source + ':' + item.id;
      if (state.busy) return;
      if (action === 'remove') {
        const ok = await confirm(t('packages.remove.confirm', {name: item.name || item.id}));
        if (!ok) return;
      }
      state.busy = key;
      render();
      try {
        const data = await api.post('/packages/' + action, {source: item.source, package: item.id});
        if (disposed) return;
        state.job = data.job;
        follow();
      } catch (err) {
        if (disposed) return;
        // A refusal here is the useful kind — "needs root", "apt is not here",
        // "another job is running" — so it goes on screen verbatim.
        toast(err instanceof ApiError ? err.message : String(err), {type: 'error'});
      } finally {
        state.busy = '';
        render();
      }
    }

    function follow() {
      if (timer) clearTimeout(timer);
      if (!state.job || disposed) return;
      timer = setTimeout(async () => {
        try {
          const data = await api.get('/packages/jobs/' + encodeURIComponent(state.job.id));
          if (disposed) return;
          state.job = data.job;
          render();
          if (state.job.state === 'running' || state.job.state === 'queued') {
            follow();
          } else {
            const ok = state.job.state === 'done';
            toast(state.job.message || state.job.state, {type: ok ? 'success' : 'error'});
            if (ok && state.results) await runSearch();
            await loadOverview();
          }
        } catch (err) {
          if (!disposed) render();
        }
      }, POLL_MS);
    }

    /* ---------------------------------------------------------- render */

    function backendsCard() {
      return card({
        title: t('packages.backends.title'), iconName: 'apps',
        body: h('div', {class: 'stack stack--sm'},
          state.backends.map((backend) => h('div', {class: 'field-row'},
            h('span', {class: 'field-row__label'}, backend.name),
            h('span', {class: 'field-row__value'},
              h('span', {class: 'conn', dataset: {state: backend.can_install ? 'open' : 'closed'}},
                h('span', {class: 'conn__dot'}),
                backend.can_install ? t('packages.backend.ready') : t('packages.backend.unavailable'),
              ),
            ),
          )),
          state.backends.filter((b) => !b.can_install && b.reason).map(
            (b) => h('p', {class: 'muted small'}, b.reason),
          ),
          !state.enabled ? h('div', {class: 'notice notice--warn'},
            h('div', {class: 'notice__body'},
              h('span', null, t('packages.disabled')),
              h('a', {class: 'btn btn--sm btn--outline', href: '#/settings/developer'},
                t('packages.disabled.link')))) : null,
        ),
      });
    }

    function searchCard() {
      const input = h('input', {
        class: 'input grow', type: 'search', value: state.query,
        placeholder: t('packages.search.placeholder'),
        onInput: (event) => { state.query = event.target.value; },
        onKeyDown: (event) => { if (event.key === 'Enter') runSearch(); },
      });
      return card({
        title: t('packages.title'), iconName: 'search',
        body: h('div', {class: 'input-group'},
          input,
          h('button', {
            class: 'btn btn--primary', type: 'button', disabled: state.searching,
            onClick: () => runSearch(),
          }, state.searching ? t('packages.action.working') : t('packages.search.action')),
        ),
      });
    }

    function resultRow(item) {
      const key = item.source + ':' + item.id;
      const working = state.busy === key;
      const disabled = working || !state.enabled || Boolean(state.busy);
      return h('div', {class: 'list__row'},
        h('div', {class: 'list__main'},
          h('div', {class: 'list__title'}, item.name || item.id),
          h('div', {class: 'list__sub'},
            [item.source, item.version, item.summary].filter(Boolean).join(' · '),
          ),
        ),
        h('div', {class: 'list__actions'},
          item.installed ? h('span', {class: 'badge badge--ok'}, t('packages.installed')) : null,
          h('button', {
            class: 'btn btn--sm' + (item.installed ? ' btn--danger' : ' btn--primary'),
            type: 'button', disabled,
            onClick: () => act(item.installed ? 'remove' : 'install', item),
          }, working
            ? t('packages.action.working')
            : item.installed ? t('packages.action.remove') : t('packages.action.install')),
        ),
      );
    }

    function resultsCard() {
      if (state.results === null) {
        return card({
          title: t('packages.start.title'), iconName: 'info',
          body: h('p', {class: 'muted'}, t('packages.start.text')),
        });
      }
      const skipped = state.skipped.map((entry) => h('p', {class: 'muted small'},
        t('packages.skipped', {source: entry.source, reason: entry.reason})));
      if (!state.results.length) {
        return card({
          title: t('packages.empty.title'), iconName: 'info',
          body: h('div', {class: 'stack stack--sm'},
            h('p', {class: 'muted'}, t('packages.empty.text')), skipped),
        });
      }
      return card({
        title: t('packages.title'), iconName: 'apps',
        body: h('div', {class: 'stack stack--sm'},
          h('div', {class: 'list'}, state.results.map(resultRow)),
          skipped,
        ),
      });
    }

    function jobCard() {
      const job = state.job;
      if (!job) return null;
      const lines = Array.isArray(job.log) ? job.log : [];
      return card({
        title: t('packages.job.title', {action: job.action, package: job.package}),
        iconName: 'terminal',
        tools: h('span', {class: 'conn', dataset: {state: jobConnState(job.state)}},
          h('span', {class: 'conn__dot'}),
          t(JOB_STATE_LABEL[job.state] || 'packages.job.queued'),
        ),
        body: h('div', {class: 'stack stack--sm'},
          job.message ? h('p', {class: 'muted small'}, job.message) : null,
          h('pre', {class: 'log log--compact'}, lines.join('\n') || ' '),
        ),
      });
    }

    function recentCard() {
      if (!state.recent.length) return null;
      return card({
        title: t('packages.jobs.recent'), iconName: 'logs',
        body: h('div', {class: 'list'},
          state.recent.slice(0, 6).map((job) => h('div', {class: 'list__row'},
            h('div', {class: 'list__main'},
              h('div', {class: 'list__title'},
                t('packages.job.title', {action: job.action, package: job.package})),
              h('div', {class: 'list__sub'}, job.message || job.created_at || ''),
            ),
            h('span', {class: 'conn', dataset: {state: jobConnState(job.state)}},
              h('span', {class: 'conn__dot'}),
              t(JOB_STATE_LABEL[job.state] || 'packages.job.queued'),
            ),
          )),
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
          h('div', {class: 'notice__body'},
            h('span', null, state.error),
            h('div', {class: 'notice__actions'},
              h('button', {class: 'btn btn--sm', type: 'button', onClick: () => loadOverview()},
                t('packages.action.retry')),
            ),
          ),
        ));
        return;
      }
      mount(slot, [searchCard(), jobCard(), resultsCard(), backendsCard(), recentCard()]);
    }

    render();
    await loadOverview();

    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  },
};
