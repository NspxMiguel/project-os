// views/setup.js — first-run wizard: welcome -> create the admin account ->
// what to do next. This is the only screen reachable while the server answers
// 428 setup_required.

import {h, mount, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import * as api from '../lib/api.js';
import {navigate} from '../lib/router.js';
import {t, setStrings} from '../lib/format.js';

setStrings('en', {
  'setup.title': 'Set up project-os',
  'setup.welcome.title': 'Welcome to project-os',
  'setup.welcome.lead': 'This machine is not set up yet. It takes one minute.',
  'setup.welcome.point1': 'Create an administrator account for this box.',
  'setup.welcome.point2': 'project-os then looks for speakers, TVs and hubs on your network.',
  'setup.welcome.point3': 'Apps you install show up in the sidebar and on the dashboard.',
  'setup.welcome.start': 'Get started',
  'setup.create.title': 'Create your admin account',
  'setup.create.lead': 'This is the only account that exists until you add more.',
  'setup.username': 'Username',
  'setup.password': 'Password',
  'setup.confirm': 'Confirm password',
  'setup.ssh': 'Use this password for SSH too (the terminal login for this machine).',
  'setup.submit': 'Create account',
  'setup.submitting': 'Creating…',
  'setup.needUser': 'Choose a username.',
  'setup.needPassword': 'Type a password.',
  'setup.mismatch': 'The two passwords do not match.',
  'setup.failed': 'Could not create the account.',
  'setup.keepSafe': 'Store this password somewhere safe — there is no email recovery on a Raspberry Pi.',
  'setup.strength.any': 'Any password you like.',
  'setup.strength.weak': 'Weak',
  'setup.strength.fair': 'Fair',
  'setup.strength.good': 'Good',
  'setup.strength.strong': 'Strong',
  'setup.strength.hint1': 'Short, but it is yours.',
  'setup.strength.hint2': 'Longer is better than complicated.',
  'setup.strength.hint3': 'Good. A few more characters would not hurt.',
  'setup.strength.hint4': 'Strong.',
  'setup.done.title': 'You are all set',
  'setup.done.lead': 'Signed in as {name}.',
  'setup.done.devices.title': 'Find your devices',
  'setup.done.devices.text': 'project-os scans the network for AirPlay speakers, Chromecasts, Home Assistant and more. That is where BirdTunes gets its output.',
  'setup.done.apps.title': 'Then pick an app',
  'setup.done.apps.text': 'Apps are installed from the store and appear in the sidebar with their own panel.',
  'setup.done.discover': 'Discover devices',
  'setup.done.dashboard': 'Go to the dashboard',
  'setup.ssh.ready': 'SSH is ready with the same password: ssh project-os@project-os.local',
  'setup.ssh.failed': 'The SSH password was not set: {reason}',
}, {activate: false});

// No minimum. "n coloca isso: The password needs at least 8 characters." --
// this is his own box on his own network, and a rule that refuses the password
// someone actually wants is a rule that gets worked around, not obeyed. The
// meter still says what it thinks; it just does not stand in the doorway.
const GOOD_LENGTH = 8;

/** Cheap, honest strength estimate: length first, variety second. */
function strength(password) {
  const value = String(password || '');
  if (!value) return {level: 0, label: '', hint: t('setup.strength.any')};
  let score = 0;
  if (value.length >= GOOD_LENGTH) score += 1;
  if (value.length >= 12) score += 1;
  if (value.length >= 16) score += 1;
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((re) => re.test(value)).length;
  if (classes >= 2) score += 1;
  if (classes >= 3) score += 1;
  const level = Math.max(1, Math.min(4, Math.round(score * 4 / 5)));
  const labels = {1: t('setup.strength.weak'), 2: t('setup.strength.fair'), 3: t('setup.strength.good'), 4: t('setup.strength.strong')};
  const hints = {
    1: t('setup.strength.hint1'),
    2: t('setup.strength.hint2'),
    3: t('setup.strength.hint3'),
    4: t('setup.strength.hint4'),
  };
  return {level, label: labels[level], hint: hints[level]};
}

export default {
  id: 'setup',
  get title() { return t('setup.title'); },

  async mount(root, ctx) {
    let step = 0;
    let busy = false;
    let createdUser = '';
    let sshProblem = '';

    const host = h('div', {class: 'auth__card'});
    mount(root, host);

    function steps() {
      return h('div', {class: 'auth__steps', 'aria-hidden': 'true'},
        h('span', {class: 'auth__step' + (step >= 0 ? ' is-on' : '')}),
        h('span', {class: 'auth__step' + (step >= 1 ? ' is-on' : '')}),
        h('span', {class: 'auth__step' + (step >= 2 ? ' is-on' : '')}),
      );
    }

    function brand(title, lead) {
      return [
        h('div', {class: 'auth__brand'},
          h('span', {class: 'auth__mark'}, icon('chip', {size: 22})),
          h('h1', {class: 'auth__title'}, title),
        ),
        lead ? h('p', {class: 'auth__lead'}, lead) : null,
        steps(),
      ];
    }

    /* --------------------------------------------------------- step 0 */

    function renderWelcome() {
      mount(host, [
        brand(t('setup.welcome.title'), t('setup.welcome.lead')),
        h('ul', {class: 'stack stack--sm small muted', style: {listStyle: 'none', padding: '0', margin: '0'}},
          h('li', {class: 'row row--tight'}, icon('lock', {size: 16}), h('span', null, t('setup.welcome.point1'))),
          h('li', {class: 'row row--tight'}, icon('devices', {size: 16}), h('span', null, t('setup.welcome.point2'))),
          h('li', {class: 'row row--tight'}, icon('apps', {size: 16}), h('span', null, t('setup.welcome.point3'))),
        ),
        h('button', {
          class: 'btn btn--primary btn--block btn--lg', type: 'button',
          onClick: () => { step = 1; render(); },
        }, t('setup.welcome.start')),
      ]);
    }

    /* --------------------------------------------------------- step 1 */

    function renderCreate() {
      const problem = h('div', {class: 'notice notice--error hidden', role: 'alert'},
        icon('warning', {size: 18}),
        h('div', {class: 'notice__body'}, h('span', {class: 'problem-text'}, '')),
      );

      const username = h('input', {
        type: 'text', id: 'setup-username', name: 'username', value: createdUser || 'admin',
        autocomplete: 'username', autocapitalize: 'none', autocorrect: 'off',
        spellcheck: 'false', required: true,
      });
      const password = h('input', {
        type: 'password', id: 'setup-password', name: 'new-password',
        autocomplete: 'new-password', required: true,
      });

      const confirmPassword = h('input', {
        type: 'password', id: 'setup-confirm', name: 'confirm-password',
        autocomplete: 'new-password', required: true,
      });

      const bars = [0, 1, 2, 3].map(() => h('span', {class: 'strength__bar'}));
      const meterHint = h('span', {class: 'field__hint'}, t('setup.strength.any'));
      const meter = h('div', {class: 'strength', dataset: {level: '0'}},
        h('div', {class: 'strength__bars'}, bars),
        meterHint,
      );

      // The image ships with the Linux account locked -- a password printed in a
      // public README is not a password. This is where the box gets one, which
      // is also the only place it will ever exist.
      const sshToggle = h('input', {type: 'checkbox', id: 'setup-ssh', checked: true});
      const sshField = h('label', {class: 'row row--tight'},
        sshToggle,
        h('span', {class: 'small muted'},
          t('setup.ssh')),
      );

      const submit = h('button', {class: 'btn btn--primary btn--block btn--lg', type: 'submit'}, t('setup.submit'));

      function updateMeter() {
        const result = strength(password.value);
        meter.dataset.level = String(result.level);
        bars.forEach((bar, index) => bar.classList.toggle('is-on', index < result.level));
        mount(meterHint, result.label ? result.label + ' — ' + result.hint : result.hint);
      }

      function showProblem(message) {
        mount(problem.querySelector('.problem-text'), message);
        problem.classList.remove('hidden');
      }

      async function onSubmit(event) {
        event.preventDefault();
        if (busy) return;
        problem.classList.add('hidden');

        const user = username.value.trim();
        const pass = password.value;

        if (!user) {
          showProblem(t('setup.needUser'));
          username.focus();
          return;
        }
        if (!pass) {
          showProblem(t('setup.needPassword'));
          password.focus();
          return;
        }
        if (pass !== confirmPassword.value) {
          showProblem(t('setup.mismatch'));
          confirmPassword.select();
          confirmPassword.focus();
          return;
        }

        busy = true;
        submit.disabled = true;
        submit.classList.add('is-busy');
        mount(submit, [h('span', {class: 'spinner spinner--sm'}), t('setup.submitting')]);

        try {
          await api.post('/setup', {username: user, password: pass}, {redirectOnAuth: false});
          // The box boots on UTC and has no way to know where it is. It asks the
          // network at boot -- "puxar fuso pela net ne..." -- and that answer
          // wins, because it describes where the machine is, not where whoever
          // opened this page happens to be. The browser is only the fallback for
          // when that lookup found nothing (no internet on first boot, say).
          try {
            const current = await api.get('/settings');
            const known = ((current.settings || {}).system || {}).timezone || '';
            const zone = (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone || '';
            if (!known && zone) await api.put('/settings', {values: {'system.timezone': zone}});
          } catch (err) {
            /* a wrong clock is worth a warning, never a failed setup */
          }
          // Needs the session that setup just created, so it goes after it.
          if (sshToggle.checked) {
            try {
              await api.post('/system/password', {password: pass});
            } catch (err) {
              // A box without the helper (a manual install) simply cannot do
              // this, and that is not a reason to fail the whole setup.
              sshProblem = String((err && err.message) || err);
            }
          }
          // The server may or may not hand back a session; make sure we have one.
          try {
            await api.get('/auth/me', {redirectOnAuth: false});
          } catch (err) {
            await api.post('/auth/login', {username: user, password: pass}, {redirectOnAuth: false});
          }
          createdUser = user;
          busy = false;
          step = 2;
          render();
        } catch (err) {
          busy = false;
          submit.disabled = false;
          submit.classList.remove('is-busy');
          mount(submit, t('setup.submit'));
          if (err && err.code === 'already_configured') {
            // "ele ta mandando eu criar um usuario, sendo q o usuario ja
            // existe" -- so stop asking. The account is there; what he needs is
            // the sign-in screen, not a form that can only refuse him.
            navigate('#/login', {replace: true});
            return;
          }
          showProblem((err && err.message) || t('setup.failed'));
        }
      }

      mount(host, [
        brand(t('setup.create.title'), t('setup.create.lead')),
        h('form', {class: 'form', onSubmit: onSubmit, novalidate: true},
          problem,
          h('div', {class: 'field'},
            h('label', {class: 'field__label', for: 'setup-username'}, t('setup.username')),
            username,
          ),
          h('div', {class: 'field'},
            h('label', {class: 'field__label', for: 'setup-password'}, t('setup.password')),
            password,
            meter,
          ),
          h('div', {class: 'field'},
            h('label', {class: 'field__label', for: 'setup-confirm'}, t('setup.confirm')),
            confirmPassword,
          ),
          sshField,
          submit,
        ),
        h('p', {class: 'auth__foot'}, t('setup.keepSafe')),
      ]);

      password.addEventListener('input', updateMeter);
      updateMeter();
      username.focus();
      username.select();
    }

    /* --------------------------------------------------------- step 2 */
    // Says which of the two happened, because "you can SSH in now" and "you
    // cannot" are very different facts to leave someone with.
    function sshNote() {
      if (!sshProblem) {
        return h('p', {class: 'auth__foot'},
          t('setup.ssh.ready'));
      }
      return h('div', {class: 'notice notice--warning'},
        h('div', {class: 'notice__body'},
          t('setup.ssh.failed', {reason: sshProblem})));
    }


    function go(hash) {
      return async () => {
        // Put the destination in place *before* re-booting the shell, so the
        // shell's own dispatch lands on the right screen.
        try {
          window.history.replaceState(null, '', hash);
        } catch (err) {
          window.location.hash = hash;
        }
        await ctx.reload();
      };
    }

    function renderDone() {
      mount(host, [
        brand(t('setup.done.title'), t('setup.done.lead', {name: createdUser || 'admin'})),
        h('div', {class: 'stack stack--sm'},
          h('div', {class: 'notice notice--info'},
            icon('devices', {size: 18}),
            h('div', {class: 'notice__body'},
              h('span', {class: 'notice__title'}, t('setup.done.devices.title')),
              h('span', {class: 'small muted'},
                t('setup.done.devices.text')),
            ),
          ),
          h('div', {class: 'notice'},
            icon('apps', {size: 18}),
            h('div', {class: 'notice__body'},
              h('span', {class: 'notice__title'}, t('setup.done.apps.title')),
              h('span', {class: 'small muted'},
                t('setup.done.apps.text')),
            ),
          ),
        ),
        h('button', {class: 'btn btn--primary btn--block btn--lg', type: 'button', onClick: go('#/devices')},
          icon('devices', {size: 17}), t('setup.done.discover')),
        h('button', {class: 'btn btn--ghost btn--block', type: 'button', onClick: go('#/')},
          t('setup.done.dashboard')),
        sshNote(),
      ]);
    }

    function render() {
      clear(host);
      if (step === 0) renderWelcome();
      else if (step === 1) renderCreate();
      else renderDone();
    }

    render();

    return () => {
      clear(host);
    };
  },
};
