/* Application shell: sign-in, sidebar, hash router, health indicator. */
import { session, login, clearSession, health, friendly, isAnalyst } from './api.js';
import { $, $$, esc, icon, callout } from './util.js';
import { loadDepartments } from './data.js';

import overview from './pages/overview.js';
import admissions from './pages/admissions.js';
import resources from './pages/resources.js';
import anomalies from './pages/anomalies.js';
import jobs from './pages/jobs.js';
import ingestion from './pages/ingestion.js';
import reports from './pages/reports.js';

/* [id, title, page, section]: Analyses and Integrations are reached through Settings, not the main menu. */
const ROUTES = [
  ['overview', 'Overview', overview], ['admissions', 'Admission patterns', admissions], ['resources', 'Resource utilisation', resources],
  ['anomalies', 'Unusual workloads', anomalies], ['reports', 'Reports', reports],
  ['jobs', 'Settings · Analyses', jobs, 'settings'], ['ingestion', 'Settings · Integrations', ingestion, 'settings'],
];
const MENU = ROUTES.filter((r) => !r[3]);
const ROLE = { viewer: 'Viewer', analyst: 'Analyst', admin: 'Administrator' };

let token = 0;       // identifies the latest navigation; stale renders stop themselves
let cleanup = null;  // returned by a page to stop timers when leaving it
let booted = false;

const show = (id) => { $('#signin').hidden = id !== 'signin'; $('#shell').hidden = id !== 'shell'; };

/* ── sign in / out ── */
function showSignin() { booted = false; show('signin'); $('#email').focus(); }
function signOut() { cleanup?.(); cleanup = null; clearSession(); location.hash = ''; showSignin(); }

$('#signin-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const note = $('#signin-error'), btn = $('#signin-go');
  note.hidden = true; btn.disabled = true;
  try { await login($('#email').value.trim(), $('#password').value); $('#password').value = ''; await boot(); }
  catch (err) { note.textContent = err.status === 401 || err.status >= 500 ? err.message : friendly(err); note.hidden = false; }
  finally { btn.disabled = false; }
});
$('#signout').addEventListener('click', signOut);
window.addEventListener('signedout', signOut);

/* ── routing ── */
function renderRoute() {
  const id = (location.hash.replace(/^#\/?/, '') || 'overview').split('?')[0];
  const route = ROUTES.find((r) => r[0] === id) ?? ROUTES[0];
  const my = ++token;
  cleanup?.(); cleanup = null;
  const here = route[3] === 'settings' ? '#/jobs' : `#/${route[0]}`;
  $$('#nav a').forEach((a) => (a.getAttribute('href') === here ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
  document.title = `${route[1]} · Hospital Event Analytics`;
  $('#topbar-title').textContent = route[1];
  document.body.classList.remove('menu');
  const root = $('#page');
  const ctx = { current: () => my === token, rerender: () => my === token && renderInto(route, root, my) };
  renderInto(route, root, my, ctx);
  window.scrollTo(0, 0);
}
async function renderInto(route, root, my, ctx) {
  ctx ??= { current: () => my === token, rerender: () => my === token && renderInto(route, root, my, ctx) };
  cleanup?.(); cleanup = null;
  try {
    const result = await route[2].render(root, ctx);
    if (typeof result === 'function') { if (my === token) cleanup = result; else result(); }
  } catch (err) {
    if (my === token) root.innerHTML = `<div style="max-width:36rem">${callout('bad', `Something went wrong while loading this page. ${esc(friendly(err))}`)}</div>`;
    console.error(err);
  }
}
window.addEventListener('hashchange', () => booted && renderRoute());

/* ── sidebar + health ── */
function buildNav() {
  const settings = isAnalyst() ? `<div class="gap"></div><a href="#/jobs">${icon('settings')}<span>Settings</span></a>` : '';
  $('#nav').innerHTML = MENU.map(([id, label]) => `<a href="#/${id}">${icon(id)}<span>${label}</span></a>`).join('') + settings;
}
async function pollHealth() {
  const h = await health();
  const el = $('#status');
  el.className = `status ${!h.ok ? 'bad' : h.redis === false ? 'warn' : 'ok'}`;
  $('#status-text').textContent = !h.ok ? 'Service unavailable' : h.redis === false ? 'Running with reduced features' : 'All systems normal';
}
$('#menu').addEventListener('click', () => document.body.classList.toggle('menu'));
document.addEventListener('click', (e) => { if (document.body.classList.contains('menu') && !e.target.closest('#side') && !e.target.closest('#menu')) document.body.classList.remove('menu'); });

/* ── start ── */
async function boot() {
  show('shell');
  buildNav();
  $('#who-email').textContent = session.email ?? '';
  $('#who-role').textContent = ROLE[session.role] ?? '';
  const root = $('#page');
  try { await loadDepartments(); }
  catch (err) { if (err.status === 401) return; root.innerHTML = `<div style="max-width:36rem">${callout('bad', `We could not reach the service. ${esc(friendly(err))}`)}</div>`; return; }
  booted = true;
  renderRoute();
}

buildNav();
pollHealth(); setInterval(pollHealth, 30000);
if (session.access) boot(); else showSignin();
