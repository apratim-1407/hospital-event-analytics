/* The only place that talks to the REST API. Session lives in sessionStorage (cleared with the tab). */

export const API = '/api/v1';

const mem = {};
const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return mem[k] ?? null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { mem[k] = v; } },
  del(k) { try { sessionStorage.removeItem(k); } catch { delete mem[k]; } },
};

export const session = { access: store.get('access'), refresh: store.get('refresh'), email: store.get('email'), role: store.get('role') };
export const isAnalyst = () => session.role === 'analyst' || session.role === 'admin';
export const isAdmin = () => session.role === 'admin';

function save(tokens, email) {
  session.access = tokens.access_token;
  session.refresh = tokens.refresh_token;
  store.set('access', session.access);
  store.set('refresh', session.refresh);
  try {
    session.role = JSON.parse(atob(session.access.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))).role;
    store.set('role', session.role);
  } catch { /* keep previous role */ }
  if (email) { session.email = email; store.set('email', email); }
}
export function clearSession() {
  ['access', 'refresh', 'email', 'role'].forEach(store.del);
  session.access = session.refresh = session.email = session.role = null;
}

export class ApiError extends Error {
  constructor(message, status, code, details) { super(message); this.status = status; this.code = code; this.details = details; }
}

export async function login(email, password) {
  const r = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) });
  if (r.status === 401) throw new ApiError('That email and password do not match.', 401);
  if (!r.ok) throw new ApiError('The service is busy right now. Please try again in a moment.', r.status);
  save(await r.json(), email);
}

async function refresh() {
  if (!session.refresh) return false;
  try {
    const r = await fetch(`${API}/auth/refresh`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: session.refresh }) });
    if (!r.ok) return false;
    save(await r.json());
    return true;
  } catch { return false; }
}

async function parse(r) {
  if (r.ok) return r.status === 204 ? null : r.json();
  const body = await r.json().catch(() => ({}));
  throw new ApiError(body?.error?.message || `Something went wrong (${r.status}).`, r.status, body?.error?.code, body?.error?.details);
}

/** JWT-authenticated call. A 401 triggers one silent token refresh, then a sign-out. */
export async function api(path, opts = {}, retry = true) {
  const headers = { ...(opts.headers || {}), Authorization: `Bearer ${session.access}` };
  if (opts.body) headers['Content-Type'] = 'application/json';
  const r = await fetch(API + path, { ...opts, headers, body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body });
  if (r.status === 401) {
    if (retry && (await refresh())) return api(path, opts, false);
    window.dispatchEvent(new Event('signedout'));
    throw new ApiError('Your session has ended. Please sign in again.', 401);
  }
  return parse(r);
}

/** Machine-credential call (event ingestion): authenticates with X-API-Key, never the user's JWT. */
export async function apiWithKey(path, apiKey, opts = {}) {
  const r = await fetch(API + path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  return parse(r);
}

export async function health() {
  try { const r = await fetch('/health'); return { ok: r.ok, ...(await r.json()) }; } catch { return { ok: false }; }
}

/** Plain-language message for an error. */
export function friendly(e) {
  if (!e) return 'Something went wrong.';
  if (e.status === 403) return 'Your role does not have access to this.';
  if (e.status === 503) return 'The service is temporarily unavailable. Please try again in a moment.';
  if (e instanceof TypeError) return 'We could not reach the service. Please check your connection.';
  return e.message || 'Something went wrong.';
}
