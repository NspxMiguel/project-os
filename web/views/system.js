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
  const rx = svg('polyline', {
    fill: 'none', stroke: 'var(--accent)', strokeWidth: 1.6,
    strokeLinejoin: 'round', strokeLinecap: 'round',
    'vector-effect': 'non-scaling-stroke', points: '',
  });
  const tx = svg('polyline', {
    fill: 'none', stroke: 'var(--info)', strokeWidth: 1.6,
    strokeLinejoin: 'round', strokeLinecap: 'round',
    strokeDasharray: '3 3', 'vector-effect': 'non-scaling-stroke', points: '',
  });
  const node = svg('svg', {
    viewBox: '0 0 ' + width + ' ' + height,
    preserveAspectRatio: 'none',
    width: '100%', height,
    'aria-hidden': 'true',
    style: {display: 'block', overflow: 'visible'},
  }, rx, tx);

  const project = (values, peak) => {
    if (values.length < 2) return '';
    const step = width / (values.length - 1);
    const points = [];
    for (let i = 0; i < values.length; i += 1) {
      const value = values[i] === null ? 0 : values[i];
      const y = height - (peak > 0 ? (value / peak) * (height - 2) : 0) - 1;
      points.push((i * step).toFixed(1) + ',' + y.toFixed(1));
    }
    return points.join(' ');
  };

  return {
    node,
    set(rxValues, txValues) {
      const peak = Math.max(1, ...rxValues.map((v) => v || 0), ...txValues.map((v) => v || 0));
      rx.setAttribute('points', project(rxValues, peak));
      tx.setAttribute('points', project(txValues, peak));
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
      title: 'Not enough power right now',
      body: 'The board is reporting under-voltage. Use the official power supply for this ' +
        'model and a short, thick USB cable — phone chargers and long cables are the usual cause.',
    });
  }
  if (throttled.throttled_now) {
    out.push({
      level: 'danger',
      title: 'The CPU is throttled right now',
      body: 'Clock speed is being cut to protect the chip, so everything feels slow. ' +
        'Check airflow and the temperature above, or fix the power supply.',
    });
  }
  if (throttled.undervoltage_since_boot && !throttled.undervoltage_now) {
    out.push({
      level: 'warn',
      title: 'Under-voltage happened since boot',
      body: 'Power dipped at least once since this machine started. It is fine now, but the ' +
        'supply is marginal and SD-card corruption follows from exactly this.',
    });
  }
  if (throttled.throttled_since_boot && !throttled.throttled_now) {
    out.push({
      level: 'warn',
      title: 'The CPU was throttled since boot',
      body: 'The chip hit its limit at least once since boot — heat or power. Worth a heatsink ' +
        'or a better supply if it keeps happening.',
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
  title: 'System',

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
    }, icon('refresh', {size: 15}), 'Refresh');

    const grid = h('div', {class: 'grid grid--wide'});

    mount(root, [
      h('div', {class: 'page__header'},
        h('div', null,
          h('h2', null, 'System'),
          h('p', {class: 'page__lead'}, 'Live readings from this machine. Everything here is measured, nothing is cached.'),
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
    const hostTitle = h('div', {class: 'card__title'}, 'This machine');
    const hostSub = h('div', {class: 'card__sub'}, 'Reading hardware…');
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
          kvRow('Model', hostFields.model),
          kvRow('Operating system', hostFields.os),
          kvRow('Kernel', hostFields.kernel),
          kvRow('Architecture', hostFields.arch),
          kvRow('CPU', hostFields.cpu),
          kvRow('Memory', hostFields.ram),
          kvRow('Python', hostFields.python),
          kvRow('ProjectOS', hostFields.version),
        ),
        h('div', {class: 'stack stack--sm'},
          h('span', {class: 'stat__label'}, 'Addresses'),
          addressList,
        ),
        h('div', {class: 'stats'},
          h('div', {class: 'stat'},
            h('span', {class: 'stat__label'}, 'Uptime'), uptimeValue),
          h('div', {class: 'stat'},
            h('span', {class: 'stat__label'}, 'ProjectOS up'), serviceUptimeValue),
          h('div', {class: 'stat'},
            h('span', {class: 'stat__label'}, 'Load'), loadValue,
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
        h('div', {class: 'grow'}, h('div', {class: 'card__title'}, 'Processor'), cpuSub),
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
        coreHost.appendChild(h('p', {class: 'small faint'}, 'Per-core usage is not reported on this machine.'));
        return;
      }
      coreHost.appendChild(h('span', {class: 'stat__label'}, 'Per core'));
      for (let i = 0; i < count; i += 1) {
        const bar = meter('Core ' + i);
        coreBars.push(bar);
        coreHost.appendChild(bar.node);
      }
    }

    /* ------------------------------------------------------ memory card */

    const memMeter = meter('RAM', {warn: 80, danger: 92});
    const swapMeter = meter('Swap', {warn: 40, danger: 75});
    const memDetail = h('div', {class: 'fields'});
    const memCard = card({
      title: 'Memory',
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
      kvRow('Used', memFields.used),
      kvRow('Available', memFields.available),
      kvRow('Cached', memFields.cached),
      kvRow('Swap', memFields.swap),
    ]);

    /* ------------------------------------------------- temperature card */

    const tempValue = h('span', {class: 'stat__value', style: {fontSize: 'var(--text-2xl)'}}, '—');
    const tempHint = h('span', {class: 'stat__hint'}, 'Warm above 70 °C, throttling near 80 °C');
    const warningHost = h('div', {class: 'stack stack--sm'});
    const tempCard = card({
      title: 'Temperature & power',
      iconName: 'thermometer',
      body: [
        h('div', {class: 'stat'}, h('span', {class: 'stat__label'}, 'SoC temperature'), tempValue, tempHint),
        warningHost,
      ],
    });

    /* -------------------------------------------------------- disk card */

    const diskHost = h('div', {class: 'stack stack--sm'});
    const diskCard = card({title: 'Storage', iconName: 'disk', body: diskHost});
    let diskMeters = new Map();

    /* ----------------------------------------------------- network card */

    const spark = sparkline(240, 46);
    const netDown = h('span', {class: 'stat__value'}, '—');
    const netUp = h('span', {class: 'stat__value'}, '—');
    const netTotals = h('span', {class: 'stat__hint'}, '—');
    const ifaceHost = h('div', {class: 'stack stack--sm'});
    const netCard = card({
      title: 'Network',
      iconName: 'wifi',
      body: [
        h('div', {class: 'stats'},
          h('div', {class: 'stat'}, h('span', {class: 'stat__label'}, 'Down'), netDown),
          h('div', {class: 'stat'}, h('span', {class: 'stat__label'}, 'Up'), netUp),
        ),
        h('div', {class: 'stack stack--sm'},
          spark.node,
          h('div', {class: 'row', style: {gap: 'var(--space-3)'}},
            h('span', {class: 'tiny faint'}, '━ down'),
            h('span', {class: 'tiny faint'}, '┄ up'),
            h('span', {class: 'tiny faint', style: {marginLeft: 'auto'}}, 'last ' + HISTORY + ' samples'),
          ),
        ),
        h('div', {class: 'stat'}, h('span', {class: 'stat__label'}, 'Since boot'), netTotals),
        ifaceHost,
      ],
    });

    /* ----------------------------------------------------- process card */

    const processBody = h('tbody');
    const processNote = h('div', {class: 'small faint'}, 'Loading…');
    const processCard = h('div', {class: 'card card--span2'},
      h('div', {class: 'card__header'},
        h('span', {class: 'card__icon'}, icon('apps', {size: 18})),
        h('div', {class: 'grow'},
          h('div', {class: 'card__title'}, 'Top processes'),
          h('div', {class: 'card__sub'}, 'Sorted by memory — the resource a 1 GB board runs out of first'),
        ),
      ),
      h('div', {class: 'card__body'},
        h('div', {class: 'table-wrap'},
          h('table', {class: 'table'},
            h('thead', null,
              h('tr', null,
                h('th', null, 'PID'),
                h('th', null, 'Process'),
                h('th', null, 'User'),
                h('th', {class: 'num'}, 'CPU'),
                h('th', {class: 'num'}, 'Memory'),
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
      hostSub.textContent = tier || (board.is_raspberry_pi ? 'Raspberry Pi' : 'Generic host');

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
          diskHost.appendChild(h('p', {class: 'small faint'}, 'No mounted filesystems reported.'));
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
          const delta = (a, b) => {
            if (a === null || b === null || seconds <= 0) return 0;
            const diff = a - b;
            return diff < 0 ? 0 : diff / seconds;
          };
          rxRates.push(delta(net.rx, previous.rx));
          txRates.push(delta(net.tx, previous.tx));
          while (rxRates.length > HISTORY) rxRates.shift();
          while (txRates.length > HISTORY) txRates.shift();
        }
      }

      netDown.textContent = rxRates.length ? fmt.bytes(rxRates[rxRates.length - 1]) + '/s' : '—';
      netUp.textContent = txRates.length ? fmt.bytes(txRates[txRates.length - 1]) + '/s' : '—';
      netTotals.textContent = net.rx === null && net.tx === null
        ? '—'
        : fmt.bytes(net.rx) + ' in · ' + fmt.bytes(net.tx) + ' out';
      spark.set(rxRates, txRates);

      const names = Object.keys(net.interfaces || {});
      mount(ifaceHost, names.length
        ? [
            h('span', {class: 'stat__label'}, 'Interfaces'),
            h('div', {class: 'fields'}, names.map((name) => {
              const entry = net.interfaces[name] || {};
              const rx = num(entry.rx_bytes);
              const tx = num(entry.tx_bytes);
              return kvRow(name, h('span', {class: 'field-row__value tiny'},
                (rx === null ? '—' : fmt.bytes(rx)) + ' / ' + (tx === null ? '—' : fmt.bytes(tx))));
            })),
          ]
        : h('p', {class: 'small faint'}, 'No interfaces reported.'));
    }

    function paintProcesses(list) {
      clear(processBody);
      if (!Array.isArray(list)) {
        processNote.textContent = processEndpoint === false
          ? 'This build does not report per-process usage. Installing psutil on the Pi ' +
            '(pip install psutil) gives the server what it needs.'
          : 'Loading…';
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
        processNote.textContent = 'No processes reported.';
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
        ? 'Showing the ' + PROCESS_ROWS + ' hungriest of ' + rows.length + ' processes.'
        : rows.length + ' processes.';
      processNote.className = 'small faint';
    }

    function applyStats(raw) {
      const s = readSystem(raw);

      cpuGauge.set(s.cpuPercent);
      ensureCoreBars(s.perCore.length);
      for (let i = 0; i < coreBars.length; i += 1) coreBars[i].set(s.perCore[i]);
      const freq = s.cpuFreq === null ? '' : fmt.number(s.cpuFreq) + ' MHz';
      const count = s.cpuCount === null ? '' : s.cpuCount + ' cores';
      cpuSub.textContent = [count, freq].filter(Boolean).join(' · ') || 'Usage since the last sample';

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
          h('div', {class: 'notice__body'}, h('span', {class: 'small muted'}, 'Power and thermals are clean since boot.'))));
      } else {
        mount(warningHost, h('p', {class: 'small faint'},
          'Throttling flags come from vcgencmd and are only available on Raspberry Pi OS.'));
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
        infoError = (err && err.message) || 'Could not read the host description.';
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
        hostSub.textContent = (err && err.message) || 'Could not read system stats.';
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
      if (!disposed) toast((err && err.message) || 'The system page could not load.', {type: 'error'});
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
