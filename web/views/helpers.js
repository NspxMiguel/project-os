// views/helpers.js — Ajudantes: other machines lending this Pi a hand.
//
// *"opção de conectar outros pcs pra ajuda no processamento tbm é legal...
// suporte a windows mac e linux"* and *"o esp32 tbm n precisa ser só um
// sistema, poderia ser tbm algo q ajuda o rp, algum acessorio"* — one screen,
// because the backend treats a laptop lending CPU and an ESP32 lending a
// sensor pin as the same idea: something that paired and declared what it
// can do (`project_os/core/helpers.py`).
//
// This is a job queue, not process migration — the backend says so in its own
// docstring, and the copy here repeats it on purpose. A queued job waits for
// whichever helper can do it; nothing here moves a running program anywhere.

import {h, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import {setStrings, t} from '../lib/format.js';
import * as api from '../lib/api.js';
import {toast, confirm, promptText} from '../lib/toast.js';

setStrings('en', {
  'helpers.title': 'Helpers',
  'helpers.lead': 'Other computers, boards and boxes lending this Pi a hand — a queue of whole tasks, not shared processes.',
  'helpers.section.paired': 'Paired helpers',
  'helpers.section.pair': 'Pair a new helper',
  'helpers.section.codes': 'Outstanding codes',
  'helpers.section.jobs': 'Job queue',
  'helpers.empty.paired.title': 'No helpers paired yet',
  'helpers.empty.paired.text': 'Once a PC, another Pi or an ESP32 pairs, it shows up here with what it can offer.',
  'helpers.empty.codes.title': 'No codes waiting',
  'helpers.empty.codes.text': 'Generate one above and it will appear here until it is used or expires.',
  'helpers.empty.jobs.title': 'Queue is empty',
  'helpers.empty.jobs.text': 'Jobs handed to a helper — a transcode, a download, a smoke-test ping — will list here with their state.',
  'helpers.error.load': 'Could not load the helpers screen.',
  'helpers.online': 'Online',
  'helpers.offline': 'Offline',
  'helpers.lastSeen': 'last seen {time}',
  'helpers.neverSeen': 'never reported in',
  'helpers.offers': 'Offers: {list}',
  'helpers.offersNone': 'Declared nothing yet',
  'helpers.rename': 'Rename',
  'helpers.rename.prompt': 'New name for {name}',
  'helpers.enable': 'Enable',
  'helpers.disable': 'Disable',
  'helpers.forget': 'Forget',
  'helpers.forget.title': 'Forget {name}?',
  'helpers.forget.detail': 'This does not lose its work — any job it is running goes back to the queue for another helper. It just stops being paired; pairing again needs a new code.',
  'helpers.forgotten': '{name} forgotten',
  'helpers.renamed': 'Renamed to {name}',
  'helpers.enabled': '{name} enabled',
  'helpers.disabled': '{name} disabled',
  'helpers.kind.pc': 'Computer',
  'helpers.kind.pi': 'Raspberry Pi',
  'helpers.kind.esp32': 'ESP32',
  'helpers.kind.other': 'Unknown device',
  'helpers.codes.new': 'Generate code',
  'helpers.codes.kind.label': 'Restrict to',
  'helpers.codes.kind.any': 'Any kind',
  'helpers.codes.ttl.label': 'Valid for',
  'helpers.codes.ttl.10m': '10 minutes',
  'helpers.codes.ttl.30m': '30 minutes',
  'helpers.codes.ttl.1h': '1 hour',
  'helpers.codes.expiresIn': 'expires in {time}',
  'helpers.codes.expired': 'expired',
  'helpers.codes.revoke': 'Revoke',
  'helpers.codes.revoked': 'Code revoked',
  'helpers.codes.copy.pc.title': 'On a Windows, macOS or Linux machine',
  'helpers.codes.copy.pc.body': 'Copy {file} from the project-os repo onto that machine and run:',
  'helpers.codes.copy.esp32.title': 'On an ESP32',
  'helpers.codes.copy.esp32.body': 'Flash MicroPython, copy {file} as main.py, and before rebooting fill in these settings:',
  'helpers.codes.copy.esp32.fields': 'WIFI_SSID, WIFI_PASSWORD, SERVER ({server}), PAIRING_CODE ({code}), and CAPABILITIES for whatever is actually wired up.',
  'helpers.codes.copied': 'Command copied',
  'helpers.jobs.submit': 'Submit test job',
  'helpers.act.read': 'Read now',
  'helpers.act.download': 'Download here',
  'helpers.act.download.prompt': 'Address of the file for {name} to fetch',
  'helpers.act.download.confirm': 'Download',
  'helpers.act.transcode': 'Convert a file',
  'helpers.act.transcode.input': 'File on {name} to convert',
  'helpers.act.transcode.output': 'Where to write the converted file',
  'helpers.act.transcode.confirm': 'Convert',
  'helpers.act.transcode.next': 'Next',
  'helpers.act.transcode.detail': 'The path is on {name}, not on the Pi — a shared folder is the usual way.',
  'helpers.act.staysThere': 'The file stays on {name}: project-os queues the job, it does not copy the result back.',
  'helpers.act.relayOn': 'Relay on',
  'helpers.act.relayOff': 'off',
  'helpers.act.queued': 'Sent to {name}. It runs on the next beat.',
  'helpers.fact.reading': 'reading',
  'helpers.fact.button': 'button',
  'helpers.fact.uptime': 'up for',
  'helpers.fact.ip': 'ip',
  'helpers.jobs.submit.hint': 'A "ping" job needs no capability at all — the honest smoke test that a helper is really taking work.',
  'helpers.jobs.submitted': 'Job queued',
  'helpers.jobs.noRunner': 'Queued, but no paired helper currently offers this yet — it will run once one does.',
  'helpers.jobs.cancel': 'Cancel',
  'helpers.jobs.cancelled': 'Job cancelled',
  'helpers.jobs.state.queued': 'Queued',
  'helpers.jobs.state.running': 'Running',
  'helpers.jobs.state.done': 'Done',
  'helpers.jobs.state.failed': 'Failed',
  'helpers.jobs.state.cancelled': 'Cancelled',
  'helpers.jobs.col.kind': 'Job',
  'helpers.jobs.col.state': 'State',
  'helpers.jobs.col.helper': 'Ran on',
  'helpers.jobs.col.result': 'Result',
  'helpers.jobs.col.when': 'Queued',
  'helpers.jobs.unassigned': '—',
  'helpers.stats.online': '{online} of {total} online',
  'helpers.badge.disabled': 'Disabled',
  'helpers.command.copy': 'Copy',
}, {activate: false});

/* --------------------------------------------------------------- helpers */

function card(title, tools, body) {
  return h('div', {class: 'card'},
    h('div', {class: 'card__header'},
      h('div', {class: 'grow'}, h('div', {class: 'card__title'}, title)),
      tools ? h('div', {class: 'card__tools'}, tools) : null,
    ),
    h('div', {class: 'card__body'}, body),
  );
}

function skeletonCard() {
  return h('div', {class: 'card'},
    h('div', {class: 'card__header'}, h('div', {class: 'skeleton skeleton--title', style: {flex: '1'}})),
    h('div', {class: 'card__body'},
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
              icon('refresh', {size: 14}), t('action.retry')),
          )
        : null,
    ),
  );
}

function emptyState(title, message) {
  return h('div', {class: 'empty empty--sm'},
    h('span', {class: 'empty__icon'}, icon('info', {size: 18})),
    h('p', {class: 'empty__title'}, title),
    message ? h('p', {class: 'empty__text'}, message) : null,
  );
}

const KIND_ICON = {pc: 'server', pi: 'chip', esp32: 'chip', other: 'box'};

function kindLabel(kind) {
  return t('helpers.kind.' + kind) === 'helpers.kind.' + kind ? kind : t('helpers.kind.' + kind);
}

/** Countdown text from an ISO expiry timestamp. Never negative. */
function timeLeft(expiresAt) {
  const target = Date.parse(expiresAt);
  if (!isFinite(target)) return null;
  const seconds = Math.max(0, Math.round((target - Date.now()) / 1000));
  return seconds;
}

function mmss(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m + ':' + String(s).padStart(2, '0');
}

/* ------------------------------------------------------------------ view */

export default {
  id: 'helpers',
  get title() { return t('nav.helpers'); },

  async mount(root, ctx) {
    let disposed = false;

    let helpersList = [];
    let stats = null;
    let kinds = [];
    let codes = [];
    let jobs = [];
    let loadError = null;

    // Form state for the pairing-code panel.
    let newCodeKind = '';
    let newCodeTtl = 600;

    const body = h('div', {class: 'stack stack--lg'});
    root.appendChild(h('div', {class: 'stack stack--lg'},
      h('div', {class: 'page__header'},
        h('h1', null, t('helpers.title')),
        h('p', {class: 'page__lead'}, t('helpers.lead')),
      ),
      body,
    ));

    function paintLoading() {
      clear(body);
      body.appendChild(skeletonCard());
      body.appendChild(skeletonCard());
      body.appendChild(skeletonCard());
    }

    function paintError() {
      clear(body);
      body.appendChild(errorState(loadError, load));
    }

    /* ----------------------------------------------------- paired helpers */

    /** O que o ajudante contou de si na última batida.
     *
     *  O ESP32 manda a leitura do sensor e o clique do botão a cada 15
     *  segundos desde o primeiro dia, e nada no `web/` lia `facts`: a
     *  temperatura do quarto dos passarinhos virava um número sobrescrito numa
     *  linha do SQLite. Agora ela aparece.
     */
    function fatosDoAjudante(helper) {
      const facts = helper.facts && typeof helper.facts === 'object' ? helper.facts : null;
      if (!facts) return null;
      const escondidos = ['platform', 'python', 'version', 'hostname', 'firmware', 'board',
                          'sensor_pin', 'relay_pin', 'button_pin'];
      const pares = Object.keys(facts)
        .filter((k) => escondidos.indexOf(k) < 0)
        .filter((k) => facts[k] !== null && facts[k] !== undefined && facts[k] !== '')
        .map((k) => h('span', {class: 'tag'}, rotuloDoFato(k) + ': ' + String(facts[k])));
      if (!pares.length) return null;
      return h('div', {class: 'row', style: {gap: 'var(--space-1)', marginTop: '4px'}}, pares);
    }

    function rotuloDoFato(chave) {
      const conhecido = t('helpers.fact.' + chave);
      return conhecido === 'helpers.fact.' + chave ? chave : conhecido;
    }

    /** Botões que mandam trabalho de verdade para *este* ajudante.
     *
     *  Antes, a única tarefa que a tela sabia enfileirar era um ping, e ainda
     *  por cima para quem pegasse primeiro. O firmware do ESP32 já sabia
     *  executar `read` e `relay` desde o começo -- faltava quem pedisse.
     */
    function controlesDoAjudante(helper) {
      const capacidades = helper.capabilities || [];
      const botoes = [];
      if (capacidades.indexOf('sensor') >= 0) {
        botoes.push(h('button', {
          class: 'btn btn--sm btn--ghost', type: 'button', disabled: !helper.online,
          onClick: () => enviarTarefa(helper, 'read', {}, ['sensor']),
        }, icon('pulse', {size: 14}), t('helpers.act.read')));
      }
      if (capacidades.indexOf('actuator') >= 0) {
        botoes.push(h('button', {
          class: 'btn btn--sm btn--ghost', type: 'button', disabled: !helper.online,
          onClick: () => enviarTarefa(helper, 'relay', {on: true}, ['actuator']),
        }, icon('power', {size: 14}), t('helpers.act.relayOn')));
        botoes.push(h('button', {
          class: 'btn btn--sm btn--ghost', type: 'button', disabled: !helper.online,
          onClick: () => enviarTarefa(helper, 'relay', {on: false}, ['actuator']),
        }, t('helpers.act.relayOff')));
      }
      // Um PC anuncia "baixar arquivos grandes" e "converter vídeo e áudio", e
      // a tela mostrava esses rótulos sem nenhuma forma de pedir nem uma coisa
      // nem outra: a única tarefa que dava para enfileirar era o ping. O
      // arquivo fica na máquina do ajudante -- não há transporte de volta --,
      // e a tela diz isso antes, no lugar de descobrir depois.
      if (capacidades.indexOf('download') >= 0) {
        botoes.push(h('button', {
          class: 'btn btn--sm btn--ghost', type: 'button', disabled: !helper.online,
          onClick: () => pedirDownload(helper),
        }, icon('download', {size: 14}), t('helpers.act.download')));
      }
      if (capacidades.indexOf('transcode') >= 0) {
        botoes.push(h('button', {
          class: 'btn btn--sm btn--ghost', type: 'button', disabled: !helper.online,
          onClick: () => pedirConversao(helper),
        }, icon('film', {size: 14}), t('helpers.act.transcode')));
      }
      if (!botoes.length) return null;
      return h('div', {class: 'row', style: {gap: 'var(--space-1)'}}, botoes);
    }

    async function pedirDownload(helper) {
      const url = await promptText(t('helpers.act.download.prompt', {name: helper.name}), {
        title: t('helpers.act.download'),
        confirmLabel: t('helpers.act.download.confirm'),
        placeholder: 'https://…',
        detail: t('helpers.act.staysThere', {name: helper.name}),
      });
      if (!url || !url.trim()) return;
      await enviarTarefa(helper, 'download', {url: url.trim()}, ['download']);
    }

    async function pedirConversao(helper) {
      const entrada = await promptText(t('helpers.act.transcode.input', {name: helper.name}), {
        title: t('helpers.act.transcode'),
        confirmLabel: t('helpers.act.transcode.next'),
        placeholder: '/mnt/midia/video.mkv',
        detail: t('helpers.act.transcode.detail', {name: helper.name}),
      });
      if (!entrada || !entrada.trim()) return;
      const saida = await promptText(t('helpers.act.transcode.output'), {
        title: t('helpers.act.transcode'),
        confirmLabel: t('helpers.act.transcode.confirm'),
        placeholder: '/mnt/midia/video.mp4',
        detail: t('helpers.act.staysThere', {name: helper.name}),
      });
      if (!saida || !saida.trim()) return;
      await enviarTarefa(
        helper, 'transcode', {input: entrada.trim(), output: saida.trim()}, ['transcode'],
      );
    }

    async function enviarTarefa(helper, kind, payload, needs) {
      try {
        await api.post('/helpers/jobs', {kind, payload, needs, helper_id: helper.id});
        toast(t('helpers.act.queued', {name: helper.name}), {type: 'success'});
        await loadJobs();
        paintAll();
      } catch (err) {
        toast((err && err.message) || t('helpers.error.load'), {type: 'error'});
      }
    }

    function helperRow(helper) {
      const since = helper.online
        ? null
        : (helper.seconds_since_seen === null || helper.seconds_since_seen === undefined
            ? t('helpers.neverSeen')
            : t('helpers.lastSeen', {time: fmt.relativeTime(helper.last_seen)}));

      const offers = helper.labels && helper.labels.length
        ? t('helpers.offers', {list: helper.labels.join(', ')})
        : t('helpers.offersNone');

      return h('div', {class: 'list__row'},
        h('span', {class: 'badge badge--plain'}, icon(KIND_ICON[helper.kind] || 'box', {size: 16})),
        h('div', {class: 'list__main'},
          h('div', {class: 'list__title'},
            helper.name,
            h('span', {class: 'badge ' + (helper.online ? 'badge--ok' : 'badge--plain')},
              helper.online ? t('helpers.online') : t('helpers.offline')),
            !helper.enabled ? h('span', {class: 'badge badge--warn'}, t('helpers.badge.disabled')) : null,
          ),
          h('div', {class: 'list__sub'},
            [kindLabel(helper.kind), helper.platform, since].filter(Boolean).join(' · '),
          ),
          h('div', {class: 'list__sub'}, offers),
          fatosDoAjudante(helper),
        ),
        h('div', {class: 'list__aside'},
          controlesDoAjudante(helper),
          h('button', {
            class: 'btn btn--sm btn--ghost', type: 'button',
            onClick: () => renameHelper(helper),
          }, icon('edit', {size: 14}), t('helpers.rename')),
          h('button', {
            class: 'btn btn--sm btn--ghost', type: 'button',
            onClick: () => toggleHelper(helper),
          }, icon(helper.enabled ? 'power' : 'sync', {size: 14}),
             helper.enabled ? t('helpers.disable') : t('helpers.enable')),
          h('button', {
            class: 'btn btn--sm btn--danger', type: 'button',
            onClick: () => forgetHelper(helper),
          }, icon('trash', {size: 14}), t('helpers.forget')),
        ),
      );
    }

    async function renameHelper(helper) {
      const next = await promptText(t('helpers.rename.prompt', {name: helper.name}), {
        title: t('helpers.rename'),
        confirmLabel: t('helpers.rename'),
        value: helper.name,
      });
      if (next === null) return;
      const name = next.trim();
      if (!name || name === helper.name) return;
      try {
        await api.patch('/helpers/' + helper.id, {name});
        toast(t('helpers.renamed', {name}), {type: 'success'});
        await load();
      } catch (err) {
        toast((err && err.message) || loadError, {type: 'error'});
      }
    }

    async function toggleHelper(helper) {
      try {
        await api.post('/helpers/' + helper.id + '/enabled', {enabled: !helper.enabled});
        toast(
          !helper.enabled
            ? t('helpers.enabled', {name: helper.name})
            : t('helpers.disabled', {name: helper.name}),
          {type: 'success'},
        );
        await load();
      } catch (err) {
        toast((err && err.message) || t('helpers.error.load'), {type: 'error'});
      }
    }

    async function forgetHelper(helper) {
      const ok = await confirm(t('helpers.forget.title', {name: helper.name}), {
        danger: true,
        detail: t('helpers.forget.detail'),
        confirmLabel: t('helpers.forget'),
      });
      if (!ok) return;
      try {
        await api.del('/helpers/' + helper.id);
        toast(t('helpers.forgotten', {name: helper.name}), {type: 'success'});
        await load();
      } catch (err) {
        toast((err && err.message) || t('helpers.error.load'), {type: 'error'});
      }
    }

    function pairedCard() {
      const statsLine = stats
        ? h('span', {class: 'small muted'}, t('helpers.stats.online', {online: stats.online, total: stats.helpers}))
        : null;
      const list = helpersList.length
        ? h('div', {class: 'list'}, helpersList.map(helperRow))
        : emptyState(t('helpers.empty.paired.title'), t('helpers.empty.paired.text'));
      return card(t('helpers.section.paired'), statsLine, list);
    }

    /* ----------------------------------------------------------- pairing */

    let liveCode = null; // {code, kind, created_at, expires_at, expires_in}
    let countdownTimer = null;

    function pairCommand(code) {
      const origin = window.location.origin;
      return 'python3 helper_agent.py --pair ' + origin + ' ' + code;
    }

    async function mintCode() {
      try {
        const created = await api.post('/helpers/codes', {
          kind: newCodeKind || undefined,
          ttl: newCodeTtl,
        });
        liveCode = created;
        startCountdown();
        await loadCodes();
        paintAll();
      } catch (err) {
        toast((err && err.message) || t('helpers.error.load'), {type: 'error'});
      }
    }

    function startCountdown() {
      stopCountdown();
      countdownTimer = setInterval(() => {
        if (disposed) return;
        const codeCard = document.getElementById('helpers-live-code');
        if (!codeCard || !liveCode) { stopCountdown(); return; }
        const left = timeLeft(liveCode.expires_at);
        if (left <= 0) {
          liveCode = null;
          stopCountdown();
          paintAll();
          return;
        }
        const el = codeCard.querySelector('[data-countdown]');
        if (el) el.textContent = mmss(left);
      }, 1000);
    }

    function stopCountdown() {
      if (countdownTimer) clearInterval(countdownTimer);
      countdownTimer = null;
    }

    function pairingCard() {
      const kindSelect = h('select', {value: newCodeKind,
        onChange: (event) => { newCodeKind = event.target.value; }},
        h('option', {value: ''}, t('helpers.codes.kind.any')),
        kinds.map((k) => h('option', {value: k}, kindLabel(k))),
      );
      const ttlSelect = h('select', {value: String(newCodeTtl),
        onChange: (event) => { newCodeTtl = Number(event.target.value); }},
        h('option', {value: '600'}, t('helpers.codes.ttl.10m')),
        h('option', {value: '1800'}, t('helpers.codes.ttl.30m')),
        h('option', {value: '3600'}, t('helpers.codes.ttl.1h')),
      );

      const form = h('div', {class: 'fields'},
        h('div', {class: 'field'},
          h('label', {class: 'field__label'}, t('helpers.codes.kind.label')), kindSelect),
        h('div', {class: 'field'},
          h('label', {class: 'field__label'}, t('helpers.codes.ttl.label')), ttlSelect),
        h('button', {class: 'btn btn--primary', type: 'button', onClick: mintCode},
          icon('key', {size: 14}), t('helpers.codes.new')),
      );

      const codePanel = liveCode ? liveCodePanel() : null;
      return card(t('helpers.section.pair'), null, h('div', {class: 'stack'}, form, codePanel));
    }

    function liveCodePanel() {
      const left = timeLeft(liveCode.expires_at);
      const command = pairCommand(liveCode.code);
      return h('div', {class: 'stack stack--sm', id: 'helpers-live-code'},
        h('div', {class: 'row row--between'},
          h('span', {class: 'mono', style: {fontSize: 'var(--text-2xl, 2rem)', letterSpacing: '0.15em'}}, liveCode.code),
          h('span', {class: 'badge badge--info'}, icon('clock', {size: 13}),
            h('span', {'data-countdown': true}, left !== null ? mmss(left) : '—')),
        ),
        h('div', {class: 'notice notice--info'},
          icon('info', {size: 16}),
          h('div', {class: 'notice__body'},
            h('div', {class: 'notice__title'}, t('helpers.codes.copy.pc.title')),
            h('p', null, t('helpers.codes.copy.pc.body', {file: 'agents/helper_agent.py'})),
            h('div', {class: 'row row--between'},
              h('code', {class: 'code'}, command),
              h('button', {class: 'btn btn--sm', type: 'button', onClick: () => copyText(command)},
                icon('code', {size: 13}), t('helpers.command.copy')),
            ),
          ),
        ),
        h('div', {class: 'notice notice--info'},
          icon('chip', {size: 16}),
          h('div', {class: 'notice__body'},
            h('div', {class: 'notice__title'}, t('helpers.codes.copy.esp32.title')),
            h('p', null, t('helpers.codes.copy.esp32.body', {file: 'agents/esp32/main.py'})),
            h('p', {class: 'small muted'}, t('helpers.codes.copy.esp32.fields', {
              server: window.location.origin,
              code: liveCode.code,
            })),
          ),
        ),
      );
    }

    async function copyText(text) {
      try {
        await navigator.clipboard.writeText(text);
        toast(t('helpers.codes.copied'), {type: 'success'});
      } catch (err) {
        // Clipboard permission denied or unavailable — the command is still
        // visible and selectable, so this is a convenience, not a blocker.
      }
    }

    /* ----------------------------------------------------- outstanding codes */

    async function loadCodes() {
      const answer = await api.get('/helpers/codes');
      codes = answer.codes || [];
    }

    async function revokeCode(code) {
      try {
        await api.del('/helpers/codes/' + code.code);
        toast(t('helpers.codes.revoked'), {type: 'success'});
        if (liveCode && liveCode.code === code.code) { liveCode = null; stopCountdown(); }
        await loadCodes();
        paintAll();
      } catch (err) {
        toast((err && err.message) || t('helpers.error.load'), {type: 'error'});
      }
    }

    function codeRow(code) {
      const left = timeLeft(code.expires_at);
      const expired = left === null || left <= 0;
      return h('div', {class: 'list__row'},
        h('span', {class: 'mono'}, code.code),
        h('div', {class: 'list__main'},
          h('div', {class: 'list__sub'},
            expired ? t('helpers.codes.expired') : t('helpers.codes.expiresIn', {time: mmss(left)}),
            code.kind ? ' · ' + kindLabel(code.kind) : '',
          ),
        ),
        h('div', {class: 'list__aside'},
          h('button', {class: 'btn btn--sm btn--ghost', type: 'button', onClick: () => revokeCode(code)},
            icon('x', {size: 14}), t('helpers.codes.revoke')),
        ),
      );
    }

    function codesCard() {
      const list = codes.length
        ? h('div', {class: 'list'}, codes.map(codeRow))
        : emptyState(t('helpers.empty.codes.title'), t('helpers.empty.codes.text'));
      return card(t('helpers.section.codes'), null, list);
    }

    /* --------------------------------------------------------------- jobs */

    async function submitPing() {
      try {
        const answer = await api.post('/helpers/jobs', {kind: 'ping', payload: {}, needs: []});
        toast(answer.will_run ? t('helpers.jobs.submitted') : t('helpers.jobs.noRunner'),
          {type: answer.will_run ? 'success' : 'info'});
        await loadJobs();
        paintAll();
      } catch (err) {
        toast((err && err.message) || t('helpers.error.load'), {type: 'error'});
      }
    }

    async function cancelJob(job) {
      try {
        await api.del('/helpers/jobs/' + job.id);
        toast(t('helpers.jobs.cancelled'), {type: 'success'});
        await loadJobs();
        paintAll();
      } catch (err) {
        toast((err && err.message) || t('helpers.error.load'), {type: 'error'});
      }
    }

    async function loadJobs() {
      const answer = await api.get('/helpers/jobs', {query: {limit: 50}});
      jobs = answer.jobs || [];
    }

    function jobStateBadge(job) {
      const level = job.state === 'done' ? 'ok'
        : job.state === 'failed' ? 'danger'
        : job.state === 'running' ? 'info'
        : job.state === 'cancelled' ? 'plain'
        : 'warn';
      return h('span', {class: 'badge badge--' + level}, t('helpers.jobs.state.' + job.state));
    }

    function jobRow(job) {
      const helperName = job.helper_id
        ? (helpersList.find((h2) => h2.id === job.helper_id) || {}).name || job.helper_id
        : t('helpers.jobs.unassigned');
      const resultText = job.error
        ? job.error
        : (job.result !== null && job.result !== undefined ? JSON.stringify(job.result) : '—');
      return h('tr', null,
        h('td', null, job.kind),
        h('td', null, jobStateBadge(job)),
        h('td', null, helperName),
        h('td', null, h('span', {class: 'truncate', style: {maxWidth: '24ch', display: 'inline-block'}}, resultText)),
        h('td', null, fmt.relativeTime(job.created_at)),
        h('td', null,
          (job.state === 'queued')
            ? h('button', {class: 'btn btn--sm btn--ghost', type: 'button', onClick: () => cancelJob(job)},
                t('helpers.jobs.cancel'))
            : null,
        ),
      );
    }

    function jobsCard() {
      const submitBtn = h('button', {class: 'btn btn--sm btn--primary', type: 'button', onClick: submitPing},
        icon('pulse', {size: 14}), t('helpers.jobs.submit'));
      const body2 = jobs.length
        ? h('div', {class: 'stack'},
            h('p', {class: 'small muted'}, t('helpers.jobs.submit.hint')),
            h('div', {class: 'table-wrap'},
              h('table', {class: 'table'},
                h('thead', null, h('tr', null,
                  h('th', null, t('helpers.jobs.col.kind')),
                  h('th', null, t('helpers.jobs.col.state')),
                  h('th', null, t('helpers.jobs.col.helper')),
                  h('th', null, t('helpers.jobs.col.result')),
                  h('th', null, t('helpers.jobs.col.when')),
                  h('th', null, ''),
                )),
                h('tbody', null, jobs.map(jobRow)),
              ),
            ),
          )
        : h('div', {class: 'stack'},
            h('p', {class: 'small muted'}, t('helpers.jobs.submit.hint')),
            emptyState(t('helpers.empty.jobs.title'), t('helpers.empty.jobs.text')),
          );
      return card(t('helpers.section.jobs'), submitBtn, body2);
    }

    /* ------------------------------------------------------------ layout */

    function paintAll() {
      if (disposed) return;
      clear(body);
      body.appendChild(pairedCard());
      body.appendChild(pairingCard());
      body.appendChild(codesCard());
      body.appendChild(jobsCard());
    }

    async function load() {
      loadError = null;
      paintLoading();
      try {
        const [helpersAnswer] = await Promise.all([
          api.get('/helpers'),
          loadCodes(),
          loadJobs(),
        ]);
        helpersList = helpersAnswer.helpers || [];
        stats = helpersAnswer.stats || null;
        kinds = (helpersAnswer.kinds || []).filter((k) => k !== 'other');
        paintAll();
      } catch (err) {
        loadError = (err && err.message) || t('helpers.error.load');
        paintError();
      }
    }

    /* ------------------------------------------------------------- live */

    const offOnline = ctx.ws ? ctx.ws.on('helpers.online', () => load()) : null;
    const offOffline = ctx.ws ? ctx.ws.on('helpers.offline', () => load()) : null;
    const offEvent = ctx.ws ? ctx.ws.on('helpers.event', () => load()) : null;

    // Cair não gera evento: um ajudante que some simplesmente para de bater.
    // Quem decide "offline" é a idade da última batida, e essa conta só muda
    // quando alguém pergunta de novo -- sem isto o selo Online ficava verde até
    // recarregar a página na mão. O intervalo é metade da carência de 120s, o
    // que faz o selo mudar no máximo um minuto depois da verdade.
    const relogio = setInterval(() => { if (!disposed) load(); }, 60000);

    await load();

    return () => {
      disposed = true;
      stopCountdown();
      clearInterval(relogio);
      if (offOnline) offOnline();
      if (offOffline) offOffline();
      if (offEvent) offEvent();
    };
  },
};
