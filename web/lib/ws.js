// ws.js — event stream client for /api/ws.
//
// Server frames follow the EventBus contract: {topic, ts, data}. Subscribers
// register per topic and get (data, event). Nothing is buffered: a view that
// subscribes late simply sees future events, which is what a live dashboard
// wants.
//
//   const ws = connect();
//   const off = ws.on('system.stats', stats => render(stats));
//   ws.on('app.birdtunes.*', (data, ev) => ...);   // prefix wildcard
//   ws.on('*', (data, ev) => ...);                 // everything
//   ws.state.subscribe(s => pill.textContent = s.status);

import {createStore} from './store.js';

const MIN_BACKOFF = 500;
const MAX_BACKOFF = 15000;
const HEARTBEAT_MS = 25000;
// If the socket goes quiet for this long we assume a half-open connection
// (very common on Wi-Fi sleep) and force a reconnect.
const SILENCE_MS = 75000;

function defaultUrl(path) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return protocol + '//' + window.location.host + path;
}

export function connect(options = {}) {
  const {
    path = '/api/ws',
    url = defaultUrl(path),
    autoStart = true,
  } = options;

  const handlers = new Map(); // topic pattern -> Set<fn>
  const state = createStore({
    status: 'idle', // idle | connecting | open | reconnecting | closed
    attempts: 0,
    lastError: null,
    connectedAt: null,
    lastEventAt: null,
  });

  let socket = null;
  let attempts = 0;
  let reconnectTimer = null;
  let heartbeatTimer = null;
  let closedByUser = false;
  let lastInbound = 0;

  function emit(topic, data, event) {
    const deliver = (set) => {
      if (!set) return;
      for (const fn of Array.from(set)) {
        try {
          fn(data, event);
        } catch (err) {
          console.error('[ws] handler failed for', topic, err);
        }
      }
    };
    deliver(handlers.get(topic));
    // "__open"/"__close" are ours, not the server's: only someone who asked for
    // them by name gets them, so on('*') stays "every event on the bus".
    if (topic.startsWith('__')) return;
    deliver(handlers.get('*'));
    // prefix wildcards: "app.birdtunes.*" matches "app.birdtunes.state"
    for (const [pattern, set] of handlers) {
      if (pattern === '*' || !pattern.endsWith('*')) continue;
      const prefix = pattern.slice(0, -1);
      if (topic.startsWith(prefix) && topic !== pattern) deliver(set);
    }
  }

  function clearTimers() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function scheduleReconnect(reason) {
    if (closedByUser || reconnectTimer) return;
    attempts += 1;
    const base = Math.min(MAX_BACKOFF, MIN_BACKOFF * Math.pow(2, attempts - 1));
    const delay = Math.round(base * (0.7 + Math.random() * 0.6)); // jitter
    state.set({status: 'reconnecting', attempts, lastError: reason || null});
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      open();
    }, delay);
  }

  function beat() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    if (lastInbound && Date.now() - lastInbound > SILENCE_MS) {
      // Half-open socket: close it and let the backoff logic reconnect.
      try {
        socket.close(4000, 'silent');
      } catch (err) {
        /* ignore */
      }
      return;
    }
    try {
      socket.send(JSON.stringify({type: 'ping', ts: Date.now()}));
    } catch (err) {
      /* the close handler will deal with it */
    }
  }

  function open() {
    if (closedByUser) return;
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    state.set({status: attempts ? 'reconnecting' : 'connecting'});
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      scheduleReconnect(String(err && err.message ? err.message : err));
      return;
    }
    socket = ws;

    ws.addEventListener('open', () => {
      attempts = 0;
      lastInbound = Date.now();
      state.set({status: 'open', attempts: 0, lastError: null, connectedAt: Date.now()});
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
      if (heartbeatTimer) clearInterval(heartbeatTimer);
      heartbeatTimer = setInterval(beat, HEARTBEAT_MS);
      emit('__open', {url}, {topic: '__open', data: {url}});
    });

    ws.addEventListener('message', (event) => {
      lastInbound = Date.now();
      state.set({lastEventAt: lastInbound});
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (err) {
        return; // non-JSON frames are not part of the contract
      }
      if (!payload || typeof payload !== 'object') return;
      if (payload.type === 'ping') {
        try {
          ws.send(JSON.stringify({type: 'pong', ts: Date.now()}));
        } catch (err) {
          /* ignore */
        }
        return;
      }
      if (payload.type === 'pong') return;
      const topic = payload.topic || payload.event || payload.type;
      if (!topic) return;
      emit(String(topic), payload.data !== undefined ? payload.data : payload, payload);
    });

    ws.addEventListener('error', () => {
      state.set({lastError: 'connection error'});
    });

    ws.addEventListener('close', (event) => {
      if (socket === ws) socket = null;
      if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
      emit('__close', {code: event.code}, {topic: '__close', data: {code: event.code}});
      if (closedByUser) {
        state.set({status: 'closed'});
        return;
      }
      scheduleReconnect(event.reason || ('closed (' + event.code + ')'));
    });
  }

  function wake() {
    if (closedByUser) return;
    if (socket && socket.readyState === WebSocket.OPEN) return;
    // Coming back from sleep / regaining network: retry immediately.
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    attempts = 0;
    open();
  }

  const onOnline = () => wake();
  const onVisible = () => {
    if (document.visibilityState === 'visible') wake();
  };

  window.addEventListener('online', onOnline);
  document.addEventListener('visibilitychange', onVisible);

  const client = {
    state,
    url,
    on(topic, fn) {
      const key = String(topic);
      let set = handlers.get(key);
      if (!set) {
        set = new Set();
        handlers.set(key, set);
      }
      set.add(fn);
      return () => {
        const current = handlers.get(key);
        if (!current) return;
        current.delete(fn);
        if (!current.size) handlers.delete(key);
      };
    },
    /** Subscribe to several topics with one handler; returns one unsubscribe. */
    onAny(topics, fn) {
      const offs = topics.map((topic) => client.on(topic, fn));
      return () => offs.forEach((off) => off());
    },
    send(message) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return false;
      try {
        socket.send(typeof message === 'string' ? message : JSON.stringify(message));
        return true;
      } catch (err) {
        return false;
      }
    },
    start() {
      closedByUser = false;
      open();
      return client;
    },
    reconnect() {
      if (socket) {
        try {
          socket.close(4001, 'manual');
        } catch (err) {
          /* ignore */
        }
      }
      attempts = 0;
      wake();
      return client;
    },
    close() {
      closedByUser = true;
      clearTimers();
      window.removeEventListener('online', onOnline);
      document.removeEventListener('visibilitychange', onVisible);
      if (socket) {
        try {
          socket.close(1000, 'client closed');
        } catch (err) {
          /* ignore */
        }
        socket = null;
      }
      state.set({status: 'closed'});
    },
    isOpen() {
      return Boolean(socket && socket.readyState === WebSocket.OPEN);
    },
  };

  if (autoStart) client.start();
  return client;
}

export default connect;
