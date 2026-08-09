// api.js — fetch wrapper for the ProjectOS HTTP API.
//
// Every error the server can produce follows {error, message, detail}; this
// module turns that into a thrown ApiError with .code / .status / .detail so
// views can branch on a code instead of parsing prose.
//
//   await api.get('/system/stats')          -> /api/system/stats
//   await api.post('/auth/login', {...})
//   const appApi = apiFor('/api/apps/birdtunes');
//   await appApi.get('/status')             -> /api/apps/birdtunes/status
//
// A path that already starts with the prefix (or with http://) is left alone,
// so both '/health' and '/api/health' work.

import {navigate} from './router.js';

const BASE = '/api';

export class ApiError extends Error {
  constructor(message, {code = 'error', status = 0, detail = null, path = ''} = {}) {
    super(message || code || 'Request failed');
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.detail = detail;
    this.path = path;
  }
}

function joinPath(prefix, path) {
  const p = String(path || '');
  if (/^https?:\/\//i.test(p)) return p;
  if (!prefix) return p.startsWith('/') ? p : '/' + p;
  if (p === '' || p === '/') return prefix;
  if (p.startsWith(prefix + '/') || p === prefix) return p;
  if (p.startsWith('/')) return prefix + p;
  return prefix + '/' + p;
}

function isJson(response) {
  const type = response.headers.get('content-type') || '';
  return type.includes('json');
}

async function readBody(response) {
  if (response.status === 204 || response.status === 205) return null;
  const raw = await response.text();
  if (!raw) return null;
  if (isJson(response)) {
    try {
      return JSON.parse(raw);
    } catch (err) {
      throw new ApiError('Malformed JSON in response', {code: 'bad_response', status: response.status});
    }
  }
  return raw;
}

function toError(response, body, path) {
  let code = 'http_' + response.status;
  let message = response.statusText || 'Request failed';
  let detail = null;
  if (body && typeof body === 'object') {
    code = body.error || body.code || code;
    message = body.message || body.detail_message || message;
    detail = body.detail !== undefined ? body.detail : null;
    // FastAPI's own validation errors arrive as {detail: [...]}
    if (!body.message && typeof body.detail === 'string') message = body.detail;
  } else if (typeof body === 'string' && body.trim()) {
    message = body.slice(0, 400);
  }
  return new ApiError(message, {code, status: response.status, detail, path});
}

/** Low-level request. Options: {headers, signal, redirectOnAuth, raw, query}. */
export async function request(method, path, body, options = {}) {
  const {
    prefix = BASE,
    headers = {},
    signal,
    redirectOnAuth = true,
    raw = false,
    query = null,
  } = options;

  let url = joinPath(prefix, path);
  if (query && typeof query === 'object') {
    const search = new URLSearchParams();
    for (const key of Object.keys(query)) {
      const value = query[key];
      if (value === undefined || value === null || value === '') continue;
      search.set(key, String(value));
    }
    const qs = search.toString();
    if (qs) url += (url.includes('?') ? '&' : '?') + qs;
  }

  const init = {
    method,
    credentials: 'same-origin',
    headers: Object.assign({Accept: 'application/json'}, headers),
    signal,
  };

  if (body !== undefined && body !== null) {
    if (body instanceof FormData || body instanceof Blob || typeof body === 'string') {
      init.body = body;
    } else {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
  }

  let response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError('Cannot reach ProjectOS. Check that the server is running.', {
      code: 'network_error',
      status: 0,
      path: url,
    });
  }

  if (raw) return response;

  const payload = await readBody(response);

  if (!response.ok) {
    const error = toError(response, payload, url);
    if (redirectOnAuth) {
      if (response.status === 401) navigate('#/login', {replace: true});
      else if (response.status === 428 || error.code === 'setup_required') navigate('#/setup', {replace: true});
    }
    throw error;
  }
  return payload;
}

export const get = (path, options) => request('GET', path, null, options);
export const post = (path, body, options) => request('POST', path, body, options);
export const put = (path, body, options) => request('PUT', path, body, options);
export const patch = (path, body, options) => request('PATCH', path, body, options);
export const del = (path, body, options) => request('DELETE', path, body, options);

/** Same interface bound to a path prefix — used for app panels. */
export function apiFor(prefix) {
  const base = String(prefix || '').replace(/\/$/, '') || BASE;
  const withPrefix = (options) => Object.assign({}, options, {prefix: base});
  return {
    prefix: base,
    request: (method, path, body, options) => request(method, path, body, withPrefix(options)),
    get: (path, options) => request('GET', path, null, withPrefix(options)),
    post: (path, body, options) => request('POST', path, body, withPrefix(options)),
    put: (path, body, options) => request('PUT', path, body, withPrefix(options)),
    patch: (path, body, options) => request('PATCH', path, body, withPrefix(options)),
    del: (path, body, options) => request('DELETE', path, body, withPrefix(options)),
    upload: (path, file, onProgress, options) => upload(path, file, onProgress, withPrefix(options)),
    url: (path) => joinPath(base, path),
    for: (sub) => apiFor(joinPath(base, sub)),
  };
}

/** Multipart upload with progress. XHR, because fetch cannot report it. */
export function upload(path, file, onProgress, options = {}) {
  const {prefix = BASE, field = 'file', fields = null, method = 'POST'} = options;
  const url = joinPath(prefix, path);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Accept', 'application/json');

    if (xhr.upload && typeof onProgress === 'function') {
      xhr.upload.addEventListener('progress', (event) => {
        const total = event.lengthComputable ? event.total : 0;
        onProgress({
          loaded: event.loaded,
          total,
          percent: total ? Math.min(100, (event.loaded / total) * 100) : null,
        });
      });
    }

    xhr.addEventListener('error', () => {
      reject(new ApiError('Upload failed: network error', {code: 'network_error', path: url}));
    });
    xhr.addEventListener('abort', () => {
      reject(new ApiError('Upload cancelled', {code: 'aborted', path: url}));
    });
    xhr.addEventListener('load', () => {
      let payload = null;
      const raw = xhr.responseText;
      if (raw) {
        try {
          payload = JSON.parse(raw);
        } catch (err) {
          payload = raw;
        }
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload);
        return;
      }
      let code = 'http_' + xhr.status;
      let message = xhr.statusText || 'Upload failed';
      let detail = null;
      if (payload && typeof payload === 'object') {
        code = payload.error || code;
        message = payload.message || message;
        detail = payload.detail !== undefined ? payload.detail : null;
      }
      if (xhr.status === 401) navigate('#/login', {replace: true});
      else if (xhr.status === 428) navigate('#/setup', {replace: true});
      reject(new ApiError(message, {code, status: xhr.status, detail, path: url}));
    });

    const form = new FormData();
    if (fields && typeof fields === 'object') {
      for (const key of Object.keys(fields)) form.append(key, fields[key]);
    }
    form.append(field, file, file && file.name ? file.name : 'upload.bin');
    xhr.send(form);
  });
}

/** Absolute-ish URL for a core API path (media, downloads, panel assets). */
export function url(path) {
  return joinPath(BASE, path);
}

export default {request, get, post, put, patch, del, upload, apiFor, url, ApiError};
