// views/login.js — sign in.

import {h, mount} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import {t, setStrings} from '../lib/format.js';
import * as api from '../lib/api.js';

setStrings('en', {
  'login.title': 'Sign in',
  'login.lead': 'Sign in to manage this machine.',
  'login.username': 'Username',
  'login.password': 'Password',
  'login.submit': 'Sign in',
  'login.submitting': 'Signing in…',
  'login.needBoth': 'Enter your username and password.',
  'login.wrong': 'Wrong username or password.',
  'login.failed': 'Could not sign in.',
}, {activate: false});

export default {
  id: 'login',
  get title() { return t('login.title'); },

  async mount(root, ctx) {
    let busy = false;

    const problem = h('div', {class: 'notice notice--error hidden', role: 'alert'},
      icon('warning', {size: 18}),
      h('div', {class: 'notice__body'}, h('span', {class: 'problem-text'}, '')),
    );

    const username = h('input', {
      type: 'text', id: 'login-username', name: 'username',
      autocomplete: 'username', autocapitalize: 'none', autocorrect: 'off',
      spellcheck: 'false', required: true, placeholder: 'admin',
    });

    const password = h('input', {
      type: 'password', id: 'login-password', name: 'password',
      autocomplete: 'current-password', required: true,
    });

    const submit = h('button', {class: 'btn btn--primary btn--block btn--lg', type: 'submit'}, t('login.submit'));

    function showProblem(message) {
      const target = problem.querySelector('.problem-text');
      mount(target, message);
      problem.classList.remove('hidden');
    }

    function hideProblem() {
      problem.classList.add('hidden');
    }

    async function onSubmit(event) {
      event.preventDefault();
      if (busy) return;
      hideProblem();

      const user = username.value.trim();
      const pass = password.value;
      if (!user || !pass) {
        showProblem(t('login.needBoth'));
        (user ? password : username).focus();
        return;
      }

      busy = true;
      submit.classList.add('is-busy');
      submit.disabled = true;
      mount(submit, [h('span', {class: 'spinner spinner--sm'}), t('login.submitting')]);

      try {
        await api.post('/auth/login', {username: user, password: pass}, {redirectOnAuth: false});
        password.value = '';
        await ctx.reload();
      } catch (err) {
        busy = false;
        submit.classList.remove('is-busy');
        submit.disabled = false;
        mount(submit, t('login.submit'));
        if (err && err.status === 401) showProblem(t('login.wrong'));
        else if (err && err.status === 428) showProblem('project-os has not been set up yet.');
        else showProblem((err && err.message) || t('login.failed'));
        password.select();
        password.focus();
      }
    }

    const form = h('form', {class: 'form', onSubmit: onSubmit, novalidate: true},
      problem,
      h('div', {class: 'field'},
        h('label', {class: 'field__label', for: 'login-username'}, t('login.username')),
        username,
      ),
      h('div', {class: 'field'},
        h('label', {class: 'field__label', for: 'login-password'}, t('login.password')),
        password,
      ),
      submit,
    );

    const authDisabled = ctx.health && ctx.health.auth_enabled === false;

    mount(root, h('div', {class: 'auth__card'},
      h('div', {class: 'auth__brand'},
        h('span', {class: 'auth__mark'}, icon('chip', {size: 22})),
        h('h1', {class: 'auth__title'}, 'project-os'),
      ),
      h('p', {class: 'auth__lead'}, t('login.lead')),
      form,
      authDisabled
        ? h('p', {class: 'auth__foot'}, 'Authentication is disabled in the configuration — anyone on this network can control project-os.')
        : null,
      h('p', {class: 'auth__foot'}, t('app.name') + (ctx.health && ctx.health.version ? ' ' + ctx.health.version : '')),
    ));

    // Autofocus after the node is in the document.
    username.focus();

    return () => {
      /* nothing retained: no timers, no subscriptions */
    };
  },
};
