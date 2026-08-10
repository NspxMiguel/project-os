// icons.js — the whole icon set, inline, no network, no sprite file.
//
// Each entry is a list of shapes. A string is shorthand for <path d="...">;
// anything else is [tag, attrs]. Everything is drawn on a 24x24 grid with
// currentColor strokes, so an icon inherits text colour and size context.
//
//   nav.appendChild(icon('devices', {size: 20}));

import {svg} from './dom.js';

export const ICONS = {
  dashboard: [
    ['rect', {x: 3, y: 3, width: 7.5, height: 7.5, rx: 1.8}],
    ['rect', {x: 13.5, y: 3, width: 7.5, height: 4.5, rx: 1.5}],
    ['rect', {x: 13.5, y: 10.5, width: 7.5, height: 10.5, rx: 1.8}],
    ['rect', {x: 3, y: 13.5, width: 7.5, height: 7.5, rx: 1.8}],
  ],
  apps: [
    'M21 8v8a2 2 0 0 1-1 1.73l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.73l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8z',
    'M3.4 7.1 12 12l8.6-4.9',
    'M12 22V12',
  ],
  devices: [
    ['rect', {x: 2, y: 4.5, width: 13, height: 9.5, rx: 2}],
    'M6 18h6',
    ['rect', {x: 16.5, y: 9, width: 5.5, height: 11, rx: 1.6}],
  ],
  settings: [
    ['circle', {cx: 16, cy: 6, r: 2.2}],
    ['circle', {cx: 9, cy: 12, r: 2.2}],
    ['circle', {cx: 15, cy: 18, r: 2.2}],
    'M3.5 6h10.3', 'M18.2 6H20.5',
    'M3.5 12h3.3', 'M11.2 12H20.5',
    'M3.5 18h9.3', 'M17.2 18H20.5',
  ],
  system: [
    ['rect', {x: 3, y: 4, width: 18, height: 7, rx: 2}],
    ['rect', {x: 3, y: 13, width: 18, height: 7, rx: 2}],
    'M7 7.5h.01', 'M7 16.5h.01',
  ],
  files: [
    'M9 3h5l4.5 4.5V19a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z',
    'M14 3v5h4.5',
    'M4.5 7.5V18a3 3 0 0 0 3 3H15',
  ],
  terminal: ['M4.5 17.5 10 12 4.5 6.5', 'M12.5 18.5H20'],
  logs: ['M4 6h16', 'M4 10.5h10', 'M4 15h16', 'M4 19.5h7'],
  services: [
    ['circle', {cx: 12, cy: 12, r: 3.2}],
    'M12 2.5v3', 'M12 18.5v3', 'M2.5 12h3', 'M18.5 12h3',
    'M5.2 5.2l2.1 2.1', 'M16.7 16.7l2.1 2.1', 'M18.8 5.2l-2.1 2.1', 'M7.3 16.7l-2.1 2.1',
  ],
  bird: [
    'M19.6 8.1a2.8 2.8 0 1 0-4.8-2L10.4 7.3A6.6 6.6 0 0 0 4 13.9V21l3.4-2.6h4.2a6.6 6.6 0 0 0 6.6-6.6V9.4z',
    'M18.4 9.4 22 8.2',
    ['circle', {cx: 17.3, cy: 6.5, r: 0.75, fill: 'currentColor', stroke: 'none'}],
  ],
  play: ['M8 5.2 19 12 8 18.8z'],
  pause: [
    ['rect', {x: 8, y: 5, width: 3, height: 14, rx: 1.2}],
    ['rect', {x: 13, y: 5, width: 3, height: 14, rx: 1.2}],
  ],
  stop: [['rect', {x: 6, y: 6, width: 12, height: 12, rx: 2.2}]],
  next: ['M6 5.5 15 12 6 18.5z', 'M18 5.5v13'],
  previous: ['M18 5.5 9 12l9 6.5z', 'M6 5.5v13'],
  heart: ['M12 20.6 4.9 13.4a4.6 4.6 0 0 1 6.5-6.5l.6.6.6-.6a4.6 4.6 0 0 1 6.5 6.5z'],
  'heart-broken': [
    'M12 20.6 4.9 13.4a4.6 4.6 0 0 1 6.5-6.5l.6.6.6-.6a4.6 4.6 0 0 1 6.5 6.5z',
    'M12.6 7.2 10.4 11h3.4l-2.4 4.2',
  ],
  'thumbs-down': [
    'M17.5 3.5v9.5',
    'M6.5 3.5h7.6a2 2 0 0 1 2 1.7l1 6.2a2 2 0 0 1-2 2.3h-2.4l.9 3.3a2.2 2.2 0 0 1-4.2 1.5L6.6 14h-.1a2 2 0 0 1-2-2V5.5a2 2 0 0 1 2-2z',
  ],
  'thumbs-up': [
    'M6.5 20.5V11',
    'M6.5 11h1.2l2.7-6.2a2.2 2.2 0 0 1 4.2 1.5L13.7 9.6h2.4a2 2 0 0 1 2 2.3l-1 6.2a2 2 0 0 1-2 1.7H8.5a2 2 0 0 1-2-2z',
  ],
  volume: [
    'M11 5 6.5 9H3v6h3.5L11 19z',
    'M15.4 9.4a4 4 0 0 1 0 5.2',
    'M18.4 6.8a8 8 0 0 1 0 10.4',
  ],
  mute: ['M11 5 6.5 9H3v6h3.5L11 19z', 'M16 10l5 4', 'M21 10l-5 4'],
  speaker: [
    ['rect', {x: 5, y: 2.5, width: 14, height: 19, rx: 2.4}],
    ['circle', {cx: 12, cy: 14.5, r: 3.4}],
    'M12 6.5h.01',
  ],
  tv: [
    ['rect', {x: 2.5, y: 6, width: 19, height: 13, rx: 2.2}],
    'M8 2.8 12 6l4-3.2',
  ],
  cast: [
    'M15.5 19.5h4a1.5 1.5 0 0 0 1.5-1.5V7a1.5 1.5 0 0 0-1.5-1.5h-15A1.5 1.5 0 0 0 3 7v1.5',
    'M3 12.5a7 7 0 0 1 7 7',
    'M3 16.5a3 3 0 0 1 3 3',
    ['circle', {cx: 3.4, cy: 19.9, r: 0.9, fill: 'currentColor', stroke: 'none'}],
  ],
  cpu: [
    ['rect', {x: 4.5, y: 4.5, width: 15, height: 15, rx: 2.2}],
    ['rect', {x: 9, y: 9, width: 6, height: 6, rx: 1.2}],
    'M9 2v2.5', 'M15 2v2.5', 'M9 19.5V22', 'M15 19.5V22',
    'M2 9h2.5', 'M2 15h2.5', 'M19.5 9H22', 'M19.5 15H22',
  ],
  chip: [
    ['rect', {x: 3.5, y: 6.5, width: 17, height: 11, rx: 2}],
    'M8 10.5h8v3H8z',
    'M8 3.5v3', 'M16 3.5v3', 'M8 17.5v3', 'M16 17.5v3',
  ],
  thermometer: ['M14 14.8V4.6a2.5 2.5 0 0 0-5 0v10.2a4.6 4.6 0 1 0 5 0z', 'M11.5 8.5v6.8'],
  disk: [
    ['ellipse', {cx: 12, cy: 6, rx: 8, ry: 3.1}],
    'M4 6v12c0 1.7 3.6 3.1 8 3.1s8-1.4 8-3.1V6',
    'M20 12c0 1.7-3.6 3.1-8 3.1S4 13.7 4 12',
  ],
  wifi: [
    'M2.6 9.3a15 15 0 0 1 18.8 0',
    'M5.9 13a10 10 0 0 1 12.2 0',
    'M9.2 16.6a5 5 0 0 1 5.6 0',
    ['circle', {cx: 12, cy: 20, r: 1, fill: 'currentColor', stroke: 'none'}],
  ],
  plus: ['M12 5v14', 'M5 12h14'],
  minus: ['M5 12h14'],
  trash: [
    'M4 7h16', 'M9.5 7V5.2A1.2 1.2 0 0 1 10.7 4h2.6a1.2 1.2 0 0 1 1.2 1.2V7',
    'M6.2 7l.9 12.1A2 2 0 0 0 9.1 21h5.8a2 2 0 0 0 2-1.9L17.8 7',
    'M10.3 11v6', 'M13.7 11v6',
  ],
  refresh: ['M20.5 12a8.5 8.5 0 1 1-2.5-6', 'M20.5 4v5h-5'],
  search: [['circle', {cx: 11, cy: 11, r: 6.6}], 'M20.5 20.5 16 16'],
  chevron: ['M9.5 5 16.5 12 9.5 19'],
  check: ['M5 12.6 9.6 17.2 19 6.8'],
  x: ['M6 6l12 12', 'M18 6 6 18'],
  warning: [
    'M12 4.2 2.9 20h18.2z',
    'M12 10v4.4',
    ['circle', {cx: 12, cy: 17.4, r: 0.9, fill: 'currentColor', stroke: 'none'}],
  ],
  info: [
    ['circle', {cx: 12, cy: 12, r: 8.6}],
    'M12 11.2v5',
    ['circle', {cx: 12, cy: 8.2, r: 0.9, fill: 'currentColor', stroke: 'none'}],
  ],
  lock: [
    ['rect', {x: 4.5, y: 10, width: 15, height: 10.5, rx: 2.2}],
    'M8.2 10V7.6a3.8 3.8 0 0 1 7.6 0V10',
  ],
  unlock: [
    ['rect', {x: 4.5, y: 10, width: 15, height: 10.5, rx: 2.2}],
    'M8.2 10V7.6a3.8 3.8 0 0 1 7.3-1.3',
  ],
  moon: ['M20.4 14.3A8.7 8.7 0 0 1 9.7 3.6a8.6 8.6 0 1 0 10.7 10.7z'],
  sun: [
    ['circle', {cx: 12, cy: 12, r: 4.2}],
    'M12 2v2.4', 'M12 19.6V22', 'M2 12h2.4', 'M19.6 12H22',
    'M4.9 4.9 6.6 6.6', 'M17.4 17.4l1.7 1.7', 'M19.1 4.9l-1.7 1.7', 'M6.6 17.4l-1.7 1.7',
  ],
  menu: ['M4 7h16', 'M4 12h16', 'M4 17h16'],
  download: ['M12 3.5v11.5', 'M7.5 10.5 12 15l4.5-4.5', 'M4.5 19.5h15'],
  upload: ['M12 20.5V9', 'M7.5 13.5 12 9l4.5 4.5', 'M4.5 3.5h15'],
  folder: [
    'M3 7.5A2.5 2.5 0 0 1 5.5 5h3.2a2 2 0 0 1 1.6.8l1 1.4a2 2 0 0 0 1.6.8h5.6A2.5 2.5 0 0 1 21 10.5v6A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z',
  ],
  file: ['M14 3H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7z', 'M14 3v4h4'],
  edit: ['M4 20h4.2L20 8.2a2.9 2.9 0 0 0-4.2-4.2L4 15.8z', 'M14.6 5.4l4 4'],
  save: [
    'M4 5.5A1.5 1.5 0 0 1 5.5 4h9.7L20 8.8v9.7a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5z',
    'M8 4v5.5h7V4.5',
    'M8 20v-6h8v6',
  ],
  power: ['M12 3.5v8.8', 'M17.9 6.6a8.4 8.4 0 1 1-11.8 0'],
  link: [
    'M10.5 13.5a4 4 0 0 0 5.7 0l2.6-2.6a4 4 0 0 0-5.7-5.7l-1.4 1.4',
    'M13.5 10.5a4 4 0 0 0-5.7 0l-2.6 2.6a4 4 0 1 0 5.7 5.7l1.4-1.4',
  ],
  clock: [['circle', {cx: 12, cy: 12, r: 8.6}], 'M12 7.2V12l3.2 2'],
  home: ['M4 10.5 12 3.5l8 7V19a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z', 'M9.5 21v-6h5v6'],
  user: [['circle', {cx: 12, cy: 8, r: 3.8}], 'M4.8 20.5a7.4 7.4 0 0 1 14.4 0'],
  eye: ['M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z', ['circle', {cx: 12, cy: 12, r: 3}]],
  'eye-off': [
    'M4 4l16 16',
    'M9.6 6.3A9.7 9.7 0 0 1 12 6c6 0 9.5 6 9.5 6a17 17 0 0 1-3.4 4',
    'M6.4 8.2A16.7 16.7 0 0 0 2.5 12S6 18 12 18a9.8 9.8 0 0 0 3.6-.7',
  ],
  dots: [
    ['circle', {cx: 5.5, cy: 12, r: 1.4, fill: 'currentColor', stroke: 'none'}],
    ['circle', {cx: 12, cy: 12, r: 1.4, fill: 'currentColor', stroke: 'none'}],
    ['circle', {cx: 18.5, cy: 12, r: 1.4, fill: 'currentColor', stroke: 'none'}],
  ],

  // --- store categories -----------------------------------------------------
  // Every `icon:` in project_os/data/catalog.yaml must resolve to a name here.
  plug: [
    'M9 3v5', 'M15 3v5',
    'M6 8h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6z',
    'M12 17v4',
  ],
  key: [
    ['circle', {cx: 8, cy: 12, r: 4}],
    'M12 12h9', 'M17.5 12v3.5', 'M20.5 12v2.5',
  ],
  broadcast: [
    ['circle', {cx: 12, cy: 12, r: 2}],
    'M8.5 8.5a5 5 0 0 0 0 7', 'M15.5 15.5a5 5 0 0 0 0-7',
    'M5.7 5.7a9 9 0 0 0 0 12.6', 'M18.3 18.3a9 9 0 0 0 0-12.6',
  ],
  bulb: [
    'M9 18h6', 'M10 21h4',
    'M12 3a6 6 0 0 0-3.5 10.9V15h7v-1.1A6 6 0 0 0 12 3z',
  ],
  // Paper on top, paper below, machine in between. Serves the 3D printer too:
  // one icon that reads as "printer" beats two that read as neither.
  printer: [
    'M7 9V3.5h10V9',
    'M6 17.5H4.5A1.5 1.5 0 0 1 3 16v-5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v5a1.5 1.5 0 0 1-1.5 1.5H18',
    ['rect', {x: 7, y: 14, width: 10, height: 6.5, rx: 1}],
    ['circle', {cx: 17.5, cy: 12, r: 0.6, fill: 'currentColor'}],
  ],
  flow: [
    ['rect', {x: 2.5, y: 4, width: 6, height: 5, rx: 1.4}],
    ['rect', {x: 15.5, y: 4, width: 6, height: 5, rx: 1.4}],
    ['rect', {x: 9, y: 15, width: 6, height: 5, rx: 1.4}],
    'M5.5 9v3.5h13V9', 'M12 12.5V15',
  ],
  film: [
    ['rect', {x: 2.5, y: 4.5, width: 19, height: 15, rx: 2}],
    'M7.5 4.5v15', 'M16.5 4.5v15', 'M2.5 12h19',
  ],
  music: [
    'M9 18V6l11-2v12',
    ['circle', {cx: 6.5, cy: 18, r: 2.5}],
    ['circle', {cx: 17.5, cy: 16, r: 2.5}],
  ],
  book: [
    'M4 4.5A1.5 1.5 0 0 1 5.5 3H19v18H5.5A1.5 1.5 0 0 1 4 19.5z',
    'M4 17.5h15',
  ],
  image: [
    ['rect', {x: 3, y: 4.5, width: 18, height: 15, rx: 2}],
    ['circle', {cx: 8.5, cy: 10, r: 1.6}],
    'M3.5 17l5-4.5 4 3.5 3-2.5 5 4',
  ],
  box: [
    'M21 8v8a2 2 0 0 1-1 1.73l-7 4a2 2 0 0 1-2 0l-7-4A2 2 0 0 1 3 16V8a2 2 0 0 1 1-1.73l7-4a2 2 0 0 1 2 0l7 4A2 2 0 0 1 21 8z',
    'M3.4 7.1 12 12l8.6-4.9', 'M12 22V12',
  ],
  globe: [
    ['circle', {cx: 12, cy: 12, r: 9}],
    'M3.2 9.5h17.6', 'M3.2 14.5h17.6',
    'M12 3a14 14 0 0 0 0 18', 'M12 3a14 14 0 0 1 0 18',
  ],
  code: ['M8.5 8 4 12l4.5 4', 'M15.5 8 20 12l-4.5 4', 'M13.5 4.5l-3 15'],
  git: [
    ['circle', {cx: 7, cy: 5.5, r: 2.5}],
    ['circle', {cx: 7, cy: 18.5, r: 2.5}],
    ['circle', {cx: 17, cy: 9, r: 2.5}],
    'M7 8v8', 'M17 11.5c0 3-3.5 3.5-6.5 4.5',
  ],
  server: [
    ['rect', {x: 3, y: 3.5, width: 18, height: 6.5, rx: 1.8}],
    ['rect', {x: 3, y: 14, width: 18, height: 6.5, rx: 1.8}],
    'M7 6.8h.01', 'M7 17.2h.01',
  ],
  shield: ['M12 3l8 3v6c0 4.5-3.2 8.2-8 9.9-4.8-1.7-8-5.4-8-9.9V6z', 'M9 12l2 2 4-4.5'],
  sync: ['M20 11a8 8 0 0 0-13.7-5L3.5 8.5', 'M3.5 4v4.5H8', 'M4 13a8 8 0 0 0 13.7 5l2.8-2.5', 'M20.5 20v-4.5H16'],
  pulse: ['M3 12.5h4l2.5-6 4 12 2.5-6H21'],
  camera: [
    ['rect', {x: 2.5, y: 6.5, width: 13, height: 11, rx: 2}],
    'M15.5 10.5 21.5 7v10l-6-3.5z',
  ],
  chat: ['M20.5 12.5c0 3.9-3.8 7-8.5 7a10 10 0 0 1-2.6-.33L4 21l1.2-3.5A6.4 6.4 0 0 1 3.5 12.5c0-3.9 3.8-7 8.5-7s8.5 3.1 8.5 7z'],
  chart: ['M4 20V4', 'M4 20h16', 'M8 17V12', 'M12.5 17V7.5', 'M17 17v-6.5'],
};

const warned = new Set();

/** Build an <svg> icon element. Unknown names degrade to a neutral dot. */
export function icon(name, options = {}) {
  const {size = 20, class: cls = '', title = '', strokeWidth = 1.7} = options;
  let shapes = ICONS[name];
  if (!shapes) {
    if (!warned.has(name)) {
      warned.add(name);
      console.warn('[icons] unknown icon:', name);
    }
    shapes = ICONS.dots;
  }
  const children = shapes.map((shape) => {
    if (typeof shape === 'string') return svg('path', {d: shape});
    return svg(shape[0], shape[1] || {});
  });
  if (title) children.unshift(svg('title', null, title));
  return svg('svg', {
    class: 'icon' + (cls ? ' ' + cls : ''),
    viewBox: '0 0 24 24',
    width: size,
    height: size,
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': strokeWidth,
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': title ? null : 'true',
    role: title ? 'img' : null,
    focusable: 'false',
  }, children);
}

export function hasIcon(name) {
  return typeof name === 'string' && Object.prototype.hasOwnProperty.call(ICONS, name);
}

/** Whether a manifest icon is an emoji rather than one of our names.
 *  Kept so we can *detect* and drop them: an app from outside may still ship one,
 *  and one emoji in a row of line icons is exactly what makes a list look sloppy. */
export function isEmojiIcon(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 8 &&
    /[^\x00-\x7F]/.test(value);
}

/** Resolve a manifest icon to a renderable node. Always a line icon, never an
 *  emoji: an unknown or emoji value falls back to `fallback`, so a card always
 *  has a mark and the row always looks like one set. */
export function appIcon(value, {fallback = 'apps', size = 18, class: cls = ''} = {}) {
  const name = hasIcon(value) ? value : fallback;
  return icon(name, {size, class: cls});
}

export default {ICONS, icon, hasIcon, isEmojiIcon, appIcon};
