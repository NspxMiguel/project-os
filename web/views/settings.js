// views/settings.js — General, Account, Network, Updates/About, Developer.
//
// Route is `#/settings/:section`; `ctx.params.section` picks the tab and
// defaults to the first (`general`) when the hash is bare `#/settings`.
// Switching tabs is a normal link to a new hash rather than local state, so
// the URL always matches what's on screen and a refresh lands on the same
// tab — that costs one extra fetch of /api/settings per switch, which is
// cheap next to the clarity of "the address bar is the truth".
//
// Everything here writes through `PUT /api/settings` with a flat dotted-path
// body (`{values: {"ui.theme": "dark"}}`) — see api/settings.py. A field with
// no home in `project_os/config.py` DEFAULTS (instance name, language) still
// persists, because the backend accepts any dotted path; it is simply not
// read by anything else on the server today. Both are called out in the
// delivery report as guessed, not officially supported, fields.
//
// Mode and theme changes can't call app.js's own setMode()/applyTheme() (not
// exported on ctx), so this view reproduces their side effects by hand:
// the same localStorage keys, the same store.set(), and ctx.reload() (which
// *is* exposed — it's app.js's boot()) to force the shell to repaint nav and
// topbar around the new value.

import {h, mount, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';
import {ApiError} from '../lib/api.js';
import {toast, confirm} from '../lib/toast.js';
import {navigate} from '../lib/router.js';

setStrings('en', {
  'settings.lead': 'How project-os behaves, and who can get in.',
  'settings.section.general': 'General',
  'settings.section.account': 'Account',
  'settings.section.network': 'Network',
  'settings.section.integrations': 'Integrations',
  'settings.section.updates': 'Updates & about',
  'settings.section.developer': 'Developer',
  'settings.error.load': 'Could not load settings.',
  'settings.action.retry': 'Retry',
  'settings.action.save': 'Save',
  'settings.action.saved': 'Saved.',
  'settings.action.saveFailed': 'Could not save that.',
  'settings.restartRequired': 'This takes effect after project-os restarts.',
  // general
  'settings.general.instanceName': 'Instance name',
  'settings.general.instanceName.hint': 'What this box is called. Shows in the sidebar and the browser tab — useful once there is more than one.',
  'settings.general.theme': 'Theme',
  'settings.general.timezone': 'Timezone',
  'settings.general.timezone.hint': 'Decides when scheduled things happen. The image boots on UTC, because it cannot know where it will be plugged in.',
  'settings.general.timezone.detect': 'Use this browser',
  'settings.general.mode': 'Interface',
  'settings.general.mode.hint': 'Simple is one screen per idea. Advanced adds system, services, files, terminal and logs.',
  // account
  'settings.account.changePassword': 'Change password',
  'settings.account.currentPassword': 'Current password',
  'settings.account.newPassword': 'New password',
  'settings.account.confirmPassword': 'Confirm new password',
  'settings.account.password.empty': 'Type the new password.',
  'settings.account.password.mismatch': 'The new password and its confirmation do not match.',
  'settings.account.password.changed': 'Password changed. Every session, including this one, was signed out — sign back in.',
  'settings.account.sessions': 'Signed-in sessions',
  'settings.account.sessions.empty': 'No other session data to show.',
  'settings.account.sessions.revokeAll': 'Log out everywhere',
  'settings.account.sessions.revokeAll.confirm.title': 'Log out everywhere?',
  'settings.account.sessions.revokeAll.confirm.body': 'Every session is signed out, including this one — you land back on the login screen.',
  'settings.account.sessions.revoked': 'All sessions were signed out.',
  'settings.account.logout': 'Log out',
  'settings.account.anonymous': 'Authentication is turned off, so there is no account to manage here — see Developer for the switch.',
  // network
  'settings.network.hostname': 'Hostname',
  'settings.network.host': 'Listen address',
  'settings.network.host.hint': '0.0.0.0 answers on every network interface. Restart required after a change.',
  'settings.network.port': 'Port',
  'settings.network.discovery': 'Network discovery',
  'settings.network.discovery.hint': 'Lets project-os look for smart-home devices and speakers on the local network (mDNS).',
  // integrations
  'settings.ha.title': 'Home Assistant',
  'settings.ha.sub': 'The one you already have, not a second one',
  'settings.ha.lead': 'project-os is not built on Home Assistant and works without it. If there is one on this network, connecting to it is the cheapest way to reach devices project-os would otherwise have to learn one protocol at a time.',
  'settings.ha.url': 'Address',
  'settings.ha.url.hint': 'Something like homeassistant.local:8123 or 192.168.1.20:8123. http:// is filled in if you leave it out.',
  'settings.ha.token': 'Long-lived access token',
  'settings.ha.token.hint': 'In Home Assistant: your profile, Security tab, at the bottom — "Long-lived access tokens", Create token. It is only shown once.',
  'settings.ha.token.saved': 'A token is saved. Type a new one to replace it.',
  'settings.ha.test': 'Test',
  'settings.ha.connect': 'Connect',
  'settings.ha.disconnect': 'Disconnect',
  'settings.ha.disconnect.confirm.title': 'Forget this Home Assistant?',
  'settings.ha.disconnect.confirm.body': 'project-os forgets the address and the token. Nothing changes on the Home Assistant side, and the token keeps working until you delete it there.',
  'settings.ha.connected': 'Connected',
  'settings.ha.notConnected': 'Not connected',
  'settings.ha.entities': 'Entities',
  'settings.ha.entities.load': 'Show what is there',
  'settings.ha.entities.none': 'This Home Assistant reports no entities.',
  'settings.ha.entities.more': 'and {count} more',
  'settings.ha.on': 'On',
  'settings.ha.off': 'Off',
  'settings.ha.needsHttpx': 'This integration needs the httpx package, which is not installed on this box.',
  // updates
  'settings.updates.version': 'Version',
  'settings.updates.about': 'project-os ships empty and grows from the Store — this box has no apps until you add them there.',
  'settings.updates.docs': 'API documentation',
  'settings.updates.check': 'Check for updates',
  'settings.updates.checking': 'Checking…',
  'settings.updates.uptodate': 'This is the newest version.',
  'settings.updates.found': 'Version {version} is available.',
  'settings.updates.install': 'Install {version}',
  'settings.updates.open': 'Updates screen',
  // developer
  'settings.developer.warning': 'These are for people comfortable with what they turn on. Nothing here is required to use project-os.',
  'settings.developer.terminal': 'Dock a terminal in the corner',
  'settings.developer.terminal.hint': 'Pins a small terminal over every screen — for installing things by hand, poking at logs, anything a shell is faster for.',
  'settings.developer.card': 'Developer tools',
  'settings.developer.terminal.needsShell': 'Shell access is off above, so a docked terminal would have nothing to run against.',
  'settings.developer.allowShell': 'Allow shell access',
  'settings.developer.allowShell.hint': 'Lets project-os (and the docked terminal) run commands on this machine.',
  'settings.developer.verboseLogging': 'Verbose logging',
  'settings.developer.verboseLogging.hint': 'Switches the log level to DEBUG. Noisy — turn it off again once you have what you needed.',
  'settings.developer.docsLink': 'Open /api/docs',
  'settings.developer.allowHardware': 'Allow hardware control',
  'settings.developer.allowHardware.hint': 'Unlocks Hardware tuning: fan, clock, LEDs, HDMI, Wi-Fi power save. Off by default because a wrong clock setting is one of the few things here that can leave the box needing a keyboard and a monitor.',
  'settings.developer.allowServices': 'Allow service control',
  'settings.developer.allowServices.hint': 'Unlocks the Start/Stop/Restart buttons on the Services screen, and the two buttons below. Runs systemctl as root.',
  'settings.developer.allowFileWrite': 'Allow writing files',
  'settings.developer.allowFileWrite.hint': 'Lets the Files screen save, upload, rename and delete. Reading works either way.',
  'settings.developer.allowPackages': 'Allow installing packages',
  'settings.developer.allowPackages.hint': 'Lets the Packages screen install and remove system software with apt. On by default — installing things from the browser is the point of Advanced mode.',
  'settings.developer.power': 'Power',
  'settings.developer.power.sub': 'The board itself, not just project-os',
  'settings.developer.power.needsServices': 'Service control is off above, so these two would be refused.',
  'settings.developer.reboot': 'Reboot the board',
  'settings.developer.reboot.confirm.title': 'Reboot now?',
  'settings.developer.reboot.confirm.body': 'The box goes away for about a minute. This page finds it again on its own when it comes back.',
  'settings.developer.reboot.sent': 'Rebooting. This page comes back by itself.',
  'settings.developer.shutdown': 'Shut down',
  'settings.developer.shutdown.confirm.title': 'Shut down?',
  'settings.developer.shutdown.confirm.body': 'The board turns itself off. Only unplugging and plugging the power back in brings it up again — there is no button here for that.',
  'settings.developer.shutdown.sent': 'Shutting down. Wait for the green light to stop blinking before pulling the cable.',
}, {activate: false});

const LS_THEME = 'project_os.theme';
const LS_MODE = 'project_os.mode';
const LS_DOCK_TERMINAL = 'project_os.dockTerminal';

const SECTIONS = ['general', 'account', 'network', 'integrations', 'updates', 'developer'];

function writeLocal(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (err) {
    /* private mode: preference simply does not persist client-side */
  }
}

function readLocal(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (err) {
    return fallback;
  }
}

function card(options) {
  const {title, sub, iconName, body, footer} = options;
  return h('div', {class: 'card'},
    h('div', {class: 'card__header'},
      iconName ? h('span', {class: 'card__icon'}, icon(iconName, {size: 18})) : null,
      h('div', {class: 'grow'},
        h('div', {class: 'card__title'}, title),
        sub ? h('div', {class: 'card__sub'}, sub) : null,
      ),
    ),
    body ? h('div', {class: 'card__body'}, body) : null,
    footer ? h('div', {class: 'card__footer'}, footer) : null,
  );
}

function skeletonCard() {
  return h('div', {class: 'card'},
    h('div', {class: 'card__header'}, h('div', {class: 'skeleton skeleton--title'})),
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
              icon('refresh', {size: 14}), t('settings.action.retry')),
          )
        : null,
    ),
  );
}

function field(labelText, hintText, inputNode) {
  return h('div', {class: 'field'},
    h('label', {class: 'field__label'}, labelText),
    inputNode,
    hintText ? h('div', {class: 'field__hint'}, hintText) : null,
  );
}

function dig(obj, path, fallback) {
  if (!obj) return fallback;
  let node = obj;
  for (const part of String(path).split('.')) {
    if (node === null || node === undefined || typeof node !== 'object') return fallback;
    node = node[part];
  }
  return node === undefined || node === null ? fallback : node;
}

export default {
  id: 'settings',
  get title() { return t('nav.settings'); },

  async mount(root, ctx) {
    let disposed = false;
    const section = SECTIONS.includes(ctx.params && ctx.params.section) ? ctx.params.section : SECTIONS[0];

    const state = {
      status: 'loading', // loading | ready | error
      error: null,
      settings: null,
      restartRequired: [],
      sessions: null,
      sessionsError: null,
      hostname: null,
      saving: new Set(), // field paths currently being written
      passwordForm: {current: '', next: '', confirm: ''},
      changingPassword: false,
      instanceName: '',
      language: 'en',
      dockTerminal: readLocal(LS_DOCK_TERMINAL, '0') === '1',
      home: null,          // resposta de GET /api/home
      homeBusy: false,
      haUrl: '',
      haToken: '',         // só sobe; nunca desce do servidor
      haEntities: null,    // null = ainda não pedi
      updateCheck: null,   // resposta de POST /api/updates/check
      updateChecking: false,
      updateError: '',
    };

    async function load() {
      state.status = 'loading';
      render();
      try {
        const data = await api.get('/settings');
        if (disposed) return;
        state.settings = data.settings || {};
        state.restartRequired = data.restart_required || [];
        state.instanceName = dig(state.settings, 'ui.instance_name', '') || '';
        state.language = dig(state.settings, 'ui.language', 'en') || 'en';
        state.status = 'ready';
      } catch (err) {
        if (disposed) return;
        state.status = 'error';
        state.error = err instanceof ApiError ? err.message : t('settings.error.load');
        render();
        return;
      }
      // Best-effort extras: neither failure should block the settings page itself.
      if (section === 'account') loadSessions();
      if (section === 'network') loadHostname();
      if (section === 'integrations') {
        state.haUrl = dig(state.settings, 'integrations.home_assistant.url', '') || '';
        loadHome();
      }
      render();
    }

    async function loadSessions() {
      try {
        const data = await api.get('/auth/sessions');
        if (disposed) return;
        state.sessions = data.sessions || [];
      } catch (err) {
        if (disposed) return;
        state.sessionsError = err instanceof ApiError ? err.message : t('settings.error.load');
      }
      render();
    }

    async function loadHostname() {
      try {
        const raw = await api.get('/system/stats');
        if (disposed) return;
        const stats = ctx.readStats ? ctx.readStats(raw) : {net: raw && raw.net};
        state.hostname = dig(stats, 'net.hostname', null);
      } catch (err) {
        /* the network card still works without a hostname line */
      }
      render();
    }

    async function saveValues(values, {onSuccess} = {}) {
      const paths = Object.keys(values);
      paths.forEach((p) => state.saving.add(p));
      render();
      try {
        const data = await api.put('/settings', {values});
        if (disposed) return;
        state.settings = data.settings || state.settings;
        // The shell reads the box's name straight from here, so the sidebar and
        // the tab title change the moment the save lands.
        if (data.settings && ctx.store) ctx.store.set({config: data.settings});
        if (data.restart_required && data.restart_required.length) {
          state.restartRequired = Array.from(new Set(state.restartRequired.concat(data.restart_required)));
          toast(t('settings.restartRequired'), {type: 'info'});
        } else {
          toast(t('settings.action.saved'));
        }
        if (onSuccess) onSuccess(data);
      } catch (err) {
        if (disposed) return;
        toast((err instanceof ApiError && err.message) || t('settings.action.saveFailed'), {type: 'error'});
      } finally {
        if (!disposed) {
          paths.forEach((p) => state.saving.delete(p));
          render();
        }
      }
    }

    function applyThemeLocally(pref) {
      const el = document.documentElement;
      const resolved = pref === 'light' || pref === 'dark'
        ? pref
        : (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
      el.setAttribute('data-theme', resolved);
      el.setAttribute('data-theme-pref', pref);
      ctx.store.set({theme: pref});
      writeLocal(LS_THEME, pref);
    }

    function setTheme(pref) {
      applyThemeLocally(pref);
      saveValues({'ui.theme': pref});
    }

    function setMode(mode) {
      ctx.store.set({mode});
      writeLocal(LS_MODE, mode);
      saveValues({'ui.default_mode': mode}, {
        // A mode switch changes which nav items and routes exist, and app.js's
        // own setMode() (unreachable from here) is what normally re-renders
        // the shell for that — ctx.reload maps to the same boot() it calls,
        // so this is the closest equivalent without editing app.js.
        onSuccess: () => { if (ctx.reload) ctx.reload(); },
      });
    }

    async function changePassword() {
      const {current, next, confirm: confirmValue} = state.passwordForm;
      if (!next) {
        toast(t('settings.account.password.empty'), {type: 'error'});
        return;
      }
      if (next !== confirmValue) {
        toast(t('settings.account.password.mismatch'), {type: 'error'});
        return;
      }
      state.changingPassword = true;
      render();
      try {
        await api.post('/auth/password', {current_password: current, new_password: next});
        if (disposed) return;
        toast(t('settings.account.password.changed'));
        navigate('#/login', {replace: true});
      } catch (err) {
        if (disposed) return;
        toast((err instanceof ApiError && err.message) || t('settings.action.saveFailed'), {type: 'error'});
      } finally {
        if (!disposed) {
          state.changingPassword = false;
          render();
        }
      }
    }

    async function revokeAllSessions() {
      const ok = await confirm(t('settings.account.sessions.revokeAll.confirm.body'), {
        title: t('settings.account.sessions.revokeAll.confirm.title'),
        danger: true,
      });
      if (!ok || disposed) return;
      try {
        await api.del('/auth/sessions');
        if (disposed) return;
        toast(t('settings.account.sessions.revoked'));
        navigate('#/login', {replace: true});
      } catch (err) {
        if (disposed) return;
        toast((err instanceof ApiError && err.message) || t('settings.action.saveFailed'), {type: 'error'});
      }
    }

    async function logout() {
      try {
        await api.post('/auth/logout', {}, {redirectOnAuth: false});
      } catch (err) {
        /* logging out never fails from the user's point of view */
      }
      if (disposed) return;
      ctx.store.set({user: null, authenticated: false});
      navigate('#/login', {replace: true});
      if (ctx.reload) ctx.reload();
    }

    function setDockTerminal(on) {
      state.dockTerminal = on;
      writeLocal(LS_DOCK_TERMINAL, on ? '1' : '0');
      // app.js does not know about the docked terminal — this event is the
      // documented handoff. See the delivery report for the exact contract.
      window.dispatchEvent(new CustomEvent('project_os:dock-terminal', {detail: {on}}));
      saveValues({'ui.dock_terminal': on});
    }

    // As seis abas somam mais que a largura de um celular. Sem rolagem própria,
    // a tira empurrava a *página*: a grade das Configurações passava a ter a
    // largura da tira, os cartões saíam pela direita da tela e a página inteira
    // rolava de lado. Rolar dentro da tira mantém isso onde é do tamanho dela.
    function tabs() {
      return h('div', {class: 'segmented segmented--scroll'},
        SECTIONS.map((key) => h('a', {
          class: 'segmented__btn' + (key === section ? ' is-active' : ''),
          href: '#/settings/' + key,
          'aria-current': key === section ? 'page' : null,
        }, t('settings.section.' + key))),
      );
    }

    function switchRow(labelText, hintText, checked, onChange, disabled) {
      return h('div', {class: 'field-row field-row--switch'},
        h('div', {class: 'field-row__label'},
          h('div', null, labelText),
          hintText ? h('div', {class: 'field__hint'}, hintText) : null,
        ),
        h('div', {class: 'field-row__value'},
          h('label', {class: 'switch'},
            h('input', {
              type: 'checkbox', checked, disabled,
              onChange: (e) => onChange(e.target.checked),
            }),
            h('span', {class: 'switch__track'}),
          ),
        ),
      );
    }

    function generalSection() {
      const theme = dig(state.settings, 'ui.theme', 'auto') || 'auto';
      const mode = dig(state.settings, 'ui.default_mode', 'simple') || 'simple';
      return h('div', {class: 'stack stack--lg'},
        card({
          title: t('settings.section.general'), iconName: 'settings',
          body: h('div', {class: 'fields'},
            field(t('settings.general.instanceName'), t('settings.general.instanceName.hint'),
              h('input', {
                class: 'input', type: 'text', value: state.instanceName,
                onChange: (e) => { state.instanceName = e.target.value; saveValues({'ui.instance_name': state.instanceName}); },
              })),
            // No language row: there is exactly one language to choose, so the
            // dropdown was a control that could not change anything. It comes
            // back the day a second translation exists.
            field(t('settings.general.timezone'), t('settings.general.timezone.hint'),
              h('div', {class: 'input-group'},
                h('input', {
                  class: 'input', type: 'text', placeholder: 'America/Sao_Paulo',
                  value: dig(state.settings, 'system.timezone', '') || '',
                  onChange: (e) => saveValues({'system.timezone': e.target.value.trim()}),
                }),
                // The browser already knows, and typing "America/Sao_Paulo" by
                // hand on a phone is a small cruelty.
                h('button', {
                  class: 'btn btn--outline', type: 'button',
                  onClick: () => {
                    const guess = (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone || '';
                    if (guess) saveValues({'system.timezone': guess});
                  },
                }, t('settings.general.timezone.detect')),
              )),
            field(t('settings.general.theme'), null,
              h('div', {class: 'segmented'},
                ['auto', 'dark', 'light'].map((key) => h('button', {
                  class: 'segmented__btn' + (theme === key ? ' is-active' : ''),
                  type: 'button', onClick: () => setTheme(key),
                }, icon(key === 'dark' ? 'moon' : key === 'light' ? 'sun' : 'sync', {size: 14}), t('theme.' + key))))),
            field(t('settings.general.mode'), t('settings.general.mode.hint'),
              h('div', {class: 'segmented'},
                ['simple', 'advanced'].map((key) => h('button', {
                  class: 'segmented__btn' + (mode === key ? ' is-active' : ''),
                  type: 'button', onClick: () => setMode(key),
                }, t('mode.' + key))))),
          ),
        }),
      );
    }

    function accountSection() {
      const user = ctx.user || ctx.store.get('user');
      if (user && user.anonymous) {
        return h('div', {class: 'notice notice--info'},
          icon('info', {size: 18}),
          h('div', {class: 'notice__body'}, h('span', null, t('settings.account.anonymous'))));
      }
      const pw = state.passwordForm;
      const sessionRows = state.sessions === null
        ? h('div', {class: 'skeleton-stack'}, [0, 1].map(() => h('div', {class: 'skeleton skeleton--text'})))
        : state.sessionsError
          ? errorState(state.sessionsError, loadSessions)
          : state.sessions.length === 0
            ? h('p', {class: 'muted small'}, t('settings.account.sessions.empty'))
            : h('div', {class: 'list'}, state.sessions.map((s) => h('div', {class: 'list__row'},
                h('div', {class: 'list__main'},
                  h('div', {class: 'list__title mono'}, s.token),
                  h('div', {class: 'list__sub'}, s.user_agent || '—')),
                h('div', {class: 'list__aside faint small'}, fmt.relativeTime(s.created_at)))));

      return h('div', {class: 'stack stack--lg'},
        card({
          title: t('settings.account.changePassword'), iconName: 'key',
          body: h('form', {
            class: 'form',
            onSubmit: (e) => { e.preventDefault(); changePassword(); },
          },
            field(t('settings.account.currentPassword'), null,
              h('input', {class: 'input', type: 'password', value: pw.current, autocomplete: 'current-password',
                onInput: (e) => { pw.current = e.target.value; }})),
            field(t('settings.account.newPassword'), null,
              h('input', {class: 'input', type: 'password', value: pw.next, autocomplete: 'new-password',
                onInput: (e) => { pw.next = e.target.value; }})),
            field(t('settings.account.confirmPassword'), null,
              h('input', {class: 'input', type: 'password', value: pw.confirm, autocomplete: 'new-password',
                onInput: (e) => { pw.confirm = e.target.value; }})),
            h('div', {class: 'row row--end'},
              h('button', {class: 'btn btn--primary', type: 'submit', disabled: state.changingPassword},
                state.changingPassword ? h('span', {class: 'spinner spinner--sm'}) : null,
                t('settings.account.changePassword'))),
          ),
        }),
        card({
          title: t('settings.account.sessions'), iconName: 'shield',
          body: sessionRows,
          footer: h('div', {class: 'row row--between'},
            h('button', {class: 'btn btn--outline btn--danger btn--sm', type: 'button', onClick: revokeAllSessions},
              icon('unlock', {size: 14}), t('settings.account.sessions.revokeAll')),
            h('button', {class: 'btn btn--ghost btn--sm', type: 'button', onClick: logout},
              icon('power', {size: 14}), t('settings.account.logout'))),
        }),
      );
    }

    function networkSection() {
      const host = dig(state.settings, 'server.host', '');
      const port = dig(state.settings, 'server.port', '');
      const discovery = dig(state.settings, 'discovery.enabled', true);
      return h('div', {class: 'stack stack--lg'},
        card({
          title: t('settings.section.network'), iconName: 'wifi',
          body: h('div', {class: 'fields'},
            state.hostname ? h('div', {class: 'field-row'},
              h('div', {class: 'field-row__label'}, t('settings.network.hostname')),
              h('div', {class: 'field-row__value mono'}, state.hostname)) : null,
            field(t('settings.network.host'), t('settings.network.host.hint'),
              h('input', {
                class: 'input', type: 'text', value: host,
                onChange: (e) => saveValues({'server.host': e.target.value}),
              })),
            field(t('settings.network.port'), null,
              h('input', {
                class: 'input', type: 'number', value: port, min: 1, max: 65535,
                onChange: (e) => saveValues({'server.port': Number(e.target.value) || port}),
              })),
            switchRow(t('settings.network.discovery'), t('settings.network.discovery.hint'), !!discovery,
              (checked) => saveValues({'discovery.enabled': checked})),
          ),
        }),
      );
    }

    async function procurarAtualizacao() {
      state.updateChecking = true;
      state.updateError = '';
      render();
      try {
        state.updateCheck = await api.post('/updates/check', {});
      } catch (err) {
        state.updateCheck = null;
        state.updateError = err instanceof ApiError ? err.message : String(err);
      } finally {
        state.updateChecking = false;
        if (!disposed) render();
      }
    }

    // A aba que se chama "Atualizações e sobre" mostrava a versão e mais nada: o
    // procurar/instalar mora numa tela que só existe no menu do modo Avançado.
    // No modo padrão, então, a caixa não tinha por onde ser atualizada -- e ficar
    // parado numa versão antiga não é uma escolha que alguém fez.
    //
    // Procurar acontece aqui, que é onde a pessoa foi olhar. Instalar leva para a
    // tela de Atualizações, que é quem sabe seguir o registro do trabalho,
    // esperar o reinício e voltar versão -- duplicar isso aqui seria manter duas
    // cópias de uma coisa que reinicia o serviço no meio.
    function updatesSection() {
      const version = (ctx.health && ctx.health.version) || dig(ctx.store.get('health'), 'version', '') || '—';
      const achado = state.updateCheck;
      const temNova = !!(achado && achado.update_available);
      return h('div', {class: 'stack stack--lg'},
        card({
          title: t('settings.section.updates'), iconName: 'download',
          body: h('div', {class: 'stack stack--sm'},
            h('div', {class: 'kv'},
              h('div', {class: 'kv__key'}, t('settings.updates.version')),
              h('div', {class: 'kv__value mono'}, version)),
            state.updateError
              ? h('p', {class: 'field-row__error'}, state.updateError)
              : null,
            achado && !temNova
              ? h('p', null, t('settings.updates.uptodate'))
              : null,
            // O que muda, aqui mesmo, antes do botão. Quem decide se atualiza
            // agora não devia ter que abrir outra tela para saber o que vem --
            // e o campo mostrava a URL do release, que não responde nada.
            temNova
              ? h('div', {class: 'stack stack--sm'},
                  h('p', null, t('settings.updates.found', {version: achado.latest})),
                  achado.notes
                    ? h('details', {class: 'changelog__box', open: true},
                        h('summary', null, t('updates.notes')),
                        h('pre', {class: 'changelog'}, achado.notes.trim()))
                    : null,
                  achado.notes_url
                    ? h('a', {class: 'small', href: achado.notes_url,
                              target: '_blank', rel: 'noopener noreferrer'},
                        t('updates.notes.full'))
                    : null)
              : null,
            h('p', {class: 'muted small'}, t('settings.updates.about')),
          ),
          footer: h('div', {class: 'row'},
            h('button', {
              class: 'btn btn--outline btn--sm', type: 'button',
              disabled: state.updateChecking,
              onClick: () => procurarAtualizacao(),
            }, icon('refresh', {size: 14}),
               t(state.updateChecking ? 'settings.updates.checking' : 'settings.updates.check')),
            h('a', {
              class: temNova ? 'btn btn--primary btn--sm' : 'btn btn--outline btn--sm',
              href: '#/updates',
            }, icon('download', {size: 14}),
               temNova
                 ? t('settings.updates.install', {version: achado.latest})
                 : t('settings.updates.open')),
            h('a', {class: 'btn btn--outline btn--sm', href: '/api/docs', target: '_blank', rel: 'noopener'},
              icon('link', {size: 14}), t('settings.developer.docsLink')),
          ),
        }),
      );
    }

    async function power(action) {
      const ok = await confirm(t('settings.developer.' + action + '.confirm.body'), {
        title: t('settings.developer.' + action + '.confirm.title'),
        danger: true,
      });
      if (!ok || disposed) return;
      try {
        await api.post('/system/power', {action, confirm: true});
        if (disposed) return;
        toast(t('settings.developer.' + action + '.sent'), {type: 'info'});
      } catch (err) {
        if (disposed) return;
        toast((err instanceof ApiError && err.message) || t('settings.action.saveFailed'), {type: 'error'});
      }
    }

    /* ------------------------------------------------------ integrações */
    // O cliente REST do Home Assistant (project_os/core/ha.py) existia inteiro,
    // com summary() comentado como "safe-to-serialise state for the settings
    // screen" -- e a tela nunca tinha sido feita. O painel oferecia "já existe
    // um Home Assistant em 192.168.x.x" e mandava para cá, onde não havia campo
    // nenhum. Aqui está o campo.

    async function loadHome() {
      try {
        state.home = await api.get('/home', {query: {probe: true}});
      } catch (err) {
        state.home = {error: (err instanceof ApiError && err.message) || t('state.error')};
      }
      if (!disposed) render();
    }

    async function testHome() {
      state.homeBusy = true; render();
      try {
        const body = {url: state.haUrl || undefined};
        if (state.haToken) body.token = state.haToken;
        const answer = await api.post('/home/test', body);
        toast(answer.message, {type: answer.ok ? 'success' : 'error'});
      } catch (err) {
        toast((err instanceof ApiError && err.message) || t('state.error'), {type: 'error'});
      } finally {
        state.homeBusy = false;
        if (!disposed) render();
      }
    }

    async function connectHome() {
      state.homeBusy = true; render();
      try {
        state.home = await api.post('/home/connect', {url: state.haUrl, token: state.haToken});
        state.haToken = '';
        toast(state.home.message || t('settings.ha.connected'), {type: 'success'});
      } catch (err) {
        toast((err instanceof ApiError && err.message) || t('state.error'), {type: 'error'});
      } finally {
        state.homeBusy = false;
        if (!disposed) render();
      }
    }

    async function disconnectHome() {
      const ok = await confirm(t('settings.ha.disconnect.confirm.body'), {
        title: t('settings.ha.disconnect.confirm.title'), danger: true,
      });
      if (!ok || disposed) return;
      try {
        state.home = await api.del('/home');
        state.haUrl = '';
        state.haEntities = null;
      } catch (err) {
        toast((err instanceof ApiError && err.message) || t('state.error'), {type: 'error'});
      }
      if (!disposed) render();
    }

    async function loadEntities() {
      state.homeBusy = true; render();
      try {
        const answer = await api.get('/home/entities');
        state.haEntities = Array.isArray(answer.items) ? answer.items : [];
      } catch (err) {
        toast((err instanceof ApiError && err.message) || t('state.error'), {type: 'error'});
      } finally {
        state.homeBusy = false;
        if (!disposed) render();
      }
    }

    async function callEntity(entity, service) {
      try {
        await api.post('/home/entities/' + encodeURIComponent(entity.entity_id) + '/call', {service});
        await loadEntities();
      } catch (err) {
        toast((err instanceof ApiError && err.message) || t('state.error'), {type: 'error'});
      }
    }

    function entityRow(entity) {
      const ligado = String(entity.state || '').toLowerCase() === 'on';
      // Só o que dá para ligar e desligar ganha botão. Um sensor de temperatura
      // com um botão de "ligar" seria mentira de novo.
      const controlavel = ['light', 'switch', 'fan', 'input_boolean', 'siren'].indexOf(entity.domain) >= 0;
      return h('div', {class: 'list__row'},
        h('div', {class: 'list__main'},
          h('span', {class: 'list__title'}, entity.name),
          h('span', {class: 'list__sub mono tiny'}, entity.entity_id),
        ),
        h('span', {class: 'list__aside'},
          h('span', {class: 'badge ' + (ligado ? 'badge--ok' : 'badge--plain')}, String(entity.state)),
          controlavel
            ? h('button', {
                class: 'btn btn--sm btn--outline', type: 'button',
                onClick: () => callEntity(entity, ligado ? 'turn_off' : 'turn_on'),
              }, ligado ? t('settings.ha.off') : t('settings.ha.on'))
            : null,
        ),
      );
    }

    function integrationsSection() {
      const home = state.home || {};
      if (home.error) return errorState(home.error, loadHome);
      const conectado = home.connected === true;
      const entidades = state.haEntities;
      return h('div', {class: 'stack stack--lg'},
        card({
          title: t('settings.ha.title'), sub: t('settings.ha.sub'), iconName: 'home',
          body: h('div', {class: 'stack stack--md'},
            h('p', {class: 'muted small'}, t('settings.ha.lead')),
            home.httpx === false
              ? h('div', {class: 'notice notice--warn'},
                  icon('warning', {size: 18}),
                  h('div', {class: 'notice__body'},
                    h('span', null, t('settings.ha.needsHttpx')),
                    home.hint ? h('code', {class: 'mono tiny'}, home.hint) : null))
              : null,
            h('div', {class: 'row'},
              h('span', {class: 'badge ' + (conectado ? 'badge--ok' : 'badge--plain')},
                conectado ? t('settings.ha.connected') : t('settings.ha.notConnected')),
              home.message ? h('span', {class: 'small muted'}, home.message) : null),
            h('div', {class: 'fields'},
              field(t('settings.ha.url'), t('settings.ha.url.hint'),
                h('input', {
                  class: 'input', type: 'text', placeholder: 'homeassistant.local:8123',
                  value: state.haUrl,
                  onInput: (e) => { state.haUrl = e.target.value.trim(); },
                })),
              field(t('settings.ha.token'),
                home.has_token ? t('settings.ha.token.saved') : t('settings.ha.token.hint'),
                h('input', {
                  class: 'input', type: 'password', autocomplete: 'off',
                  placeholder: home.has_token ? '••••••••' : '',
                  value: state.haToken,
                  onInput: (e) => { state.haToken = e.target.value.trim(); },
                })),
            ),
            h('div', {class: 'row'},
              h('button', {
                class: 'btn btn--outline', type: 'button', disabled: state.homeBusy || !state.haUrl,
                onClick: testHome,
              }, t('settings.ha.test')),
              h('button', {
                class: 'btn', type: 'button',
                disabled: state.homeBusy || !state.haUrl || !state.haToken,
                onClick: connectHome,
              }, t('settings.ha.connect')),
              home.configured
                ? h('button', {class: 'btn btn--danger', type: 'button', onClick: disconnectHome},
                    t('settings.ha.disconnect'))
                : null,
            ),
          ),
        }),
        home.configured
          ? card({
              title: t('settings.ha.entities'), iconName: 'devices',
              body: h('div', {class: 'stack stack--sm'},
                entidades === null
                  ? h('button', {class: 'btn btn--outline btn--sm', type: 'button',
                      disabled: state.homeBusy, onClick: loadEntities},
                      t('settings.ha.entities.load'))
                  : (entidades.length
                      ? h('div', {class: 'stack stack--sm'},
                          h('div', {class: 'list'}, entidades.slice(0, 40).map(entityRow)),
                          entidades.length > 40
                            ? h('p', {class: 'muted small'},
                                t('settings.ha.entities.more', {count: entidades.length - 40}))
                            : null)
                      : h('p', {class: 'muted small'}, t('settings.ha.entities.none'))),
              ),
            })
          : null,
      );
    }

    function developerSection() {
      const allowShell = !!dig(state.settings, 'security.allow_shell', false);
      const verbose = dig(state.settings, 'logging.level', 'INFO') === 'DEBUG';
      // The three switches below used to exist only in config.py. Tuning,
      // Services and Files each printed "turn it on in Settings > Developer"
      // and this tab did not have them -- so Hardware tuning was read-only
      // forever and Services never showed a button. The text pointed at a
      // place that did not exist; now it does.
      const allowHardware = !!dig(state.settings, 'security.allow_hardware_control', false);
      const allowServices = !!dig(state.settings, 'security.allow_service_control', false);
      const allowFileWrite = !!dig(state.settings, 'security.allow_file_write', true);
      const allowPackages = !!dig(state.settings, 'security.allow_package_management', true);
      return h('div', {class: 'stack stack--lg'},
        h('div', {class: 'notice notice--warn'},
          icon('warning', {size: 18}),
          h('div', {class: 'notice__body'}, h('span', null, t('settings.developer.warning')))),
        // One card, three switches. A card per switch put the same sentence in
        // the heading and in the switch label, which read like a rendering bug.
        card({
          title: t('settings.developer.card'), iconName: 'terminal',
          body: h('div', {class: 'stack stack--lg'},
            switchRow(t('settings.developer.allowShell'), t('settings.developer.allowShell.hint'), allowShell,
              (checked) => saveValues({'security.allow_shell': checked})),
            h('div', {class: 'stack stack--sm'},
              switchRow(t('settings.developer.terminal'), t('settings.developer.terminal.hint'),
                state.dockTerminal, (checked) => setDockTerminal(checked), !allowShell),
              !allowShell ? h('p', {class: 'muted small'}, t('settings.developer.terminal.needsShell')) : null,
            ),
            switchRow(t('settings.developer.allowHardware'), t('settings.developer.allowHardware.hint'), allowHardware,
              (checked) => saveValues({'security.allow_hardware_control': checked})),
            switchRow(t('settings.developer.allowServices'), t('settings.developer.allowServices.hint'), allowServices,
              (checked) => saveValues({'security.allow_service_control': checked})),
            switchRow(t('settings.developer.allowFileWrite'), t('settings.developer.allowFileWrite.hint'), allowFileWrite,
              (checked) => saveValues({'security.allow_file_write': checked})),
            switchRow(t('settings.developer.allowPackages'), t('settings.developer.allowPackages.hint'), allowPackages,
              (checked) => saveValues({'security.allow_package_management': checked})),
            switchRow(t('settings.developer.verboseLogging'), t('settings.developer.verboseLogging.hint'), verbose,
              (checked) => saveValues({'logging.level': checked ? 'DEBUG' : 'INFO'})),
          ),
        }),
        // POST /api/system/power has existed since the first Advanced build and
        // nothing in the front end called it, while two screens told you to
        // reboot. These are that call.
        card({
          title: t('settings.developer.power'), sub: t('settings.developer.power.sub'), iconName: 'power',
          body: h('div', {class: 'stack stack--sm'},
            !allowServices
              ? h('p', {class: 'muted small'}, t('settings.developer.power.needsServices'))
              : null,
            h('div', {class: 'row'},
              h('button', {
                class: 'btn btn--outline', disabled: !allowServices,
                onClick: () => power('reboot'),
              }, icon('refresh', {size: 14}), t('settings.developer.reboot')),
              h('button', {
                class: 'btn btn--danger', disabled: !allowServices,
                onClick: () => power('shutdown'),
              }, icon('power', {size: 14}), t('settings.developer.shutdown')),
            ),
          ),
        }),
      );
    }

    function render() {
      const nodes = [
        h('div', {class: 'page__header'},
          h('h1', null, t('nav.settings') || 'Settings'),
          h('p', {class: 'page__lead'}, t('settings.lead')),
        ),
        tabs(),
      ];

      if (state.status === 'loading') {
        nodes.push(skeletonCard());
      } else if (state.status === 'error') {
        nodes.push(errorState(state.error, load));
      } else if (section === 'general') {
        nodes.push(generalSection());
      } else if (section === 'account') {
        nodes.push(accountSection());
      } else if (section === 'network') {
        nodes.push(networkSection());
      } else if (section === 'integrations') {
        nodes.push(integrationsSection());
      } else if (section === 'updates') {
        nodes.push(updatesSection());
      } else if (section === 'developer') {
        nodes.push(developerSection());
      }

      clear(root);
      mount(root, h('div', {class: 'stack stack--lg'}, nodes));
    }

    await load();

    return () => {
      disposed = true;
      clear(root);
    };
  },
};
