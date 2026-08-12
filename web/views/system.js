// views/system.js — Advanced mode: the machine at a glance.
//
// One REST fetch paints the page (`/api/system/info` + `/api/system/stats`) and
// after that everything rides the `system.stats` topic, which the shell already
// folds into store.stats. The DOM is built once and then *updated in place*:
// re-rendering seven cards every five seconds is exactly the kind of thing that
// makes a Pi 3B feel slow, and it would fight the user for scroll position in
// the process table.
//
// Rule for every reading on this page: a field that came back null renders as
// "—". A missing sensor is not zero.

import {h, svg, mount, clear} from '../lib/dom.js';
import {icon} from '../lib/icons.js';
import * as fmt from '../lib/format.js';
import * as api from '../lib/api.js';
import {toast} from '../lib/toast.js';
import {t, setStrings} from '../lib/format.js';

setStrings('en', {
  'sys.lead': 'Live readings from this machine. Everything here is measured, nothing is cached.',
  'sys.card.host': 'This machine',
  'sys.host.reading': 'Reading hardware…',
  'sys.host.model': 'Model',
  'sys.host.os': 'Operating system',
  'sys.host.kernel': 'Kernel',
  'sys.host.arch': 'Architecture',
  'sys.host.cpu': 'CPU',
  'sys.host.memory': 'Memory',
  'sys.host.python': 'Python',
  'sys.host.addresses': 'Addresses',
  'sys.host.uptime': 'Uptime',
  'sys.host.load': 'Load',
  'sys.host.pi': 'Raspberry Pi',
  'sys.host.generic': 'Generic host',
  'sys.card.processor': 'Processor',
  'sys.perCore': 'Per core',
  'sys.perCore.none': 'Per-core usage is not reported on this machine.',
  'sys.core': 'Core {n}',
  'sys.card.memory': 'Memory',
  'sys.mem.ram': 'RAM',
  'sys.mem.swap': 'Swap',
  'sys.mem.used': 'Used',
  'sys.mem.available': 'Available',
  'sys.mem.cached': 'Cached',
  'sys.card.thermal': 'Temperature & power',
  'sys.thermal.soc': 'SoC temperature',
  'sys.thermal.scale': 'Warm above 70 °C, throttling near 80 °C',
  'sys.thermal.clean': 'Power and thermals are clean since boot.',
  'sys.thermal.viaVcgencmd': 'Throttling flags come from vcgencmd and are only available on Raspberry Pi OS.',
  'sys.card.storage': 'Storage',
  'sys.storage.none': 'No mounted filesystems reported.',
  'sys.card.network': 'Network',
  'sys.net.down': 'Down',
  'sys.net.up': 'Up',
  'sys.net.sinceBoot': 'Since boot',
  'sys.net.interfaces': 'Interfaces',
  'sys.net.samples': 'last {count} samples',
  'sys.net.none': 'No interfaces reported.',
  'sys.card.processes': 'Top processes',
  'sys.processes.sub': 'Sorted by memory — the resource a 1 GB board runs out of first',
  'sys.processes.col.pid': 'PID',
  'sys.processes.col.name': 'Process',
  'sys.processes.col.user': 'User',
  'sys.processes.col.cpu': 'CPU',
  'sys.processes.col.memory': 'Memory',
  'sys.processes.none': 'No processes reported.',
  'sys.processes.noPsutil': 'This build does not report per-process usage. Installing psutil on the Pi turns this card on.',
  'sys.processes.showing': 'Showing the {shown} hungriest of {total} processes.',
  'sys.processes.count': '{total} processes.',
  'sys.processes.cpuNote': 'Usage since the last sample',
  'sys.error.info': 'Could not read the host description.',
  'sys.error.stats': 'Could not read system stats.',
  'sys.error.page': 'The system page could not load.',
  'sys.throttle.nowPower.title': 'Not enough power right now',
  'sys.throttle.nowPower.body': 'The board is reporting under-voltage. Use the official power supply for this model and a short, thick USB cable — phone chargers and long cables are the usual cause.',
  'sys.throttle.nowCpu.title': 'The CPU is throttled right now',
  'sys.throttle.nowCpu.body': 'Clock speed is being cut to protect the chip, so everything feels slow. Check airflow and the temperature above, or fix the power supply.',
  'sys.throttle.bootPower.title': 'Under-voltage happened since boot',
  'sys.throttle.bootPower.body': 'Power dipped at least once since this machine started. It is fine now, but the supply is marginal and SD-card corruption follows from exactly this.',
  'sys.throttle.bootCpu.title': 'The CPU was throttled since boot',
  'sys.throttle.bootCpu.body': 'The chip hit its limit at least once since boot — heat or power. Worth a heatsink or a better supply if it keeps happening.',
}, {activate: false});

/** Samples of the network counters kept for the throughput sparkline. */
const HISTORY = 60;
/** Rows in the process table. More than this is noise on a 1 GB board. */
const PROCESS_ROWS = 12;
/** Poll interval used only while the event stream is down. */
const POLL_MS = 5000;
/** The static half of the payload changes rarely, but throttling flags live
 *  there and they very much do change. */
const INFO_MS = 60000;

/* --------------------------------------------------------------- reading */

function dig(obj, path, fallback) {
  if (!obj) return fallback;
  let node = obj;
  for (const part of String(path).split('.')) {
    if (node === null || node === undefined || typeof node !== 'object') return fallback;
    node = node[part];
  }
  return node === undefined || node === null ? fallback : node;
}

function pick(obj, keys, fallback) {
  if (!obj || typeof obj !== 'object') return fallback;
  for (const key of keys) {
    if (obj[key] !== undefined && obj[key] !== null) return obj[key];
  }
  return fallback;
}

/** Number or null. Never 0-for-missing, never NaN. */
function num(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const n = Number(value);
  return isFinite(n) ? n : null;
}

function ratio(part, whole) {
  const a = num(part);
  const b = num(whole);
  if (a === null || b === null || b <= 0) return null;
  return (a / b) * 100;
}

/** Normalise `/api/system/stats` (or a `system.stats` event) into one shape. */
export function readSystem(raw) {
  const s = raw && typeof raw === 'object' ? raw : {};
  const cpu = s.cpu && typeof s.cpu === 'object' ? s.cpu : {};
  const mem = s.memory && typeof s.memory === 'object' ? s.memory : (s.mem || {});
  const swap = mem.swap && typeof mem.swap === 'object' ? mem.swap : (s.swap || {});
  const net = s.net && typeof s.net === 'object' ? s.net : (s.network || {});

  let disks = s.disks || s.disk || [];
  if (disks && !Array.isArray(disks)) disks = [disks];

  let perCore = pick(cpu, ['per_core', 'percpu'], null) || s.per_core || s.cpu_per_core || null;
  if (!Array.isArray(perCore)) perCore = [];

  let processes = s.processes || s.top_processes || s.procs || null;
  if (!Array.isArray(processes)) processes = null;

  return {
    ts: s.ts || null,
    cpuPercent: num(pick(cpu, ['percent', 'usage'], typeof s.cpu === 'number' ? s.cpu : s.cpu_percent)),
    perCore: perCore.map(num),
    cpuCount: num(pick(cpu, ['count', 'cores'], null)),
    cpuFreq: num(pick(cpu, ['freq_mhz', 'frequency_mhz'], null)),
    mem: {
      total: num(mem.total),
      used: num(mem.used),
      available: num(pick(mem, ['available', 'free'], null)),
      cached: num(mem.cached),
      percent: num(pick(mem, ['percent', 'used_percent'], ratio(mem.used, mem.total))),
    },
    swap: {
      total: num(swap.total),
      used: num(swap.used),
      percent: num(pick(swap, ['percent'], ratio(swap.used, swap.total))),
    },
    disks: disks.filter(Boolean),
    load: Array.isArray(s.load) ? s.load.map(num) : (Array.isArray(s.load_avg) ? s.load_avg.map(num) : []),
    tempC: num(pick(s, ['temp_c', 'temperature_c', 'temp'], dig(s, 'temperature.cpu', null))),
    uptime: num(pick(s, ['uptime_seconds', 'uptime'], null)),
    processUptime: num(pick(s, ['process_uptime_seconds', 'process_uptime'], null)),
    net: {
      hostname: net.hostname || null,
      interfaces: net.interfaces && typeof net.interfaces === 'object' ? net.interfaces : {},
      rx: num(pick(net, ['rx_bytes', 'rx'], null)),
      tx: num(pick(net, ['tx_bytes', 'tx'], null)),
    },
    throttled: s.throttled && typeof s.throttled === 'object' ? s.throttled : null,
    processes,
  };
}

/** IP addresses, wherever the server chose to put them. */
function addressesFrom(info, stats) {
  const out = [];
  const seen = new Set();

  const add = (value, iface) => {
    if (!value) return;
    let address = value;
    let name = iface || '';
    if (typeof value === 'object') {
      address = pick(value, ['address', 'addr', 'ip', 'ipv4', 'ipv6'], null);
      name = value.interface || value.iface || value.name || name;
    }
    if (!address || typeof address !== 'string') return;
    const clean = address.trim();
    if (!clean || clean === '127.0.0.1' || clean === '::1' || seen.has(clean)) return;
    seen.add(clean);
    out.push({address: clean, iface: name});
  };

  const scanList = (list, iface) => {
    if (Array.isArray(list)) list.forEach((entry) => add(entry, iface));
    else add(list, iface);
  };

  const interfaces = dig(stats, 'net.interfaces', null) || dig(info, 'network.interfaces', null);
  if (interfaces && typeof interfaces === 'object') {
    for (const name of Object.keys(interfaces)) {
      const entry = interfaces[name];
      if (!entry || typeof entry !== 'object') continue;
      scanList(entry.addresses || entry.address || entry.ipv4 || entry.ip || null, name);
    }
  }
  for (const source of [info, stats]) {
    if (!source) continue;
    scanList(source.addresses || source.ips || source.ip_addresses || null, '');
    scanList(dig(source, 'net.addresses', null) || dig(source, 'network.addresses', null), '');
  }
  return out;
}

/* --------------------------------------------------------------- pieces */

function card(options) {
  const {title, sub, iconName, tools, body, footer, variant = ''} = options;
  return h('div', {class: 'card' + (variant ? ' card--' + variant : '')},
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

function kvRow(label, valueNode) {
  return h('div', {class: 'field-row'},
    h('span', {class: 'field-row__label'}, label),
    valueNode,
  );
}

/** A labelled bar that can be updated later through the returned handle. */
function meter(label, options = {}) {
  const {warn = 75, danger = 90} = options;
  const valueEl = h('span', {class: 'field-row__value'}, '—');
  const fillEl = h('div', {class: 'meter__fill', style: {width: '0%'}});
  const barEl = h('div', {
    class: 'meter',
    role: 'progressbar',
    'aria-label': label,
    'aria-valuemin': '0',
    'aria-valuemax': '100',
  }, fillEl);
  const node = h('div', {class: 'stack stack--sm'},
    h('div', {class: 'field-row'},
      h('span', {class: 'field-row__label'}, label),
      valueEl,
    ),
    barEl,
  );
  return {
    node,
    set(percent, text) {
      const value = num(percent);
      valueEl.textContent = text === undefined || text === null ? (value === null ? '—' : fmt.percent(value)) : text;
      valueEl.classList.toggle('faint', value === null);
      const clamped = value === null ? 0 : Math.max(0, Math.min(100, value));
      fillEl.style.width = clamped + '%';
      barEl.classList.toggle('meter--warn', value !== null && value >= warn && value < danger);
      barEl.classList.toggle('meter--danger', value !== null && value >= danger);
      if (value === null) barEl.removeAttribute('aria-valuenow');
      else barEl.setAttribute('aria-valuenow', String(Math.round(value)));
    },
  };
}

/** Circular CPU gauge. Returns {node, set(percent)}. */
function gauge() {
  const R = 42;
  const CIRC = 2 * Math.PI * R;
  const arc = svg('circle', {
    cx: 50, cy: 50, r: R, fill: 'none', stroke: 'var(--accent)', strokeWidth: 9,
    strokeLinecap: 'round', strokeDasharray: CIRC, strokeDashoffset: CIRC,
    transform: 'rotate(-90 50 50)',
  });
  const label = svg('text', {
    x: 50, y: 52, textAnchor: 'middle', fill: 'var(--text)',
    fontSize: 21, fontWeight: 640, 'dominant-baseline': 'middle',
    fontFamily: 'var(--font)',
  }, '—');
  const node = svg('svg', {
    viewBox: '0 0 100 100', width: 124, height: 124, role: 'img',
    'aria-label': 'CPU usage',
  },
    svg('circle', {cx: 50, cy: 50, r: R, fill: 'none', stroke: 'var(--surface-3)', strokeWidth: 9}),
    arc,
    label,
  );
  return {
    node,
    set(percent) {
      const value = num(percent);
      if (value === null) {
        arc.setAttribute('stroke-dashoffset', String(CIRC));
        label.textContent = '—';
        node.setAttribute('aria-label', 'CPU usage unavailable');
        return;
      }
      const clamped = Math.max(0, Math.min(100, value));
      arc.setAttribute('stroke-dashoffset', String(CIRC * (1 - clamped / 100)));
      arc.setAttribute('stroke', clamped >= 90 ? 'var(--danger)' : clamped >= 75 ? 'var(--warn)' : 'var(--accent)');
      label.textContent = fmt.percent(clamped);
      node.setAttribute('aria-label', 'CPU usage ' + fmt.percent(clamped));
    },
  };
}

/** Two-series sparkline drawn from plain numbers. */
function sparkline(width, height) {
  const rx = svg('path', {
    fill: 'none', stroke: 'var(--accent)', strokeWidth: 1.6,
    strokeLinejoin: 'round', strokeLinecap: 'round',
    'vector-effect': 'non-scaling-stroke', d: '',
  });
  const tx = svg('path', {
    fill: 'none', stroke: 'var(--info)', strokeWidth: 1.6,
    strokeLinejoin: 'round', strokeLinecap: 'round',
    strokeDasharray: '3 3', 'vector-effect': 'non-scaling-stroke', d: '',
  });
  const node = svg('svg', {
    viewBox: '0 0 ' + width + ' ' + height,
    preserveAspectRatio: 'none',
    width: '100%', height,
    'aria-hidden': 'true',
    style: {display: 'block', overflow: 'visible'},
  }, rx, tx);

  // `null` é buraco, não zero. Desenhar zero para uma leitura que não existe é
  // a mesma mentira do "0 B/s": a linha encosta no chão e quem olha lê "não
  // passou nada", quando o certo é "não sei". Um buraco parte a linha em dois
  // trechos, que é como um gráfico diz "faltou dado aqui".
  const project = (values, peak) => {
    const step = width / Math.max(1, values.length - 1);
    const trechos = [];
    let atual = [];
    for (let i = 0; i < values.length; i += 1) {
      if (values[i] === null || values[i] === undefined) {
        if (atual.length > 1) trechos.push(atual);
        atual = [];
        continue;
      }
      const y = height - (peak > 0 ? (values[i] / peak) * (height - 2) : 0) - 1;
      atual.push((i * step).toFixed(1) + ',' + y.toFixed(1));
    }
    if (atual.length > 1) trechos.push(atual);
    return trechos;
  };

  return {
    node,
    set(rxValues, txValues) {
      const reais = rxValues.concat(txValues).filter((v) => v !== null && v !== undefined);
      const peak = Math.max(1, ...reais);
      // Um <polyline> só desenha uma linha; com buracos vira um <path> de
      // trechos ("M x,y L ..." por trecho), que é o mesmo desenho quando não há
      // buraco nenhum.
      const desenhar = (alvo, values) => {
        const d = project(values, peak)
          .map((trecho) => 'M' + trecho.join(' L'))
          .join(' ');
        alvo.setAttribute('d', d);
      };
      desenhar(rx, rxValues);
      desenhar(tx, txValues);
    },
  };
}

/* -------------------------------------------------------------- warnings */

/** Raspberry Pi throttling bits, translated into something actionable. */
export function throttleWarnings(throttled) {
  if (!throttled || typeof throttled !== 'object' || !throttled.available) return [];
  const out = [];
  if (throttled.undervoltage_now) {
    out.push({
      level: 'danger',
      title: t('sys.throttle.nowPower.title'),
      body: t('sys.throttle.nowPower.body'),
    });
  }
  if (throttled.throttled_now) {
    out.push({
      level: 'danger',
      title: t('sys.throttle.nowCpu.title'),
      body: t('sys.throttle.nowCpu.body'),
    });
  }
  if (throttled.undervoltage_since_boot && !throttled.undervoltage_now) {
    out.push({
      level: 'warn',
      title: t('sys.throttle.bootPower.title'),
      body: t('sys.throttle.bootPower.body'),
    });
  }
  if (throttled.throttled_since_boot && !throttled.throttled_now) {
    out.push({
      level: 'warn',
      title: t('sys.throttle.bootCpu.title'),
      body: t('sys.throttle.bootCpu.body'),
    });
  }
  return out;
}

function noticeNode(entry) {
  return h('div', {class: 'notice notice--' + (entry.level === 'danger' ? 'error' : entry.level)},
    icon('warning', {size: 18}),
    h('div', {class: 'notice__body'},
      h('span', {class: 'notice__title'}, entry.title),
      h('span', {class: 'small muted'}, entry.body),
    ),
  );
}

/* ------------------------------------------------------------- processes */

function readProcess(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const command = pick(entry, ['name', 'command', 'cmd', 'comm'], null) ||
    (Array.isArray(entry.cmdline) ? entry.cmdline.join(' ') : entry.cmdline) || '';
  return {
    pid: num(pick(entry, ['pid'], null)),
    command: String(command || '').trim(),
    user: pick(entry, ['user', 'username', 'owner'], '') || '',
    cpu: num(pick(entry, ['cpu_percent', 'cpu', 'cpu_pct'], null)),
    memoryBytes: num(pick(entry, ['memory_bytes', 'rss', 'memory', 'rss_bytes'], null)),
    memoryPercent: num(pick(entry, ['memory_percent', 'mem_percent', 'mem_pct'], null)),
  };
}

/* ------------------------------------------------------------------ view */

export default {
  id: 'system',
  get title() { return t('nav.system'); },

  async mount(root, ctx) {
    const store = ctx.store;
    let disposed = false;

    /** Rolling network counters -> bytes/second. */
    const samples = [];
    const rxRates = [];
    const txRates = [];

    let info = null;
    let infoError = null;
    /** null = not asked yet, false = this build has no process endpoint. */
    let processEndpoint = null;
    let processList = null;

    /* ------------------------------------------------------------ chrome */

    const refreshBtn = h('button', {
      class: 'btn btn--sm', type: 'button',
      onClick: () => refreshAll(true),
    }, icon('refresh', {size: 15}), t('action.refresh'));

    const grid = h('div', {class: 'grid grid--wide'});

    mount(root, [
      h('div', {class: 'page__header'},
        h('div', null,
          h('h2', null, t('nav.system')),
          h('p', {class: 'page__lead'}, t('sys.lead')),
        ),
        h('div', {class: 'page__actions'}, refreshBtn),
      ),
      grid,
    ]);

    /* -------------------------------------------------------- host card */

    const hostFields = {
      model: h('span', {class: 'field-row__value'}, '—'),
      os: h('span', {class: 'field-row__value'}, '—'),
      kernel: h('span', {class: 'field-row__value mono tiny'}, '—'),
      arch: h('span', {class: 'field-row__value mono tiny'}, '—'),
      cpu: h('span', {class: 'field-row__value'}, '—'),
      ram: h('span', {class: 'field-row__value'}, '—'),
      python: h('span', {class: 'field-row__value mono tiny'}, '—'),
      version: h('span', {class: 'field-row__value mono tiny'}, '—'),
    };
    const hostTitle = h('div', {class: 'card__title'}, t('sys.card.host'));
    const hostSub = h('div', {class: 'card__sub'}, t('sys.host.reading'));
    const addressList = h('div', {class: 'row', style: {gap: 'var(--space-1)'}}, h('span', {class: 'faint small'}, '—'));
    const uptimeValue = h('span', {class: 'stat__value'}, '—');
    const serviceUptimeValue = h('span', {class: 'stat__value'}, '—');
    const loadValue = h('span', {class: 'stat__value'}, '—');

    const hostCard = h('div', {class: 'card card--span2'},
      h('div', {class: 'card__header'},
        h('span', {class: 'card__icon'}, icon('chip', {size: 18})),
        h('div', {class: 'grow'}, hostTitle, hostSub),
      ),
      h('div', {class: 'card__body'},
        h('div', {class: 'fields'},
          kvRow(t('sys.host.model'), hostFields.model),
          kvRow(t('sys.host.os'), hostFields.os),
          kvRow(t('sys.host.kernel'), hostFields.kernel),
          kvRow(t('sys.host.arch'), hostFields.arch),
          kvRow(t('sys.host.cpu'), hostFields.cpu),
          kvRow(t('sys.host.memory'), hostFields.ram),
          kvRow(t('sys.host.python'), hostFields.python),
          kvRow('project-os', hostFields.version),
        ),
        h('div', {class: 'stack stack--sm'},
          h('span', {class: 'stat__label'}, t('sys.host.addresses')),
          addressList,
        ),
        h('div', {class: 'stats'},
          h('div', {class: 'stat'},
            h('span', {class: 'stat__label'}, t('sys.host.uptime')), uptimeValue),
          h('div', {class: 'stat'},
            h('span', {class: 'stat__label'}, 'project-os up'), serviceUptimeValue),
          h('div', {class: 'stat'},
            h('span', {class: 'stat__label'}, t('sys.host.load')), loadValue,
            h('span', {class: 'stat__hint'}, '1 / 5 / 15 min')),
        ),
      ),
    );

    /* --------------------------------------------------------- cpu card */

    const cpuGauge = gauge();
    const coreHost = h('div', {class: 'stack stack--sm'});
    const cpuSub = h('div', {class: 'card__sub'}, '—');
    const cpuCard = h('div', {class: 'card'},
      h('div', {class: 'card__header'},
        h('span', {class: 'card__icon'}, icon('cpu', {size: 18})),
        h('div', {class: 'grow'}, h('div', {class: 'card__title'}, t('sys.card.processor')), cpuSub),
      ),
      h('div', {class: 'card__body'},
        h('div', {style: {display: 'flex', justifyContent: 'center', padding: '2px 0'}}, cpuGauge.node),
        coreHost,
      ),
    );

    let coreBars = [];
    function ensureCoreBars(count) {
      if (coreBars.length === count) return;
      coreBars = [];
      clear(coreHost);
      if (!count) {
        coreHost.appendChild(h('p', {class: 'small faint'}, t('sys.perCore.none')));
        return;
      }
      coreHost.appendChild(h('span', {class: 'stat__label'}, t('sys.perCore')));
      for (let i = 0; i < count; i += 1) {
        const bar = meter(t('sys.core', {n: i}));
        coreBars.push(bar);
        coreHost.appendChild(bar.node);
      }
    }

    /* ------------------------------------------------------ memory card */

    const memMeter = meter(t('sys.mem.ram'), {warn: 80, danger: 92});
    const swapMeter = meter(t('sys.mem.swap'), {warn: 40, danger: 75});
    const memDetail = h('div', {class: 'fields'});
    const memCard = card({
      // Estava cru: era a única carta desta tela em inglês numa tela em
      // português, e o teste de tradução não pega texto que não passa por t().
      title: t('sys.card.memory'),
      iconName: 'chip',
      body: [memMeter.node, swapMeter.node, memDetail],
    });

    const memFields = {
      used: h('span', {class: 'field-row__value'}, '—'),
      available: h('span', {class: 'field-row__value'}, '—'),
      cached: h('span', {class: 'field-row__value'}, '—'),
      swap: h('span', {class: 'field-row__value'}, '—'),
    };
    mount(memDetail, [
      kvRow(t('sys.mem.used'), memFields.used),
      kvRow(t('sys.mem.available'), memFields.available),
      kvRow(t('sys.mem.cached'), memFields.cached),
      kvRow(t('sys.mem.swap'), memFields.swap),
    ]);

    /* ------------------------------------------------- temperature card */

    const tempValue = h('span', {class: 'stat__value', style: {fontSize: 'var(--text-2xl)'}}, '—');
    const tempHint = h('span', {class: 'stat__hint'}, t('sys.thermal.scale'));
    const warningHost = h('div', {class: 'stack stack--sm'});
    const tempCard = card({
      title: t('sys.card.thermal'),
      iconName: 'thermometer',
      body: [
        h('div', {class: 'stat'}, h('span', {class: 'stat__label'}, t('sys.thermal.soc')), tempValue, tempHint),
        warningHost,
      ],
    });

    /* -------------------------------------------------------- disk card */

    const diskHost = h('div', {class: 'stack stack--sm'});
    const diskCard = card({title: t('sys.card.storage'), iconName: 'disk', body: diskHost});
    let diskMeters = new Map();

    /* ----------------------------------------------------- network card */

    const spark = sparkline(240, 46);
    const netDown = h('span', {class: 'stat__value'}, '—');
    const netUp = h('span', {class: 'stat__value'}, '—');
    const netTotals = h('span', {class: 'stat__hint'}, '—');
    const ifaceHost = h('div', {class: 'stack stack--sm'});
    const netCard = card({
      title: t('sys.card.network'),
      iconName: 'wifi',
      body: [
        h('div', {class: 'stats'},
          h('div', {class: 'stat'}, h('span', {class: 'stat__label'}, t('sys.net.down')), netDown),
          h('div', {class: 'stat'}, h('span', {class: 'stat__label'}, t('sys.net.up')), netUp),
        ),
        h('div', {class: 'stack stack--sm'},
          spark.node,
          h('div', {class: 'row', style: {gap: 'var(--space-3)'}},
            h('span', {class: 'tiny faint'}, '━ ' + t('sys.net.down')),
            h('span', {class: 'tiny faint'}, '┄ ' + t('sys.net.up')),
            h('span', {class: 'tiny faint', style: {marginLeft: 'auto'}},
              t('sys.net.samples', {count: HISTORY})),
          ),
        ),
        h('div', {class: 'stat'}, h('span', {class: 'stat__label'}, t('sys.net.sinceBoot')), netTotals),
        ifaceHost,
      ],
    });

    /* ----------------------------------------------------- process card */

    const processBody = h('tbody');
    const processNote = h('div', {class: 'small faint'}, t('state.loading'));
    const processCard = h('div', {class: 'card card--span2'},
      h('div', {class: 'card__header'},
        h('span', {class: 'card__icon'}, icon('apps', {size: 18})),
        h('div', {class: 'grow'},
          h('div', {class: 'card__title'}, t('sys.card.processes')),
          h('div', {class: 'card__sub'}, t('sys.processes.sub')),
        ),
      ),
      h('div', {class: 'card__body'},
        h('div', {class: 'table-wrap'},
          h('table', {class: 'table'},
            h('thead', null,
              h('tr', null,
                h('th', null, t('sys.processes.col.pid')),
                h('th', null, t('sys.processes.col.name')),
                h('th', null, t('sys.processes.col.user')),
                h('th', {class: 'num'}, t('sys.processes.col.cpu')),
                h('th', {class: 'num'}, t('sys.processes.col.memory')),
              ),
            ),
            processBody,
          ),
        ),
        processNote,
      ),
    );

    mount(grid, [hostCard, cpuCard, memCard, tempCard, diskCard, netCard, processCard]);

    /* ------------------------------------------------------------ paint */

    function paintInfo() {
      if (infoError && !info) {
        hostSub.textContent = infoError;
        hostSub.className = 'card__sub danger';
        return;
      }
      if (!info) return;
      hostSub.className = 'card__sub';

      const board = info.board && typeof info.board === 'object' ? info.board : {};
      const os = info.os && typeof info.os === 'object' ? info.os : {};
      const hostname = info.hostname || dig(info, 'net.hostname', '') || '';

      hostTitle.textContent = hostname || 'This machine';
      const tier = board.tier_note || board.reason || '';
      hostSub.textContent = tier || (board.is_raspberry_pi ? t('sys.host.pi') : t('sys.host.generic'));

      hostFields.model.textContent = board.model || info.model || '—';
      hostFields.os.textContent = os.name || info.os_name || '—';
      hostFields.kernel.textContent = info.kernel || '—';
      hostFields.arch.textContent = info.arch || board.arch || '—';
      hostFields.python.textContent = info.python || '—';

      const version = info.version || dig(ctx.health, 'version', '') || dig(store.get('health'), 'version', '');
      hostFields.version.textContent = version || '—';

      const soc = board.soc || '';
      const cores = num(board.cores) || num(dig(info, 'cpu.count', null));
      hostFields.cpu.textContent = [soc, cores ? cores + ' cores' : ''].filter(Boolean).join(' · ') || '—';

      const ramMb = num(board.ram_mb) || num(board.ram_total_mb);
      hostFields.ram.textContent = ramMb === null ? '—' : fmt.bytes(ramMb * 1024 * 1024);
    }

    function paintAddresses(stats) {
      const list = addressesFrom(info, stats);
      if (!list.length) {
        mount(addressList, h('span', {class: 'faint small'}, '—'));
        return;
      }
      mount(addressList, list.slice(0, 6).map((entry) =>
        h('span', {class: 'tag mono'}, entry.iface ? entry.iface + ' ' + entry.address : entry.address)));
    }

    function paintDisks(disks) {
      const keys = [];
      for (const disk of disks) {
        const mountpoint = disk.mountpoint || disk.mount || disk.path || disk.device || '?';
        keys.push(mountpoint);
      }
      // Rebuild only when the set of mounts changed (a USB stick appearing).
      const signature = keys.join('|');
      if (diskHost.dataset.signature !== signature) {
        diskHost.dataset.signature = signature;
        diskMeters = new Map();
        clear(diskHost);
        if (!disks.length) {
          diskHost.appendChild(h('p', {class: 'small faint'}, t('sys.storage.none')));
        }
        for (const key of keys) {
          const bar = meter(key, {warn: 85, danger: 93});
          diskMeters.set(key, bar);
          diskHost.appendChild(bar.node);
        }
      }
      for (const disk of disks) {
        const key = disk.mountpoint || disk.mount || disk.path || disk.device || '?';
        const bar = diskMeters.get(key);
        if (!bar) continue;
        const percent = num(disk.percent) === null ? ratio(disk.used, disk.total) : num(disk.percent);
        const used = num(disk.used);
        const total = num(disk.total);
        const text = used === null || total === null
          ? (percent === null ? '—' : fmt.percent(percent))
          : fmt.bytes(used) + ' / ' + fmt.bytes(total);
        bar.set(percent, text);
      }
    }

    function paintNetwork(net) {
      const now = Date.now();
      if (net.rx !== null || net.tx !== null) {
        const previous = samples[samples.length - 1];
        samples.push({t: now, rx: net.rx, tx: net.tx});
        if (samples.length > HISTORY + 1) samples.shift();
        if (previous) {
          const seconds = (now - previous.t) / 1000;
          // A counter that went backwards means the interface reset; a rate of
          // zero is a truthful reading of "no idea", not a lie about traffic.
          // Sem contador dos dois lados não existe taxa. Devolver 0 aqui era
          // inventar "não passou nada" -- e como rx e tx são independentes,
          // uma placa que só reporta um dos dois enchia o outro de zeros.
          const delta = (a, b) => {
            if (a === null || b === null || seconds <= 0) return null;
            const diff = a - b;
            return diff < 0 ? 0 : diff / seconds;
          };
          rxRates.push(delta(net.rx, previous.rx));
          txRates.push(delta(net.tx, previous.tx));
          while (rxRates.length > HISTORY) rxRates.shift();
          while (txRates.length > HISTORY) txRates.shift();
        }
      }

      const ultima = (lista) => {
        const valor = lista.length ? lista[lista.length - 1] : null;
        return valor === null || valor === undefined ? '—' : fmt.bytes(valor) + '/s';
      };
      netDown.textContent = ultima(rxRates);
      netUp.textContent = ultima(txRates);
      netTotals.textContent = net.rx === null && net.tx === null
        ? '—'
        : fmt.bytes(net.rx) + ' in · ' + fmt.bytes(net.tx) + ' out';
      spark.set(rxRates, txRates);

      const names = Object.keys(net.interfaces || {});
      mount(ifaceHost, names.length
        ? [
            h('span', {class: 'stat__label'}, t('sys.net.interfaces')),
            h('div', {class: 'fields'}, names.map((name) => {
              const entry = net.interfaces[name] || {};
              const rx = num(entry.rx_bytes);
              const tx = num(entry.tx_bytes);
              return kvRow(name, h('span', {class: 'field-row__value tiny'},
                (rx === null ? '—' : fmt.bytes(rx)) + ' / ' + (tx === null ? '—' : fmt.bytes(tx))));
            })),
          ]
        : h('p', {class: 'small faint'}, t('sys.net.none')));
    }

    function paintProcesses(list) {
      clear(processBody);
      if (!Array.isArray(list)) {
        processNote.textContent = processEndpoint === false
          ? t('sys.processes.noPsutil')
          : t('state.loading');
        processNote.className = 'small faint';
        return;
      }
      const rows = list.map(readProcess).filter(Boolean);
      rows.sort((a, b) => {
        const am = a.memoryBytes === null ? (a.memoryPercent || 0) : a.memoryBytes;
        const bm = b.memoryBytes === null ? (b.memoryPercent || 0) : b.memoryBytes;
        return bm - am;
      });
      if (!rows.length) {
        processNote.textContent = t('sys.processes.none');
        processNote.className = 'small faint';
        return;
      }
      for (const row of rows.slice(0, PROCESS_ROWS)) {
        processBody.appendChild(h('tr', null,
          h('td', {class: 'mono tiny'}, row.pid === null ? '—' : String(row.pid)),
          h('td', {class: 'truncate', title: row.command}, fmt.truncate(row.command || '—', 52)),
          h('td', {class: 'tiny muted'}, row.user || '—'),
          h('td', {class: 'num'}, row.cpu === null ? '—' : fmt.percent(row.cpu, {digits: 1})),
          h('td', {class: 'num'},
            row.memoryBytes === null
              ? (row.memoryPercent === null ? '—' : fmt.percent(row.memoryPercent, {digits: 1}))
              : fmt.bytes(row.memoryBytes) +
                (row.memoryPercent === null ? '' : ' · ' + fmt.percent(row.memoryPercent, {digits: 1}))),
        ));
      }
      processNote.textContent = rows.length > PROCESS_ROWS
        ? t('sys.processes.showing', {shown: PROCESS_ROWS, total: rows.length})
        : t('sys.processes.count', {total: rows.length});
      processNote.className = 'small faint';
    }

    function applyStats(raw) {
      const s = readSystem(raw);

      cpuGauge.set(s.cpuPercent);
      ensureCoreBars(s.perCore.length);
      for (let i = 0; i < coreBars.length; i += 1) coreBars[i].set(s.perCore[i]);
      const freq = s.cpuFreq === null ? '' : fmt.number(s.cpuFreq) + ' MHz';
      const count = s.cpuCount === null ? '' : s.cpuCount + ' cores';
      cpuSub.textContent = [count, freq].filter(Boolean).join(' · ') || t('sys.processes.cpuNote');

      memMeter.set(s.mem.percent, s.mem.total === null || s.mem.used === null
        ? null
        : fmt.bytes(s.mem.used) + ' / ' + fmt.bytes(s.mem.total));
      swapMeter.set(s.swap.total ? s.swap.percent : null,
        !s.swap.total ? 'none configured' : fmt.bytes(s.swap.used) + ' / ' + fmt.bytes(s.swap.total));
      memFields.used.textContent = s.mem.used === null ? '—' : fmt.bytes(s.mem.used);
      memFields.available.textContent = s.mem.available === null ? '—' : fmt.bytes(s.mem.available);
      memFields.cached.textContent = s.mem.cached === null ? '—' : fmt.bytes(s.mem.cached);
      memFields.swap.textContent = s.swap.total ? fmt.bytes(s.swap.used) : '—';

      tempValue.textContent = s.tempC === null ? '—' : fmt.temperature(s.tempC, {digits: 1});
      tempValue.classList.toggle('danger', s.tempC !== null && s.tempC >= 80);
      tempValue.classList.toggle('warn', s.tempC !== null && s.tempC >= 70 && s.tempC < 80);

      const warnings = throttleWarnings(s.throttled || dig(info, 'throttled', null));
      if (warnings.length) mount(warningHost, warnings.map(noticeNode));
      else if (dig(info, 'throttled.available', false)) {
        mount(warningHost, h('div', {class: 'notice'},
          icon('check', {size: 18}),
          h('div', {class: 'notice__body'}, h('span', {class: 'small muted'}, t('sys.thermal.clean')))));
      } else {
        mount(warningHost, h('p', {class: 'small faint'},
          t('sys.thermal.viaVcgencmd')));
      }

      paintDisks(s.disks);
      paintNetwork(s.net);
      paintAddresses(raw);

      uptimeValue.textContent = s.uptime === null ? '—' : fmt.uptime(s.uptime);
      serviceUptimeValue.textContent = s.processUptime === null ? '—' : fmt.uptime(s.processUptime);
      loadValue.textContent = s.load.length
        ? s.load.slice(0, 3).map((v) => (v === null ? '—' : v.toFixed(2))).join(' / ')
        : '—';

      if (Array.isArray(s.processes)) {
        processEndpoint = false; // the stats payload carries them; no extra call
        processList = s.processes;
        paintProcesses(processList);
      }
    }

    /* ------------------------------------------------------------ loads */

    async function loadInfo() {
      try {
        const raw = await api.get('/system/info');
        if (disposed) return;
        info = raw && raw.info && typeof raw.info === 'object' ? raw.info : raw;
        infoError = null;
      } catch (err) {
        if (disposed) return;
        infoError = (err && err.message) || t('sys.error.info');
      }
      paintInfo();
      if (store.get('stats')) applyStats(store.get('stats'));
    }

    async function loadStats() {
      try {
        const raw = await api.get('/system/stats');
        if (disposed) return;
        store.set({stats: raw});
        applyStats(raw);
      } catch (err) {
        if (disposed) return;
        hostSub.textContent = (err && err.message) || t('sys.error.stats');
        hostSub.className = 'card__sub danger';
      }
    }

    async function loadProcesses() {
      if (processEndpoint === false) return;
      try {
        const raw = await api.get('/system/processes', {redirectOnAuth: false});
        if (disposed) return;
        const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.processes) ? raw.processes : null);
        if (!list) {
          processEndpoint = false;
          paintProcesses(null);
          return;
        }
        processEndpoint = true;
        processList = list;
        paintProcesses(list);
      } catch (err) {
        if (disposed) return;
        // Not every build exposes this; asking once is enough.
        processEndpoint = false;
        paintProcesses(null);
      }
    }

    function refreshAll(manual) {
      if (manual) {
        refreshBtn.disabled = true;
        setTimeout(() => { if (!disposed) refreshBtn.disabled = false; }, 800);
        processEndpoint = processEndpoint === true ? true : null;
      }
      return Promise.all([loadInfo(), loadStats(), loadProcesses()]);
    }

    /* ------------------------------------------------------ live wiring */

    const cached = store.get('stats');
    if (cached) applyStats(cached);
    paintProcesses(processList);

    const offStore = store.subscribe((next, prev) => {
      if (disposed) return;
      if (!Object.is(next.stats, prev.stats)) applyStats(next.stats);
    });

    // The event stream is the primary feed; polling exists only for the case
    // where it is down, and stops the moment it comes back.
    const poll = setInterval(() => {
      if (disposed) return;
      const live = ctx.ws && typeof ctx.ws.isOpen === 'function' && ctx.ws.isOpen();
      if (!live) loadStats();
    }, POLL_MS);

    const infoTimer = setInterval(() => {
      if (!disposed) loadInfo();
    }, INFO_MS);

    const processTimer = setInterval(() => {
      if (!disposed && processEndpoint === true) loadProcesses();
    }, 10000);

    refreshAll(false).catch((err) => {
      if (!disposed) toast((err && err.message) || t('sys.error.page'), {type: 'error'});
    });

    return () => {
      disposed = true;
      offStore();
      clearInterval(poll);
      clearInterval(infoTimer);
      clearInterval(processTimer);
    };
  },
};
