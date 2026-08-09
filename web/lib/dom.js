// dom.js — hyperscript producing REAL DOM nodes. No virtual DOM, no innerHTML,
// therefore no escaping bugs: every string becomes a text node.
//
//   h('div', {class: 'card', onClick: fn}, 'hello', h('b', null, 'world'))
//
// Props:
//   class / className   -> string, array or object {name: truthy}
//   style               -> string or object (camelCase or kebab keys, --custom-props ok)
//   dataset             -> object of data-* values
//   on<Event>           -> listener; `onClick` === `onclick` === click.
//                          Value may be fn or [fn, options].
//   ref                 -> callback invoked with the created element
//   key                 -> ignored (accepted so callers can annotate lists)
//   true                -> boolean attribute present, false/null/undefined -> omitted
//   anything else        -> setAttribute (or a direct property for form values)

const SVG_NS = 'http://www.w3.org/2000/svg';

// Attributes that must be assigned as DOM properties to behave correctly.
const PROPS = new Set(['value', 'checked', 'selected', 'indeterminate', 'disabled', 'multiple', 'open']);

// camelCase -> kebab-case for the handful of SVG presentation attributes that
// callers habitually type in camel form. Case-sensitive SVG attributes such as
// viewBox or preserveAspectRatio are deliberately absent and pass through.
const SVG_ATTR_ALIAS = {
  strokeWidth: 'stroke-width',
  strokeLinecap: 'stroke-linecap',
  strokeLinejoin: 'stroke-linejoin',
  strokeDasharray: 'stroke-dasharray',
  strokeDashoffset: 'stroke-dashoffset',
  strokeOpacity: 'stroke-opacity',
  strokeMiterlimit: 'stroke-miterlimit',
  fillRule: 'fill-rule',
  fillOpacity: 'fill-opacity',
  clipRule: 'clip-rule',
  clipPath: 'clip-path',
  textAnchor: 'text-anchor',
  fontSize: 'font-size',
  fontFamily: 'font-family',
  fontWeight: 'font-weight',
  stopColor: 'stop-color',
  stopOpacity: 'stop-opacity',
};

function isPlainProps(value) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    !(value instanceof Node) &&
    !(typeof Text !== 'undefined' && value instanceof Text)
  );
}

function classToString(value) {
  if (value === null || value === undefined || value === false) return '';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(classToString).filter(Boolean).join(' ');
  if (typeof value === 'object') {
    const out = [];
    for (const key of Object.keys(value)) if (value[key]) out.push(key);
    return out.join(' ');
  }
  return String(value);
}

function applyStyle(el, value) {
  if (typeof value === 'string') {
    el.setAttribute('style', value);
    return;
  }
  if (!value || typeof value !== 'object') return;
  for (const key of Object.keys(value)) {
    const v = value[key];
    if (v === null || v === undefined || v === false) continue;
    if (key.startsWith('--')) el.style.setProperty(key, String(v));
    else el.style[key] = typeof v === 'number' && !CSS_UNITLESS.has(key) ? v + 'px' : String(v);
  }
}

const CSS_UNITLESS = new Set([
  'opacity', 'zIndex', 'flex', 'flexGrow', 'flexShrink', 'order', 'lineHeight',
  'fontWeight', 'zoom', 'gridRow', 'gridColumn', 'columnCount',
]);

// onClick -> click, onclick -> click, onDblClick -> dblclick
function eventName(key) {
  return key.slice(2).toLowerCase();
}

function setProp(el, key, value, isSvg) {
  if (key === 'key') return;
  if (key === 'ref') {
    if (typeof value === 'function') value(el);
    return;
  }
  if (key === 'class' || key === 'className') {
    const cls = classToString(value);
    if (cls) el.setAttribute('class', cls);
    return;
  }
  if (key === 'style') {
    applyStyle(el, value);
    return;
  }
  if (key === 'dataset') {
    if (value && typeof value === 'object') {
      for (const dk of Object.keys(value)) {
        const dv = value[dk];
        if (dv === null || dv === undefined || dv === false) continue;
        el.dataset[dk] = String(dv);
      }
    }
    return;
  }
  if (key.length > 2 && key.charCodeAt(0) === 111 /* o */ && key.charCodeAt(1) === 110 /* n */) {
    const type = eventName(key);
    if (Array.isArray(value)) el.addEventListener(type, value[0], value[1]);
    else if (typeof value === 'function') el.addEventListener(type, value);
    return;
  }
  if (value === null || value === undefined || value === false) return;
  if (value === true) {
    if (!isSvg && PROPS.has(key)) el[key] = true;
    else el.setAttribute(key, '');
    return;
  }
  if (!isSvg && PROPS.has(key)) {
    el[key] = value;
    // Keep the attribute in sync so CSS selectors and form resets behave.
    if (key === 'value') el.setAttribute('value', String(value));
    return;
  }
  const name = isSvg ? (SVG_ATTR_ALIAS[key] || key) : key;
  el.setAttribute(name, String(value));
}

function appendChild(parent, child) {
  if (child === null || child === undefined || child === false || child === true) return;
  if (Array.isArray(child)) {
    for (const c of child) appendChild(parent, c);
    return;
  }
  if (child instanceof Node) {
    parent.appendChild(child);
    return;
  }
  if (typeof child === 'function') {
    appendChild(parent, child());
    return;
  }
  parent.appendChild(document.createTextNode(String(child)));
}

function create(ns, tag, props, children) {
  const el = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
  if (isPlainProps(props)) {
    for (const key of Object.keys(props)) setProp(el, key, props[key], Boolean(ns));
  }
  for (const child of children) appendChild(el, child);
  return el;
}

/** Create an HTML element. `props` may be omitted — the second argument is
 *  treated as a child when it is not a plain object. */
export function h(tag, props, ...children) {
  if (!isPlainProps(props) && props !== null && props !== undefined) {
    return create(null, tag, null, [props, ...children]);
  }
  return create(null, tag, props, children);
}

/** Same as h() but in the SVG namespace (icons, sparklines, meters). */
export function svg(tag, props, ...children) {
  if (!isPlainProps(props) && props !== null && props !== undefined) {
    return create(SVG_NS, tag, null, [props, ...children]);
  }
  return create(SVG_NS, tag, props, children);
}

/** Text node. */
export function text(value) {
  return document.createTextNode(value === null || value === undefined ? '' : String(value));
}

/** DocumentFragment from any children spec. */
export function frag(...children) {
  const f = document.createDocumentFragment();
  for (const child of children) appendChild(f, child);
  return f;
}

/** Remove every child of `el`. Returns el. */
export function clear(el) {
  if (!el) return el;
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/** Replace the contents of `el` with `node` (node may be an array/fragment). */
export function mount(el, node) {
  clear(el);
  appendChild(el, node);
  return el;
}

/** Conditional child: when(isReady, () => h('div', ...)). Falsy -> null. */
export function when(cond, node) {
  if (!cond) return null;
  return typeof node === 'function' ? node() : node;
}

/** Append children to an existing element without clearing it. */
export function append(el, ...children) {
  for (const child of children) appendChild(el, child);
  return el;
}

export { SVG_NS };
