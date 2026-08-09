// store.js — the smallest state container that is still pleasant to use.
//
//   const store = createStore({count: 0});
//   const off = store.subscribe((state, prev) => render(state));
//   store.set({count: 1});          // shallow merge
//   store.update(s => ({count: s.count + 1}));
//
// Notification is synchronous so a view can rely on the DOM being up to date
// right after set(). A subscriber that throws is logged and skipped: one broken
// card must never freeze the rest of the UI.

export function createStore(initial) {
  let state = Object.assign({}, initial || {});
  const subscribers = new Set();
  let notifying = false;
  let pending = null;

  function notify(prev) {
    // Re-entrant set() during notification is queued so subscribers always see
    // a consistent state and we never recurse without bound.
    if (notifying) {
      pending = pending || prev;
      return;
    }
    notifying = true;
    try {
      let previous = prev;
      for (;;) {
        for (const fn of Array.from(subscribers)) {
          try {
            fn(state, previous);
          } catch (err) {
            console.error('[store] subscriber failed', err);
          }
        }
        if (pending === null) break;
        previous = pending;
        pending = null;
      }
    } finally {
      notifying = false;
      pending = null;
    }
  }

  function changed(patch) {
    for (const key of Object.keys(patch)) {
      if (!Object.is(state[key], patch[key])) return true;
    }
    return false;
  }

  return {
    get(key) {
      return key === undefined ? state : state[key];
    },
    set(patch) {
      if (!patch) return state;
      if (!changed(patch)) return state;
      const prev = state;
      state = Object.assign({}, state, patch);
      notify(prev);
      return state;
    },
    update(fn) {
      const patch = fn(state);
      if (patch === undefined || patch === null) return state;
      // update() may return either a patch or a whole new state object.
      return this.set(patch);
    },
    replace(next) {
      const prev = state;
      state = Object.assign({}, next || {});
      notify(prev);
      return state;
    },
    subscribe(fn, {immediate = false} = {}) {
      if (typeof fn !== 'function') throw new TypeError('subscribe() needs a function');
      subscribers.add(fn);
      if (immediate) {
        try {
          fn(state, state);
        } catch (err) {
          console.error('[store] subscriber failed', err);
        }
      }
      return () => subscribers.delete(fn);
    },
    /** Subscribe to one key only; fires when that key changes identity. */
    watch(key, fn, {immediate = false} = {}) {
      const off = this.subscribe((next, prev) => {
        if (!Object.is(next[key], prev[key])) fn(next[key], prev[key]);
      });
      if (immediate) {
        try {
          fn(state[key], state[key]);
        } catch (err) {
          console.error('[store] watcher failed', err);
        }
      }
      return off;
    },
  };
}

export default createStore;
