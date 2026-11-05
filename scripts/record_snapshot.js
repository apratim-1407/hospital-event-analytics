/* Records every API response the web interface needs, for the static demo (see scripts/build_demo.py).
 *
 * Run it in the browser console of a signed-in session on the running system (http://localhost/app/), with a
 * receiver listening on http://localhost:8765/ that stores the POSTed JSON as demo/snapshot.json.
 * It drives the real interface through every period and department on every page, so what is recorded is exactly
 * what the pages ask for. Progress is in window.__prog; it finishes by posting the snapshot. */
(async () => {
  const API = '/api/v1';
  const rec = {};
  const orig = window.fetch.bind(window);
  window.fetch = async (u, o = {}) => {
    const r = await orig(u, o);
    const url = typeof u === 'string' ? u : u.url;
    if (url.startsWith(API) && (!o.method || o.method === 'GET') && r.ok) {
      try { rec[url.slice(API.length)] = await r.clone().json(); } catch { /* not JSON */ }
    }
    return r;
  };
  const { state, PERIODS } = await import('/app/js/data.js');
  const { api } = await import('/app/js/api.js');
  const { addDays, parseDay, isoDay, STATUS } = await import('/app/js/util.js');
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const depts = ['', ...state.depts.map((d) => String(d.department_id))];
  const routes = ['overview', 'admissions', 'resources', 'anomalies', 'reports'];
  window.__prog = 'running';

  let n = 0;
  const total = PERIODS.length * depts.length;
  for (const [p] of PERIODS) {
    for (const d of depts) {
      state.period = p; state.dept = d;
      for (const rt of routes) { location.hash = '#/' + rt; await wait(1300); }
      window.__prog = `${++n}/${total}`;
    }
  }
  /* heatmap for every department, jobs filters, settings pages */
  const last = state.range.last, start = isoDay(addDays(parseDay(last), -13));
  for (const d of state.depts) await api(`/analytics/resources?start_date=${start}&end_date=${last}&department_id=${d.department_id}&limit=2000`);
  await api('/jobs?limit=20');
  for (const s of Object.keys(STATUS)) await api(`/jobs?limit=20&status=${s}`).catch(() => {});
  for (const rt of ['jobs', 'ingestion']) { location.hash = '#/' + rt; await wait(2500); }

  window.__prog = 'posting';
  await orig('http://localhost:8765/save', { method: 'POST', mode: 'no-cors', headers: { 'Content-Type': 'text/plain' }, body: JSON.stringify(rec) });
  window.__prog = `done ${Object.keys(rec).length} responses`;
})();
