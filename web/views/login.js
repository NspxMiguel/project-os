// views/login.js — sign in.

import {h, mount} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import {t} from '../lib/format.js';
import * as api from '../lib/api.js';

export default {
  id: 'login',
  title: 'Sign in',

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

    const submit = h('button', {class: 'btn btn--primary btn--block btn--lg', type: 'submit'}, 'Sign in');

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
        showProblem('Enter your username and password.');
        (user ? password : username).focus();
        return;
      }

      busy = true;
      submit.classList.add('is-busy');
      submit.disabled = true;
      mount(submit, [h('span', {class: 'spinner spinner--sm'}), 'Signing in…']);

      try {
        await api.post('/auth/login', {username: user, password: pass}, {redirectOnAuth: false});
        password.value = '';
        await ctx.reload();
      } catch (err) {
        busy = false;
        submit.classList.remove('is-busy');
        submit.disabled = false;
        mount(submit, 'Sign in');
        if (err && err.status === 401) showProblem('Wrong username or password.');
        else if (err && err.status === 428) showProblem('ProjectOS has not been set up yet.');
        else showProblem((err && err.message) || 'Could not sign in.');
        password.select();
        password.focus();
      }
    }

    const form = h('form', {class: 'form', onSubmit: onSubmit, novalidate: true},
      problem,
      h('div', {class: 'field'},
        h('label', {class: 'field__label', for: 'login-username'}, 'Username'),
        username,
      ),
      h('div', {class: 'field'},
        h('label', {class: 'field__label', for: 'login-password'}, 'Password'),
        password,
      ),
      submit,
    );

    const authDisabled = ctx.health && ctx.health.auth_enabled === false;

    mount(root, h('div', {class: 'auth__card'},
      h('div', {class: 'auth__brand'},
        h('span', {class: 'auth__mark'}, icon('chip', {size: 22})),
        h('h1', {class: 'auth__title'}, 'ProjectOS'),
      ),
      h('p', {class: 'auth__lead'}, 'Sign in to manage this machine.'),
      form,
      authDisabled
        ? h('p', {class: 'auth__foot'}, 'Authentication is disabled in the configuration — anyone on this network can control ProjectOS.')
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
