// panel.js -- control panel for the whatsapp-bot app.
//
// Plain ES module, no build step, no framework: `h()` from project-os's own
// dom.js, `t()` for every user-visible string, and only classes that already
// exist in the shared web/style.css. See ctx.appApi for the routes this talks
// to (GET/PUT config, GET status, GET messages, POST send) -- all defined in
// ../app.py's build_router().

export default {
  async mount(root, ctx) {
    const {h, t, appApi, config, toast} = ctx;
    const fmtMod = await import('/lib/format.js');
    fmtMod.setStrings('en', {
      'wa.title': 'WhatsApp bot',
      'wa.provider': 'Provider',
      'wa.provider.null': 'None (log only)',
      'wa.provider.cloud_api': 'Cloud API (Meta)',
      'wa.provider.bridge': 'Local bridge',
      'wa.status.connected': 'Connected',
      'wa.status.notconnected': 'Not connected',
      'wa.status.reason': 'Reason',
      'wa.save': 'Save',
      'wa.section.connection': 'Connection',
      'wa.section.allowlist': 'Allowed contacts',
      'wa.section.messages': 'Recent messages',
      'wa.section.send': 'Send a test message',
      'wa.section.pairing': 'Bridge pairing',
      'wa.allowlist.empty': 'No contacts allowed yet. The bot ignores everyone until a number is added here.',
      'wa.allowlist.add': 'Add a number',
      'wa.allowlist.placeholder': 'e.g. 5511912345678',
      'wa.allowlist.added': 'Number added',
      'wa.allowlist.removed': 'Number removed',
      'wa.messages.empty': 'No messages yet.',
      'wa.messages.in': 'in',
      'wa.messages.out': 'out',
      'wa.send.to': 'To',
      'wa.send.text': 'Message',
      'wa.send.button': 'Send',
      'wa.send.sent': 'Message sent',
      'wa.send.failed': 'Could not send',
      'wa.cloud.phone_number_id': 'phone_number_id',
      'wa.cloud.access_token': 'access_token',
      'wa.cloud.verify_token': 'verify_token',
      'wa.cloud.app_secret': 'app_secret',
      'wa.bridge.base_url': 'Bridge base URL',
      'wa.bridge.token': 'Bridge token',
      'wa.commands.prefix': 'Command prefix',
      'wa.rate_limit': 'Max messages per minute per contact',
      'wa.pairing.hint': 'Open the bridge container\'s own logs or QR endpoint and scan with WhatsApp > Linked devices.',
      'wa.save.ok': 'Settings saved',
      'wa.save.failed': 'Could not save settings',
    }, {activate: false});

    const t2 = (key, vars) => {
      const value = fmtMod.t(key, vars);
      return value === key ? t(key, vars) : value;
    };

    const state = {
      status: null,
      cfg: config.all() || {},
      messages: [],
      busy: false,
    };

    const view = h('div', {class: 'stack'});
    root.appendChild(view);

    async function refreshStatus() {
      try {
        state.status = await appApi.get('/status');
      } catch (err) {
        state.status = {provider: 'null', connected: false, reason: String(err)};
      }
    }

    async function refreshMessages() {
      try {
        const raw = await appApi.get('/messages', {query: {limit: 30}});
        state.messages = (raw && raw.messages) || [];
      } catch (err) {
        state.messages = [];
      }
    }

    async function saveConfig(patch) {
      state.busy = true;
      render();
      try {
        state.cfg = await config.save(patch);
        toast(t2('wa.save.ok'), {type: 'success'});
        await refreshStatus();
      } catch (err) {
        toast(t2('wa.save.failed') + ': ' + (err.message || err), {type: 'error'});
      } finally {
        state.busy = false;
        render();
      }
    }

    function dig(obj, path, fallback) {
      const parts = String(path).split('.');
      let cur = obj;
      for (const part of parts) {
        if (cur === null || cur === undefined) return fallback;
        cur = cur[part];
      }
      return cur === undefined ? fallback : cur;
    }

    function connectionCard() {
      const status = state.status || {};
      const provider = String(status.provider || 'null');
      const connected = Boolean(status.connected);

      return h('div', {class: 'card'},
        h('div', {class: 'card__header'},
          h('h3', {class: 'card__title'}, t2('wa.section.connection')),
          h('div', {class: 'card__tools'},
            h('span', {class: 'conn', dataset: {state: connected ? 'open' : 'closed'}},
              h('span', {class: 'conn__dot'}),
              connected ? t2('wa.status.connected') : t2('wa.status.notconnected')),
          ),
        ),
        h('div', {class: 'card__body form'},
          h('div', {class: 'field'},
            h('label', {class: 'field__label'}, t2('wa.provider')),
            h('select', {
              value: provider,
              onChange: (event) => saveConfig({provider: event.target.value}),
            },
              h('option', {value: 'null'}, t2('wa.provider.null')),
              h('option', {value: 'cloud_api'}, t2('wa.provider.cloud_api')),
              h('option', {value: 'bridge'}, t2('wa.provider.bridge')),
            ),
          ),
          !connected && status.reason
            ? h('p', {class: 'field__hint'}, t2('wa.status.reason') + ': ' + status.reason)
            : null,
          provider === 'cloud_api' ? cloudApiFields() : null,
          provider === 'bridge' ? bridgeFields() : null,
        ),
      );
    }

    function textField(label, path, key, {secret = false} = {}) {
      const current = dig(state.cfg, path, '');
      return h('div', {class: 'field'},
        h('label', {class: 'field__label'}, label),
        h('input', {
          type: secret ? 'password' : 'text',
          value: current === '********' ? '' : current,
          placeholder: current === '********' ? '********' : '',
          onChange: (event) => {
            const value = event.target.value;
            if (value === '') return; // never overwrite a saved secret with blank on blur
            const patch = {};
            patch[path] = value;
            saveConfig(patch);
          },
        }),
      );
    }

    function cloudApiFields() {
      return h('div', {class: 'stack'},
        textField(t2('wa.cloud.phone_number_id'), 'cloud_api.phone_number_id', 'phone_number_id'),
        textField(t2('wa.cloud.access_token'), 'cloud_api.access_token', 'access_token', {secret: true}),
        textField(t2('wa.cloud.verify_token'), 'cloud_api.verify_token', 'verify_token', {secret: true}),
        textField(t2('wa.cloud.app_secret'), 'cloud_api.app_secret', 'app_secret', {secret: true}),
      );
    }

    function bridgeFields() {
      const status = state.status || {};
      return h('div', {class: 'stack'},
        textField(t2('wa.bridge.base_url'), 'bridge.base_url', 'base_url'),
        textField(t2('wa.bridge.token'), 'bridge.token', 'token', {secret: true}),
        h('div', {class: 'notice notice--info'},
          h('strong', null, t2('wa.section.pairing')), ' ',
          status.pairing_hint || t2('wa.pairing.hint')),
      );
    }

    function allowlistCard() {
      const list = dig(state.cfg, 'allowlist', []) || [];
      let inputRef = null;
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'},
          h('h3', {class: 'card__title'}, t2('wa.section.allowlist')),
        ),
        h('div', {class: 'card__body stack'},
          list.length === 0
            ? h('div', {class: 'empty empty--sm'}, h('p', {class: 'empty__text'}, t2('wa.allowlist.empty')))
            : h('div', {class: 'list'}, list.map((entry) => h('div', {class: 'list__row'},
                h('div', {class: 'list__main'}, h('div', {class: 'list__title'}, entry)),
                h('div', {class: 'list__aside'},
                  h('button', {
                    class: 'btn btn--icon btn--sm',
                    title: t2('action.dismiss'),
                    onClick: () => saveConfig({allowlist: list.filter((x) => x !== entry)}),
                  }, ctx.icon('trash', {size: 16})),
                ),
              ))),
          h('div', {class: 'input-group'},
            h('input', {
              type: 'text',
              placeholder: t2('wa.allowlist.placeholder'),
              ref: (el) => { inputRef = el; },
            }),
            h('button', {
              class: 'btn btn--outline',
              onClick: () => {
                const value = inputRef && inputRef.value ? inputRef.value.trim() : '';
                if (!value) return;
                if (inputRef) inputRef.value = '';
                saveConfig({allowlist: list.concat([value])});
              },
            }, ctx.icon('plus', {size: 16}), t2('wa.allowlist.add')),
          ),
        ),
      );
    }

    function messagesCard() {
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'},
          h('h3', {class: 'card__title'}, t2('wa.section.messages')),
          h('div', {class: 'card__tools'},
            h('button', {class: 'btn btn--ghost btn--sm', onClick: async () => { await refreshMessages(); render(); }},
              ctx.icon('refresh', {size: 16})),
          ),
        ),
        h('div', {class: 'card__body'},
          state.messages.length === 0
            ? h('div', {class: 'empty empty--sm'}, h('p', {class: 'empty__text'}, t2('wa.messages.empty')))
            : h('div', {class: 'list'}, state.messages.map((msg) => h('div', {class: 'list__row'},
                h('span', {class: 'badge badge--' + (msg.direction === 'in' ? 'info' : 'accent')},
                  msg.direction === 'in' ? t2('wa.messages.in') : t2('wa.messages.out')),
                h('div', {class: 'list__main'},
                  h('div', {class: 'list__title'}, msg.contact),
                  h('div', {class: 'list__sub'}, msg.body),
                ),
                h('div', {class: 'list__aside'}, h('span', {class: 'list__sub'}, ctx.fmt.relativeTime(msg.created_at))),
              ))),
        ),
      );
    }

    function sendCard() {
      let toRef = null;
      let textRef = null;
      return h('div', {class: 'card'},
        h('div', {class: 'card__header'}, h('h3', {class: 'card__title'}, t2('wa.section.send'))),
        h('div', {class: 'card__body form'},
          h('div', {class: 'field'},
            h('label', {class: 'field__label'}, t2('wa.send.to')),
            h('input', {type: 'text', placeholder: t2('wa.allowlist.placeholder'), ref: (el) => { toRef = el; }}),
          ),
          h('div', {class: 'field'},
            h('label', {class: 'field__label'}, t2('wa.send.text')),
            h('textarea', {ref: (el) => { textRef = el; }}),
          ),
          h('button', {
            class: 'btn btn--primary',
            onClick: async (event) => {
              const to = toRef && toRef.value ? toRef.value.trim() : '';
              const text = textRef && textRef.value ? textRef.value.trim() : '';
              if (!to || !text) return;
              const btn = event.currentTarget;
              btn.disabled = true;
              try {
                await appApi.post('/send', {to, text});
                toast(t2('wa.send.sent'), {type: 'success'});
                if (textRef) textRef.value = '';
                await refreshMessages();
                render();
              } catch (err) {
                toast(t2('wa.send.failed') + ': ' + (err.message || err), {type: 'error'});
              } finally {
                btn.disabled = false;
              }
            },
          }, t2('wa.send.button')),
        ),
      );
    }

    function behaviorCard() {
      const prefix = dig(state.cfg, 'commands.prefix', '!');
      const rateLimit = dig(state.cfg, 'rate_limit.max_per_minute', 10);
      return h('div', {class: 'card'},
        h('div', {class: 'card__body form'},
          h('div', {class: 'field'},
            h('label', {class: 'field__label'}, t2('wa.commands.prefix')),
            h('input', {
              type: 'text', value: prefix,
              onChange: (event) => saveConfig({'commands.prefix': event.target.value}),
            }),
          ),
          h('div', {class: 'field'},
            h('label', {class: 'field__label'}, t2('wa.rate_limit')),
            h('input', {
              type: 'number', value: rateLimit, min: 1,
              onChange: (event) => saveConfig({'rate_limit.max_per_minute': Number(event.target.value) || 10}),
            }),
          ),
        ),
      );
    }

    function render() {
      const nodes = [connectionCard(), behaviorCard(), allowlistCard(), sendCard(), messagesCard()];
      view.replaceChildren(...nodes);
    }

    await refreshStatus();
    await refreshMessages();
    render();

    let cancelled = false;
    const poll = setInterval(async () => {
      if (cancelled) return;
      await refreshStatus();
      if (!cancelled) render();
    }, 15000);

    return () => {
      cancelled = true;
      clearInterval(poll);
    };
  },
};
