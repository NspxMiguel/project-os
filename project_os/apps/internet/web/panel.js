// panel.js -- a tela do app Internet.
//
// Módulo ES puro, sem build: `h()` do dom.js do próprio project-os, `t()` para
// todo texto visível, e as classes que já existem no web/style.css. As rotas
// estão em ../app.py (build_router): GET /status, GET /outages, GET /samples,
// POST /check.
//
// A tela responde três perguntas, nesta ordem, porque é a ordem em que elas
// aparecem na cabeça de quem abre: está funcionando agora? quando foi a última
// queda? e o que aconteceu nas últimas 24 horas?

export default {
  async mount(root, ctx) {
    const {h, t, appApi, toast} = ctx;
    const fmtMod = await import('/lib/format.js');
    fmtMod.setStrings('en', {
      'net.state.ok': 'Working',
      'net.state.dns': 'Names are not resolving',
      'net.state.internet': 'No internet',
      'net.state.roteador': 'Router not answering',
      'net.state.ok.detail': 'Router, internet and names all answered.',
      'net.state.dns.detail': 'The connection works by address but not by name. This is usually the one you can fix yourself.',
      'net.state.internet.detail': 'The router is up and the provider is not delivering.',
      'net.state.roteador.detail': 'The router itself did not answer — check the cable or the wifi before calling anyone.',
      'net.since': 'Since %s',
      'net.check': 'Test now',
      'net.checking': 'Testing…',
      'net.latency.router': 'Router',
      'net.latency.internet': 'Internet',
      'net.latency.dns': 'Names (DNS)',
      'net.latency.none': 'no answer',
      'net.section.outages': 'Outages',
      'net.section.day': 'Last 24 hours',
      'net.outages.empty': 'No outage recorded yet.',
      'net.outages.open': 'still down',
      'net.day.summary': '%s outages, %s off the air',
      'net.day.summary.one': 'one outage, %s off the air',
      'net.day.clean': 'No outage in the last 24 hours.',
      'net.day.empty': 'No measurement yet — the first one takes a minute.',
      'net.every': 'Measuring every %s s',
    }, {activate: false});

    const t2 = (key) => {
      const value = fmtMod.t(key);
      return value === key ? t(key) : value;
    };
    const fmtStr = (key, ...args) => {
      let texto = t2(key);
      args.forEach((a) => { texto = texto.replace(/%s|%d/, String(a)); });
      return texto;
    };

    const state = {status: null, outages: [], samples: [], checking: false};

    const view = h('div', {class: 'stack'});
    root.appendChild(view);

    const duracao = (segundos) => {
      const s = Math.max(0, Math.round(Number(segundos) || 0));
      if (s < 60) return s + 's';
      if (s < 3600) return Math.round(s / 60) + 'min';
      const horas = Math.floor(s / 3600);
      const min = Math.round((s % 3600) / 60);
      return min ? horas + 'h' + min + 'min' : horas + 'h';
    };

    // Pelo formatador do sistema, e não por toLocaleString cru: ele segue o
    // idioma escolhido na caixa, então a data não sai em formato americano no
    // meio de uma tela em português.
    const quando = (iso) => (iso ? fmtMod.dateTime(iso, {withSeconds: true}) : '—');
    const desde = (iso) => (iso ? fmtMod.dateTime(iso) : '—');

    const ms = (valor) => (valor === null || valor === undefined
      ? t2('net.latency.none') : Math.round(Number(valor)) + ' ms');

    async function carregar() {
      try {
        state.status = await appApi.get('/status');
      } catch (err) { state.status = null; }
      try {
        state.outages = (await appApi.get('/outages', {query: {limit: 20}})).outages || [];
      } catch (err) { state.outages = []; }
      try {
        state.samples = (await appApi.get('/samples', {query: {hours: 24}})).samples || [];
      } catch (err) { state.samples = []; }
    }

    async function testarAgora() {
      if (state.checking) return;
      state.checking = true;
      render();
      try {
        const r = await appApi.post('/check', {});
        state.status = r.status || state.status;
        await carregar();
      } catch (err) {
        toast(String((err && err.message) || err), {type: 'error'});
      } finally {
        state.checking = false;
        render();
      }
    }

    // O cartão de cima: o estado agora, e o porquê em uma frase.
    function cartaoEstado() {
      const s = state.status || {};
      const estado = s.state || 'ok';
      const ok = estado === 'ok';
      const ultima = s.last || {};
      return h('div', {class: 'card net-now net-now--' + estado},
        h('div', {class: 'net-now__head'},
          h('div', null,
            h('div', {class: 'net-now__state'}, t2('net.state.' + estado)),
            h('div', {class: 'small muted'}, t2('net.state.' + estado + '.detail')),
            s.since ? h('div', {class: 'small muted'}, fmtStr('net.since', desde(s.since))) : null),
          h('button', {
            class: 'btn btn--outline btn--sm', disabled: state.checking,
            onClick: testarAgora,
          }, state.checking ? t2('net.checking') : t2('net.check'))),
        h('div', {class: 'net-lat'},
          medida(t2('net.latency.router'), ultima.gateway_ms, ultima.gateway),
          medida(t2('net.latency.internet'), ultima.internet_ms, ultima.internet_target),
          medida(t2('net.latency.dns'), ultima.dns_ms, '')),
        h('div', {class: 'small muted'},
          fmtStr('net.every', Math.round(Number(s.interval_seconds) || 60))));
    }

    function medida(rotulo, valor, detalhe) {
      const caiu = valor === null || valor === undefined;
      return h('div', {class: 'net-lat__item' + (caiu ? ' net-lat__item--down' : '')},
        h('div', {class: 'small muted'}, rotulo),
        h('div', {class: 'net-lat__value'}, ms(valor)),
        detalhe ? h('div', {class: 'small muted'}, String(detalhe)) : null);
    }

    // A faixa das 24 horas: uma marca por medida, na cor do que ela viu.
    function faixaDoDia() {
      const s = state.status || {};
      const amostras = state.samples || [];
      // "1 queda(s)" é jeito de programador escrever; ninguém fala assim.
      const fora = duracao(s.downtime_24h_seconds);
      const linha = amostras.length
        ? (s.outages_24h
            ? (s.outages_24h === 1
                ? fmtStr('net.day.summary.one', fora)
                : fmtStr('net.day.summary', s.outages_24h, fora))
            : t2('net.day.clean'))
        : t2('net.day.empty');
      return h('div', {class: 'card'},
        h('div', {class: 'card__title'}, t2('net.section.day')),
        h('div', {class: 'net-strip'},
          ...amostras.slice(-240).map((a) => h('span', {
            class: 'net-strip__tick net-strip__tick--' + (a.state || 'ok'),
            title: quando(a.ts) + ' · ' + (a.state || 'ok'),
          }))),
        h('div', {class: 'small muted'}, linha));
    }

    function listaDeQuedas() {
      const quedas = state.outages || [];
      return h('div', {class: 'card'},
        h('div', {class: 'card__title'}, t2('net.section.outages')),
        quedas.length
          ? h('div', {class: 'stack stack--sm'},
              ...quedas.map((q) => h('div', {class: 'net-row'},
                h('span', {class: 'badge badge--' + (q.kind === 'dns' ? 'warn' : 'danger')},
                  t2('net.state.' + q.kind)),
                h('span', {class: 'grow small'}, quando(q.started_at)),
                h('span', {class: 'small muted'},
                  q.ended_at ? duracao(q.seconds) : t2('net.outages.open')))))
          : h('p', {class: 'muted'}, t2('net.outages.empty')));
    }

    function render() {
      view.replaceChildren(cartaoEstado(), faixaDoDia(), listaDeQuedas());
    }

    await carregar();
    render();

    let parado = false;
    const relogio = setInterval(async () => {
      if (parado) return;
      await carregar();
      if (!parado) render();
    }, 20000);

    return () => { parado = true; clearInterval(relogio); };
  },
};
