// router.js — hash router. No history API, so the whole app works when the
// static files are opened from any base path and survives a hard refresh.
//
//   const router = createRouter();
//   router.add('#/apps/:id', ctx => show(ctx.params.id));
//   router.notFound(ctx => show404(ctx.path));
//   router.start();
//   navigate('#/apps/birdtunes?tab=queue');
//
// Patterns are plain strings: "#/devices", "#/apps/:id", "#/files/*rest".
// A leading "#" is optional. Query strings are parsed into ctx.query and never
// take part in matching.

function normalizePath(input) {
  let path = String(input === null || input === undefined ? '' : input);
  if (path.startsWith('#')) path = path.slice(1);
  if (!path.startsWith('/')) path = '/' + path;
  // collapse duplicate slashes, drop a trailing slash (except root)
  path = path.replace(/\/{2,}/g, '/');
  if (path.length > 1 && path.endsWith('/')) path = path.slice(0, -1);
  return path;
}

function splitLocation(raw) {
  let hash = String(raw || '');
  if (hash.startsWith('#')) hash = hash.slice(1);
  const qIndex = hash.indexOf('?');
  const search = qIndex === -1 ? '' : hash.slice(qIndex + 1);
  const path = normalizePath(qIndex === -1 ? hash : hash.slice(0, qIndex));
  const query = {};
  if (search) {
    const params = new URLSearchParams(search);
    for (const [key, value] of params.entries()) query[key] = value;
  }
  return {path, query, search};
}

function compile(pattern) {
  const path = normalizePath(pattern);
  const segments = path.split('/').filter(Boolean);
  const parts = segments.map((segment) => {
    if (segment.startsWith(':')) return {type: 'param', name: segment.slice(1)};
    if (segment === '*') return {type: 'rest', name: 'rest'};
    if (segment.startsWith('*')) return {type: 'rest', name: segment.slice(1) || 'rest'};
    return {type: 'static', value: segment};
  });
  const score = parts.reduce((acc, p) => acc + (p.type === 'static' ? 2 : p.type === 'param' ? 1 : 0), 0);
  return {pattern: path, parts, score};
}

function matchRoute(route, path) {
  const segments = path.split('/').filter(Boolean);
  const params = {};
  for (let i = 0; i < route.parts.length; i += 1) {
    const part = route.parts[i];
    if (part.type === 'rest') {
      params[part.name] = segments.slice(i).map(decodeURIComponent).join('/');
      return params;
    }
    if (i >= segments.length) return null;
    if (part.type === 'static') {
      if (segments[i] !== part.value) return null;
    } else {
      params[part.name] = decodeURIComponent(segments[i]);
    }
  }
  return segments.length === route.parts.length ? params : null;
}

/** Change the location hash. Exported standalone so modules that must not know
 *  about the router instance (api.js on 401/428) can still redirect. */
export function navigate(path, {replace = false} = {}) {
  const target = '#' + (String(path).startsWith('#') ? String(path).slice(1) : String(path));
  const next = target === '#' ? '#/' : target;
  if (window.location.hash === next) {
    // Already there. A view that writes its own state into the URL -- Files
    // does, with ?path= -- calls this on every load, and re-dispatching would
    // mount it again, which loads again, which navigates again: the screen
    // never settles and on a Pi the fan comes on. Asking for the route you are
    // already on is not a request to run it twice; router.reload() is.
    if (replace) return;
    // A plain navigate() to the same hash fires no hashchange event, so a
    // deliberate re-run (clicking the current item in the sidebar) needs help.
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    return;
  }
  if (replace) {
    // Same route, different query: the view is recording where it is, not
    // asking to go anywhere. Files does this on every folder it opens, and
    // dispatching would tear the screen down and build it again -- losing the
    // scroll position, and, when the view navigates as it loads, looping.
    // Matching ignores the query string anyway (see the header), so this is the
    // same rule stated once more.
    const sameRoute = splitLocation(window.location.hash).path === splitLocation(next).path;
    const url = window.location.href.split('#')[0] + next;
    window.history.replaceState(null, '', url);
    if (!sameRoute) window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    window.location.hash = next;
  }
}

export function createRouter({base = '#/'} = {}) {
  const routes = [];
  const listeners = new Set();
  let missing = null;
  let started = false;
  let currentCtx = {path: normalizePath(base), params: {}, query: {}, route: null};
  let handling = 0;

  function resolve() {
    const {path, query} = splitLocation(window.location.hash);
    if (!window.location.hash || path === '/') {
      // Root -> whatever the caller declared as the base route.
      return {path: splitLocation(base).path, query};
    }
    return {path, query};
  }

  async function dispatch() {
    const token = ++handling;
    const {path, query} = resolve();
    let matched = null;
    let params = null;
    // Longest/most specific pattern wins, independent of registration order.
    const ordered = routes.slice().sort((a, b) => b.parts.length - a.parts.length || b.score - a.score);
    for (const route of ordered) {
      const found = matchRoute(route, path);
      if (found) {
        matched = route;
        params = found;
        break;
      }
    }
    const ctx = {
      path,
      params: params || {},
      query,
      route: matched ? matched.pattern : null,
      hash: '#' + path,
    };
    currentCtx = ctx;
    for (const fn of Array.from(listeners)) {
      try {
        fn(ctx);
      } catch (err) {
        console.error('[router] change listener failed', err);
      }
    }
    try {
      if (matched) await matched.handler(ctx);
      else if (missing) await missing(ctx);
      else console.warn('[router] no route for', path);
    } catch (err) {
      if (token === handling) console.error('[router] handler failed for', path, err);
    }
  }

  const onHashChange = () => { dispatch(); };

  return {
    add(pattern, handler) {
      if (typeof handler !== 'function') throw new TypeError('route handler must be a function');
      const route = compile(pattern);
      route.handler = handler;
      routes.push(route);
      return this;
    },
    /** Register many at once: {'#/': home, '#/apps/:id': panel} */
    addAll(map) {
      for (const pattern of Object.keys(map)) this.add(pattern, map[pattern]);
      return this;
    },
    notFound(fn) {
      missing = fn;
      return this;
    },
    start() {
      if (started) return this;
      started = true;
      window.addEventListener('hashchange', onHashChange);
      // Sem hash nenhum -- ou seja, quem digitou o endereço da caixa e nada
      // mais -- a barra ganha o "#/" aqui mesmo, com replaceState, e não por
      // navigate(): um endereço vazio já normaliza para "/", então navigate
      // enxerga a mesma rota, não dispara hashchange (replaceState também não
      // dispara sozinho) e volta sem ter chamado ninguém. O resultado era a
      // tela de carregamento para sempre em quem abria project-os.local --
      // funcionava só quando o navegador completava o endereço com o "#/algo"
      // de uma visita anterior.
      //
      // Escrever a barra e despachar são coisas separadas, e as duas
      // acontecem: dispatch() é chamado uma vez, com hash ou sem.
      if (!window.location.hash) {
        window.history.replaceState(null, '', window.location.href.split('#')[0] + base);
      }
      dispatch();
      return this;
    },
    stop() {
      started = false;
      window.removeEventListener('hashchange', onHashChange);
      return this;
    },
    navigate,
    /** Force the current route to run again (after login, config change, ...). */
    reload() {
      return dispatch();
    },
    current() {
      return currentCtx;
    },
    onChange(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

export {normalizePath, splitLocation};
export default createRouter;
