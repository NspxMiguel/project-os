// format.js — display helpers + a one-table i18n seam.
//
// t() is deliberately dumb: a flat string table, English by default, with
// {placeholder} interpolation. Every user-visible string in the shell goes
// through it, so translating ProjectOS later means adding a table and calling
// setStrings() — not rewriting views.

const STRINGS = {
  en: {
    'app.name': 'ProjectOS',
    'nav.dashboard': 'Dashboard',
    'nav.apps': 'Apps',
    'nav.devices': 'Devices',
    'nav.settings': 'Settings',
    'nav.system': 'System',
    'nav.services': 'Services',
    'nav.files': 'Files',
    'nav.terminal': 'Terminal',
    'nav.logs': 'Logs',
    // Kept in Portuguese on purpose: "helper" reads as a support role, and this
    // is the word the whole subsystem uses, down to the API and the agents.
    'nav.helpers': 'Ajudantes',
    'nav.tuning': 'Hardware',
    'nav.packages': 'Software',
    'nav.updates': 'Updates',
    'nav.store': 'App store',
    'mode.simple': 'Simple',
    'mode.advanced': 'Advanced',
    'mode.label': 'Interface mode',
    'theme.dark': 'Dark',
    'theme.light': 'Light',
    'theme.auto': 'Auto',
    'action.apply': 'Apply',
    'action.dismiss': 'Dismiss',
    'action.retry': 'Retry',
    'action.refresh': 'Refresh',
    'action.cancel': 'Cancel',
    'action.confirm': 'Confirm',
    'action.save': 'Save',
    'action.close': 'Close',
    'action.logout': 'Sign out',
    'action.back': 'Back',
    'action.continue': 'Continue',
    'state.loading': 'Loading…',
    'state.empty': 'Nothing here yet',
    'state.error': 'Something went wrong',
    'state.offline': 'Disconnected',
    'state.connecting': 'Connecting…',
    'state.live': 'Live',
    'time.now': 'just now',
    'unit.cpu': 'CPU',
    'unit.ram': 'RAM',
    'unit.temp': 'Temp',
    'unit.disk': 'Disk',
    'unit.uptime': 'Uptime',
  },
};

let locale = 'en';
let table = STRINGS.en;

/** Register (or extend) a string table and optionally switch to it. */
export function setStrings(code, strings, {activate = true} = {}) {
  STRINGS[code] = Object.assign({}, STRINGS[code] || {}, strings || {});
  if (activate) setLocale(code);
}

export function setLocale(code) {
  locale = STRINGS[code] ? code : 'en';
  table = STRINGS[locale];
  return locale;
}

export function getLocale() {
  return locale;
}

/** t('nav.apps') -> 'Apps'; t('greet', {name: 'Miguel'}) interpolates {name}. */
export function t(key, vars) {
  let out = table[key];
  if (out === undefined) out = STRINGS.en[key];
  if (out === undefined) out = key;
  if (vars) {
    out = out.replace(/\{(\w+)\}/g, (match, name) =>
      vars[name] === undefined || vars[name] === null ? match : String(vars[name]));
  }
  return out;
}

function round(value, digits) {
  const factor = Math.pow(10, digits);
  return Math.round(value * factor) / factor;
}

/** Number(), except that null / undefined / "" are NaN rather than 0 — a
 *  missing field must read as "—", not as a confident zero. */
function numeric(value) {
  if (value === null || value === undefined) return NaN;
  if (typeof value === 'string' && value.trim() === '') return NaN;
  if (typeof value === 'boolean') return NaN;
  return Number(value);
}

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];

/** bytes(1536) -> "1.5 KB". Binary steps, decimal-ish labels (what SD cards use). */
export function bytes(value, {digits = null, space = ' '} = {}) {
  const num = numeric(value);
  if (!isFinite(num)) return '—';
  const sign = num < 0 ? '-' : '';
  let n = Math.abs(num);
  let unit = 0;
  while (n >= 1024 && unit < BYTE_UNITS.length - 1) {
    n /= 1024;
    unit += 1;
  }
  const precision = digits === null ? (unit === 0 ? 0 : n < 10 ? 1 : n < 100 ? 1 : 0) : digits;
  return sign + round(n, precision).toFixed(precision) + space + BYTE_UNITS[unit];
}

/** duration(3725) -> "1:02:05"; duration(65) -> "1:05". For track times. */
export function duration(seconds, {forceHours = false} = {}) {
  const total = numeric(seconds);
  if (!isFinite(total) || total < 0) return '--:--';
  const whole = Math.floor(total);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  const pad = (n) => String(n).padStart(2, '0');
  if (h || forceHours) return h + ':' + pad(m) + ':' + pad(s);
  return m + ':' + pad(s);
}

/** uptime(93784) -> "1d 2h 3m". Coarse on purpose. */
export function uptime(seconds) {
  const total = numeric(seconds);
  if (!isFinite(total) || total < 0) return '—';
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  if (m) return m + 'm';
  return Math.floor(total) + 's';
}

function toDate(value) {
  if (value === null || value === undefined || value === '') return null;
  if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
  if (typeof value === 'number') {
    // Accept both seconds and milliseconds since epoch.
    const ms = value > 1e12 ? value : value * 1000;
    const date = new Date(ms);
    return isNaN(date.getTime()) ? null : date;
  }
  let raw = String(value);
  // SQLite/ISO timestamps without a zone are UTC on the server side.
  if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(raw)) raw = raw.replace(' ', 'T') + 'Z';
  const date = new Date(raw);
  return isNaN(date.getTime()) ? null : date;
}

const RTF = (() => {
  try {
    return new Intl.RelativeTimeFormat(undefined, {numeric: 'auto'});
  } catch (err) {
    return null;
  }
})();

const REL_STEPS = [
  ['second', 60],
  ['minute', 60],
  ['hour', 24],
  ['day', 7],
  ['week', 4.345],
  ['month', 12],
  ['year', Infinity],
];

/** relativeTime('2026-08-08T10:00:00Z') -> "12 minutes ago". */
export function relativeTime(value, {now = Date.now()} = {}) {
  const date = toDate(value);
  if (!date) return '—';
  let delta = (date.getTime() - now) / 1000;
  if (Math.abs(delta) < 45) return t('time.now');
  for (const [unit, span] of REL_STEPS) {
    if (Math.abs(delta) < span || span === Infinity) {
      const rounded = Math.round(delta);
      if (RTF) return RTF.format(rounded, unit);
      const abs = Math.abs(rounded);
      const label = abs === 1 ? unit : unit + 's';
      return rounded < 0 ? abs + ' ' + label + ' ago' : 'in ' + abs + ' ' + label;
    }
    delta /= span;
  }
  return date.toLocaleString();
}

/** Absolute local timestamp, short form. */
export function dateTime(value, {withSeconds = false} = {}) {
  const date = toDate(value);
  if (!date) return '—';
  const opts = {year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'};
  if (withSeconds) opts.second = '2-digit';
  try {
    return date.toLocaleString(undefined, opts);
  } catch (err) {
    return date.toISOString();
  }
}

export function clockTime(value) {
  const date = toDate(value);
  if (!date) return '—';
  try {
    return date.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
  } catch (err) {
    return date.toISOString().slice(11, 16);
  }
}

/** percent(42.4) -> "42%". Values are 0-100 by default; pass {fraction: true}
 *  for 0-1 inputs (ratios coming out of the recommender). */
export function percent(value, {digits = 0, fraction = false, space = ''} = {}) {
  const num = numeric(value);
  if (!isFinite(num)) return '—';
  const pct = fraction ? num * 100 : num;
  return round(pct, digits).toFixed(digits) + space + '%';
}

/** temperature(48.35) -> "48.4 °C"; {unit: 'F'} converts. */
export function temperature(celsius, {digits = 1, unit = 'C', space = ' '} = {}) {
  const num = numeric(celsius);
  if (!isFinite(num)) return '—';
  if (unit === 'F') return round(num * 9 / 5 + 32, digits).toFixed(digits) + space + '°F';
  return round(num, digits).toFixed(digits) + space + '°C';
}

/** Human label from an identifier: "quiet_hours" -> "Quiet hours",
 *  "maxVolume" -> "Max volume", "cpu_percent" -> "CPU percent". Sentence case,
 *  so a key an app invented reads like a label and not like a headline. */
const ACRONYMS = {cpu: 'CPU', gpu: 'GPU', ram: 'RAM', ip: 'IP', id: 'ID', url: 'URL',
  api: 'API', os: 'OS', ui: 'UI', ssid: 'SSID', mac: 'MAC', db: 'DB', tv: 'TV'};

export function humanize(value) {
  const raw = String(value === null || value === undefined ? '' : value);
  if (!raw) return '';
  const words = raw
    .replace(/[_.\-/]+/g, ' ')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ');
  return words
    .map((word, index) => {
      const known = ACRONYMS[word.toLowerCase()];
      if (known) return known;
      if (/^[A-Z0-9]{2,}$/.test(word)) return word; // already an acronym
      const lower = word.charAt(0).toLowerCase() + word.slice(1);
      return index === 0 ? lower.charAt(0).toUpperCase() + lower.slice(1) : lower;
    })
    .join(' ');
}

/** Truncate for a card without breaking the layout. */
export function truncate(value, max = 60) {
  const raw = String(value === null || value === undefined ? '' : value);
  return raw.length > max ? raw.slice(0, Math.max(1, max - 1)) + '…' : raw;
}

/** Format a number with locale separators. */
export function number(value, {digits = 0} = {}) {
  const num = numeric(value);
  if (!isFinite(num)) return '—';
  try {
    return num.toLocaleString(undefined, {minimumFractionDigits: digits, maximumFractionDigits: digits});
  } catch (err) {
    return round(num, digits).toFixed(digits);
  }
}

export default {
  t, setStrings, setLocale, getLocale,
  bytes, duration, uptime, relativeTime, dateTime, clockTime,
  percent, temperature, humanize, truncate, number,
};
