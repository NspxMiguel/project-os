// views/setup.js — first-run wizard: welcome -> create the admin account ->
// what to do next. This is the only screen reachable while the server answers
// 428 setup_required.

import {h, mount, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import * as api from '../lib/api.js';

const MIN_LENGTH = 8;

/** Cheap, honest strength estimate: length first, variety second. */
function strength(password) {
  const value = String(password || '');
  if (!value) return {level: 0, label: '', hint: 'At least ' + MIN_LENGTH + ' characters.'};
  let score = 0;
  if (value.length >= MIN_LENGTH) score += 1;
  if (value.length >= 12) score += 1;
  if (value.length >= 16) score += 1;
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((re) => re.test(value)).length;
  if (classes >= 2) score += 1;
  if (classes >= 3) score += 1;
  const level = Math.max(1, Math.min(4, Math.round(score * 4 / 5)));
  const labels = {1: 'Weak', 2: 'Fair', 3: 'Good', 4: 'Strong'};
  const hints = {
    1: value.length < MIN_LENGTH ? 'Too short — use at least ' + MIN_LENGTH + ' characters.' : 'Add length or another kind of character.',
    2: 'Longer is better than complicated.',
    3: 'Good. A few more characters would not hurt.',
    4: 'Strong.',
  };
  return {level, label: labels[level], hint: hints[level]};
}

export default {
  id: 'setup',
  title: 'Set up project-os',

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
        brand('Welcome to project-os', 'This machine is not set up yet. It takes one minute.'),
        h('ul', {class: 'stack stack--sm small muted', style: {listStyle: 'none', padding: '0', margin: '0'}},
          h('li', {class: 'row row--tight'}, icon('lock', {size: 16}), h('span', null, 'Create an administrator account for this box.')),
          h('li', {class: 'row row--tight'}, icon('devices', {size: 16}), h('span', null, 'project-os then looks for speakers, TVs and hubs on your network.')),
          h('li', {class: 'row row--tight'}, icon('apps', {size: 16}), h('span', null, 'Apps you install show up in the sidebar and on the dashboard.')),
        ),
        h('button', {
          class: 'btn btn--primary btn--block btn--lg', type: 'button',
          onClick: () => { step = 1; render(); },
        }, 'Get started'),
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
      const meterHint = h('span', {class: 'field__hint'}, 'At least ' + MIN_LENGTH + ' characters.');
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
          'Use this password for SSH too (the terminal login for this machine).'),
      );

      const submit = h('button', {class: 'btn btn--primary btn--block btn--lg', type: 'submit'}, 'Create account');

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
          showProblem('Choose a username.');
          username.focus();
          return;
        }
        if (pass.length < MIN_LENGTH) {
          showProblem('The password needs at least ' + MIN_LENGTH + ' characters.');
          password.focus();
          return;
        }
        if (pass !== confirmPassword.value) {
          showProblem('The two passwords do not match.');
          confirmPassword.select();
          confirmPassword.focus();
          return;
        }

        busy = true;
        submit.disabled = true;
        submit.classList.add('is-busy');
        mount(submit, [h('span', {class: 'spinner spinner--sm'}), 'Creating…']);

        try {
          await api.post('/setup', {username: user, password: pass}, {redirectOnAuth: false});
          // The box boots on UTC and has no way to know where it is. The browser
          // does, and asking a person to type "America/Sao_Paulo" to make the
          // schedule work would be a poor first impression.
          try {
            const zone = (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone || '';
            if (zone) await api.put('/settings', {values: {'system.timezone': zone}});
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
          mount(submit, 'Create account');
          showProblem((err && err.message) || 'Could not create the account.');
        }
      }

      mount(host, [
        brand('Create your admin account', 'This is the only account that exists until you add more.'),
        h('form', {class: 'form', onSubmit: onSubmit, novalidate: true},
          problem,
          h('div', {class: 'field'},
            h('label', {class: 'field__label', for: 'setup-username'}, 'Username'),
            username,
          ),
          h('div', {class: 'field'},
            h('label', {class: 'field__label', for: 'setup-password'}, 'Password'),
            password,
            meter,
          ),
          h('div', {class: 'field'},
            h('label', {class: 'field__label', for: 'setup-confirm'}, 'Confirm password'),
            confirmPassword,
          ),
          sshField,
          submit,
        ),
        h('p', {class: 'auth__foot'}, 'Store this password somewhere safe — there is no email recovery on a Raspberry Pi.'),
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
          'SSH is ready with the same password: ssh project-os@project-os.local');
      }
      return h('div', {class: 'notice notice--warning'},
        h('div', {class: 'notice__body'},
          'The SSH password was not set: ' + sshProblem));
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
        brand('You are all set', 'Signed in as ' + (createdUser || 'admin') + '.'),
        h('div', {class: 'stack stack--sm'},
          h('div', {class: 'notice notice--info'},
            icon('devices', {size: 18}),
            h('div', {class: 'notice__body'},
              h('span', {class: 'notice__title'}, 'Find your devices'),
              h('span', {class: 'small muted'},
                'project-os scans the network for AirPlay speakers, Chromecasts, Home Assistant and more. That is where BirdTunes gets its output.'),
            ),
          ),
          h('div', {class: 'notice'},
            icon('apps', {size: 18}),
            h('div', {class: 'notice__body'},
              h('span', {class: 'notice__title'}, 'Then pick an app'),
              h('span', {class: 'small muted'},
                'Apps are installed from the store and appear in the sidebar with their own panel.'),
            ),
          ),
        ),
        h('button', {class: 'btn btn--primary btn--block btn--lg', type: 'button', onClick: go('#/devices')},
          icon('devices', {size: 17}), 'Discover devices'),
        h('button', {class: 'btn btn--ghost btn--block', type: 'button', onClick: go('#/')},
          'Go to the dashboard'),
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
