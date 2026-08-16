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
  'updates.rollback': 'Go back to {version}',
  'updates.rollback.confirm': 'Restore {version}? The files go back and project-os restarts. Your data is untouched.',
  'updates.rollback.done': 'Back on {version}. Restarting…',
  'updates.previous.title': 'Previous version kept: {version}',
  'updates.previous.body': 'The update saved the old tree next to the install, so this box can go back to {version} without the network.',
  'updates.disabled': 'Updates are turned off in settings.',
  'updates.log': 'Log',
  // On an image install the code lives in a directory this service cannot swap
  // (see updates.can_apply). Saying so beats an [Errno 13] after a download.
  'updates.blocked': 'This box cannot swap the app on its own.',
}, {activate: false});

const POLL_MS = 1200;
const RESTART_GRACE_MS = 120000;
// Tempo do aviso "de volta na X" ser lido antes de a página se recarregar.
const RELOAD_DELAY_MS = 1500;

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
  get title() { return t('nav.updates'); },

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
        // Os dois sistemas do cartão. Falhar aqui não pode esconder a tela
        // inteira: num cartão antigo esta rota responde "não dá", e isso é uma
        // informação, não um erro.
        try {
          state.system = await api.get('/updates/system');
        } catch (err) {
          state.system = null;
        }
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
            // A atualização troca a árvore inteira, e a tela faz parte dela: o
            // navegador continua rodando o JavaScript da versão anterior contra
            // a API da nova. Dava para ver na barra lateral, que seguia
            // anunciando a versão de antes do clique. Recarregar é o que faz as
            // duas metades voltarem a ser a mesma versão -- depois do aviso,
            // para o recado não sumir junto com a página.
            setTimeout(() => window.location.reload(), RELOAD_DELAY_MS);
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
      // can_install === false means the swap would fail before it started: the
      // code tree's parent belongs to root. The version still gets announced,
      // and the system update below is the way in on that kind of install.
      const bloqueado = check.can_install === false;
      return card({
        title: t('updates.available', {version: check.latest}),
        iconName: 'download',
        body: h('div', {class: 'stack stack--sm'},
          check.notes
            ? h('div', {class: 'stack stack--sm'},
                h('div', {class: 'field-row__label'}, t('updates.notes')),
                h('p', null, check.notes))
            : null,
          bloqueado
            ? h('div', {class: 'stack stack--sm'},
                h('p', null, t('updates.blocked')),
                h('p', {class: 'muted'}, check.install_hint || check.install_blocked || ''))
            : null,
        ),
        footer: bloqueado ? null : h('div', {class: 'row'},
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
        body: h('pre', {class: 'log log--compact'}, (job.log || []).join('\n') || ' '),
      });
    }

    // O botão de voltar ficava dentro do cartão acima, que só existe enquanto
    // há um trabalho na memória do processo -- e a atualização reinicia o
    // processo. Ou seja: ele desaparecia no instante em que passaria a ser
    // útil. Agora ele mora num cartão próprio, alimentado pela pasta que a
    // atualização deixou no disco.
    function previousCard() {
      const anterior = (state.info || {}).previous;
      if (!anterior || !anterior.version) return null;
      return card({
        title: t('updates.previous.title', {version: anterior.version}),
        iconName: 'clock',
        body: h('p', {class: 'muted'}, t('updates.previous.body', {version: anterior.version})),
        footer: h('div', {class: 'row'},
          h('button', {
            class: 'btn btn--danger', type: 'button', disabled: state.waiting,
            onClick: async () => {
              const ok = await confirm(t('updates.rollback.confirm', {version: anterior.version}));
              if (!ok) return;
              try {
                await api.post('/updates/rollback', {});
                toast(t('updates.rollback.done', {version: anterior.version}), {type: 'success'});
                await restartNow();
              } catch (err) {
                toast(err instanceof ApiError ? err.message : String(err), {type: 'error'});
              }
            },
          }, t('updates.rollback', {version: anterior.version})),
        ),
      });
    }

    // Voltar os arquivos não volta o que já está na memória: o serviço segue
    // rodando o código novo até reiniciar. Fazer isso pelo mesmo botão evita a
    // caixa que diz 0.4.8 no disco e 0.4.9 na tela.
    async function restartNow() {
      try {
        await api.post('/updates/restart', {});
      } catch (err) {
        // A resposta morre junto com o processo: é isso que reiniciar parece
        // daqui. Só um erro antes de reiniciar merece recado.
        if (err instanceof ApiError && err.status && err.status !== 0) {
          toast(err.message, {type: 'error'});
          return;
        }
      }
      state.waiting = true;
      render();
      waitForRestart();
    }

    function systemCard() {
      const data = state.system;
      if (!data) return null;
      const slots = data.slots || {};
      const pode = (data.capability || {}).ok;
      const sistema = data.system || {};

      if (!pode) {
        return card({
          title: t('sys.title'), iconName: 'system',
          body: h('div', {class: 'notice notice--info'},
            h('div', {class: 'notice__body'},
              h('span', null, (data.capability || {}).reason || t('sys.unavailable')))),
        });
      }

      const linhas = h('div', {class: 'stack stack--sm'},
        h('div', {class: 'field-row'},
          h('span', {class: 'field-row__label'}, t('sys.running')),
          h('span', {class: 'field-row__value'}, t('sys.slot', {slot: slots.current || '?'}))),
        h('div', {class: 'field-row'},
          h('span', {class: 'field-row__label'}, t('sys.target')),
          h('span', {class: 'field-row__value muted'}, t('sys.slot', {slot: slots.target || '?'}))),
        h('div', {class: 'field-row'},
          h('span', {class: 'field-row__label'}, t('sys.good')),
          h('span', {class: 'field-row__value muted'}, t('sys.slot', {slot: slots.good || '?'}))),
        h('p', {class: 'muted small'}, t('sys.explain')),
      );

      const acoes = [];
      if (sistema.update_available) {
        acoes.push(h('button', {
          class: 'btn btn--primary', type: 'button',
          disabled: state.waiting || (state.job && state.job.state === 'running'),
          onClick: () => installSystem(sistema.latest),
        }, t('sys.install', {version: sistema.latest})));
      }
      acoes.push(h('button', {
        class: 'btn btn--sm', type: 'button',
        onClick: () => rollbackSystem(slots.target),
      }, t('sys.rollback', {slot: slots.target || '?'})));

      return card({
        title: sistema.update_available
          ? t('sys.available', {version: sistema.latest})
          : t('sys.title'),
        sub: t('sys.sub'),
        iconName: 'system',
        body: linhas,
        footer: h('div', {class: 'row'}, ...acoes),
      });
    }

    async function installSystem(version) {
      // Trocar o sistema inteiro reinicia a caixa. Vale perguntar.
      const ok = await confirm(t('sys.confirm', {version}));
      if (!ok) return;
      try {
        const data = await api.post('/updates/system/install', {version});
        state.job = data.job;
        render();
        follow();
      } catch (err) {
        toast(err instanceof ApiError ? err.message : String(err), {type: 'error'});
      }
    }

    async function rollbackSystem(slot) {
      const ok = await confirm(t('sys.rollback.confirm', {slot: slot || '?'}));
      if (!ok) return;
      try {
        await api.post('/updates/system/rollback', {});
        toast(t('sys.rollback.started'), {type: 'success'});
        waitForRestart();
      } catch (err) {
        toast(err instanceof ApiError ? err.message : String(err), {type: 'error'});
      }
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
      mount(slot, [statusCard(), jobCard(), availableCard(), previousCard(), systemCard()]);
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
