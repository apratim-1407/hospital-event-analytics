/* Hospital Activity: a plain-language view of the analytics API.
   No framework, no build step, no external requests. Reads only the public REST API. */
(() => {
  'use strict';

  const API = '/api/v1';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  /* ───────────────────────── formatting: words, not codes ───────────────────────── */
  const nf = new Intl.NumberFormat('en-GB');
  const dFull = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  const dWeek = new Intl.DateTimeFormat('en-GB', { weekday: 'long', day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  const dShort = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  const dMonth = new Intl.DateTimeFormat('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' });

  const parseDay = (s) => new Date(s + 'T00:00:00Z');
  const isoDay = (d) => d.toISOString().slice(0, 10);
  const addDays = (d, n) => new Date(d.getTime() + n * 86400000);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pct = (v) => `${Math.round(v * 100)}%`;
  const n0 = (v) => nf.format(Math.round(v));

  function hourWord(h) {
    h = ((h % 24) + 24) % 24;
    if (h === 0) return 'midnight';
    if (h === 12) return 'noon';
    return h < 12 ? `${h} am` : `${h - 12} pm`;
  }
  function stayWord(hours) {
    if (hours == null) return null;
    return hours < 48 ? `${Math.max(1, Math.round(hours))} hours` : `${(hours / 24).toFixed(1)} days`;
  }
  function compare(obs, exp) {
    if (!exp || exp <= 0) return '';
    const r = obs / exp;
    if (r >= 2) return `about ${r.toFixed(1)} times the usual`;
    if (r > 1.03) return `about ${Math.round((r - 1) * 100)}% above the usual`;
    if (r < 0.97) return `about ${Math.round((1 - r) * 100)}% below the usual`;
    return 'close to the usual';
  }

  /* ───────────────────────── session ───────────────────────── */
  const mem = {};
  const store = {
    get(k) { try { return sessionStorage.getItem(k); } catch { return mem[k] ?? null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch { mem[k] = v; } },
    del(k) { try { sessionStorage.removeItem(k); } catch { delete mem[k]; } },
  };
  const state = {
    access: store.get('access'), refresh: store.get('refresh'), email: store.get('email'), role: store.get('role'),
    depts: [], range: null, period: 'year', deptId: '', filter: 'serious', shown: 6, anomalies: [],
  };
  const tick = { summary: 0, pattern: 0, notes: 0 }; // ignore answers that arrive after a newer question

  function setSession(tokens, email) {
    state.access = tokens.access_token;
    state.refresh = tokens.refresh_token;
    store.set('access', state.access);
    store.set('refresh', state.refresh);
    try {
      const payload = JSON.parse(atob(state.access.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
      state.role = payload.role;
      store.set('role', state.role);
    } catch { /* role stays as it was */ }
    if (email) { state.email = email; store.set('email', email); }
  }
  function clearSession() {
    ['access', 'refresh', 'email', 'role'].forEach(store.del);
    state.access = state.refresh = state.email = state.role = null;
  }

  async function refreshTokens() {
    if (!state.refresh) return false;
    try {
      const r = await fetch(`${API}/auth/refresh`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: state.refresh }) });
      if (!r.ok) return false;
      setSession(await r.json());
      return true;
    } catch { return false; }
  }

  async function api(path, opts = {}, retry = true) {
    const headers = { ...(opts.headers || {}), Authorization: `Bearer ${state.access}` };
    if (opts.body) headers['Content-Type'] = 'application/json';
    const r = await fetch(API + path, { ...opts, headers });
    if (r.status === 401) {
      if (retry && (await refreshTokens())) return api(path, opts, false);
      signOut();
      throw Object.assign(new Error('Your session has ended. Please sign in again.'), { signedOut: true });
    }
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw Object.assign(new Error(body?.error?.message || `Something went wrong (${r.status}).`), { status: r.status });
    }
    return r.json();
  }

  /* ───────────────────────── sign in / out ───────────────────────── */
  function showLogin() {
    $('#app-view').hidden = true;
    $('#login-view').hidden = false;
    $('#email').focus();
  }
  function signOut() {
    clearSession();
    showLogin();
  }

  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const note = $('#login-error');
    note.hidden = true;
    const email = $('#email').value.trim();
    try {
      const r = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password: $('#password').value }) });
      if (r.status === 401) throw new Error('That email and password do not match. Please try again.');
      if (!r.ok) throw new Error('The service is busy right now. Please try again in a moment.');
      setSession(await r.json(), email);
      $('#password').value = '';
      start();
    } catch (err) {
      note.textContent = err.message.startsWith('That') || err.message.startsWith('The service') ? err.message : 'We could not reach the service. Please check your connection.';
      note.hidden = false;
    }
  });
  $('#signout').addEventListener('click', signOut);

  /* ───────────────────────── period and department ───────────────────────── */
  const PERIODS = { month: 30, quarter: 91, year: 365, all: null };

  function window_() {
    const first = parseDay(state.range.first), last = parseDay(state.range.last);
    const days = PERIODS[state.period];
    const start = days ? new Date(Math.max(first, addDays(last, -(days - 1)))) : first;
    return { start: isoDay(start), end: isoDay(last) };
  }
  const deptQuery = () => (state.deptId ? `&department_id=${state.deptId}` : '');
  const deptName = (id) => state.depts.find((d) => d.department_id === id)?.name ?? `Department ${id}`;

  $('#period').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-period]');
    if (!b) return;
    state.period = b.dataset.period;
    state.shown = 6;
    $$('#period button').forEach((x) => x.classList.toggle('on', x === b));
    refreshAll();
  });
  $('#dept').addEventListener('change', (e) => {
    state.deptId = e.target.value;
    state.shown = 6;
    refreshAll();
  });

  /* ───────────────────────── charts (hand-drawn SVG) ───────────────────────── */
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = 10 ** Math.floor(Math.log10(v));
    const m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
  }

  function lineChart(host, series, o) {
    const pts = series.flatMap((s) => s.points.filter((p) => p.y != null));
    if (!pts.length) { host.innerHTML = '<p class="empty">Nothing to show for this period yet.</p>'; return; }
    const W = 760, H = 250, L = 46, R = 10, T = 14, B = 30;
    const x0 = Math.min(...pts.map((p) => p.x)), x1 = Math.max(...pts.map((p) => p.x));
    const yMin = o.yMin ?? 0;
    const yMax = o.yMax ?? niceMax(Math.max(...pts.map((p) => p.y)) * 1.06);
    const X = (x) => L + (x1 === x0 ? 0.5 : (x - x0) / (x1 - x0)) * (W - L - R);
    const Y = (y) => T + (1 - (y - yMin) / (yMax - yMin)) * (H - T - B);
    const gid = 'g' + Math.random().toString(36).slice(2, 8);

    const grid = [0, 1, 2, 3, 4].map((i) => {
      const v = yMin + ((yMax - yMin) * i) / 4;
      return `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/><text class="tick" x="${L - 10}" y="${Y(v) + 4}" text-anchor="end">${o.yFmt(v)}</text>`;
    }).join('');
    const xt = o.xTicks ?? [0, 1, 2, 3, 4].map((i) => x0 + ((x1 - x0) * i) / 4);
    const xl = xt.map((x, i) => `<text class="tick" x="${X(x)}" y="${H - 8}" text-anchor="${i === 0 ? 'start' : i === xt.length - 1 ? 'end' : 'middle'}">${o.xFmt(x)}</text>`).join('');

    const paths = series.map((s) => {
      let d = '', pen = false;
      s.points.forEach((p) => {
        if (p.y == null) { pen = false; return; }
        d += `${pen ? 'L' : 'M'}${X(p.x).toFixed(1)} ${Y(p.y).toFixed(1)} `;
        pen = true;
      });
      const valid = s.points.filter((p) => p.y != null);
      const area = s.area && valid.length > 1
        ? `<path d="${d}L${X(valid[valid.length - 1].x)} ${Y(yMin)} L${X(valid[0].x)} ${Y(yMin)} Z" fill="url(#${gid})"/>` : '';
      return `${area}<path class="line" d="${d}" stroke="${s.color}" ${s.dash ? 'stroke-dasharray="6 6"' : ''}/>`;
    }).join('');

    host.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(o.label)}">
        <defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0e7c86" stop-opacity=".16"/><stop offset="1" stop-color="#0e7c86" stop-opacity="0"/></linearGradient></defs>
        ${grid}${xl}${paths}
        <line class="guide" y1="${T}" y2="${H - B}" x1="0" x2="0" style="display:none"/>
        <g class="dots"></g>
      </svg><div class="tip"></div>`;

    const svg = $('svg', host), tip = $('.tip', host), guide = $('.guide', svg), dots = $('.dots', svg);
    const nearest = (s, x) => {
      let best = null, bd = Infinity;
      for (const p of s.points) { if (p.y == null) continue; const dd = Math.abs(p.x - x); if (dd < bd) { bd = dd; best = p; } }
      return best;
    };
    svg.addEventListener('pointermove', (e) => {
      const rect = svg.getBoundingClientRect();
      const px = ((e.clientX - rect.left) / rect.width) * W;
      const xv = x0 + ((px - L) / (W - L - R)) * (x1 - x0);
      const hits = series.map((s) => ({ s, p: nearest(s, xv) })).filter((h) => h.p);
      if (!hits.length) return;
      const lead = hits[0].p;
      guide.setAttribute('x1', X(lead.x)); guide.setAttribute('x2', X(lead.x)); guide.style.display = '';
      dots.innerHTML = hits.map((h) => `<circle cx="${X(h.p.x)}" cy="${Y(h.p.y)}" r="4" fill="#fff" stroke="${h.s.color}" stroke-width="2"/>`).join('');
      tip.innerHTML = `<b>${esc(o.tipTitle(lead.x))}</b>${hits.map((h) => `${series.length > 1 ? esc(h.s.name) + ': ' : ''}${esc(o.yFmt(h.p.y, true))}`).join('<br>')}`;
      tip.style.left = `${(X(lead.x) / W) * 100}%`;
      tip.style.top = `${(Y(Math.max(...hits.map((h) => h.p.y))) / H) * rect.height}px`;
      tip.classList.add('on');
    });
    svg.addEventListener('pointerleave', () => { tip.classList.remove('on'); guide.style.display = 'none'; dots.innerHTML = ''; });
  }

  function bars(ul, rows) {
    ul.innerHTML = rows.map((r) => `
      <li>
        <span class="name">${esc(r.name)}</span>
        <span class="track"><i class="${r.warm ? 'warm' : ''}" style="width:0%" data-w="${r.width}"></i>${r.marker != null ? `<em style="left:${Math.min(r.marker, 1) * 100}%"></em>` : ''}</span>
        <span class="num">${esc(r.value)}</span>
        ${r.sub ? `<small>${esc(r.sub)}</small>` : ''}
      </li>`).join('');
    requestAnimationFrame(() => $$('i[data-w]', ul).forEach((i) => { i.style.width = `${i.dataset.w}%`; }));
  }

  /* ───────────────────────── sections ───────────────────────── */
  const fail = (host, msg = 'We could not load this just now. Please try again in a moment.') => { host.innerHTML = `<p class="empty">${esc(msg)}</p>`; };

  async function loadSummary() {
    const id = ++tick.summary;
    const { start, end } = window_();
    let s;
    try { s = await api(`/overview/summary?start_date=${start}&end_date=${end}${deptQuery()}`); }
    catch (e) { if (!e.signedOut) { $('#lede').textContent = 'We could not load the overview just now.'; fail($('#trend')); } return; }
    if (id !== tick.summary) return;

    const when = `${dFull.format(parseDay(s.start))} and ${dFull.format(parseDay(s.end))}`;
    const stay = stayWord(s.totals.avg_stay_hours);
    const lede = $('#lede');
    if (state.deptId) {
      lede.innerHTML = `Between ${when}, <b>${esc(deptName(+state.deptId))}</b> admitted <b>${n0(s.totals.admissions)} patients</b>${stay ? `, who stayed about ${stay} on average` : ''}.`;
    } else {
      lede.innerHTML = `Between ${when}, the hospital admitted <b>${n0(s.totals.admissions)} patients</b>. ${esc(s.busiest_department ?? 'One department')} was the busiest${s.fullest_department && s.fullest_department !== s.busiest_department ? `, and ${esc(s.fullest_department)} ran the fullest beds` : ''}.`;
    }

    const withBeds = s.departments.filter((d) => d.bed_use_avg != null);
    const cap = withBeds.reduce((a, d) => a + d.bed_capacity, 0);
    const bedUse = cap ? withBeds.reduce((a, d) => a + d.bed_use_avg * d.bed_capacity, 0) / cap : null;
    $('#figures').innerHTML = [
      [n0(s.totals.admissions), 'patients admitted', ''],
      [stay ?? '—', 'typical stay', ''],
      [s.busiest_day ? dShort.format(parseDay(s.busiest_day.date)) : '—', 'busiest day', s.busiest_day ? `${n0(s.busiest_day.admissions)} admissions` : ''],
      [bedUse != null ? pct(bedUse) : '—', 'of beds in use', 'on average'],
    ].map(([v, l, sm]) => `<div class="fig"><dd>${esc(v)}${sm ? `<small>${esc(sm)}</small>` : ''}</dd><dt>${esc(l)}</dt></div>`).join('');

    const unit = { day: 'day', week: 'week', month: 'month' }[s.granularity];
    $('#trend-note').textContent = `New patients admitted, one point for each ${unit}.`;
    const xFmt = (x) => (s.granularity === 'month' || s.days > 200 ? dMonth.format(new Date(x)) : dShort.format(new Date(x)));
    lineChart($('#trend'), [{ name: 'Admissions', color: '#0e7c86', area: true, points: s.trend.map((p) => ({ x: parseDay(p.period).getTime(), y: p.admissions })) }], {
      yFmt: (v, tip) => (tip ? `${n0(v)} patients` : nf.format(Math.round(v))),
      xFmt,
      tipTitle: (x) => (s.granularity === 'week' ? `Week of ${dShort.format(new Date(x))}` : s.granularity === 'month' ? dMonth.format(new Date(x)) : dWeek.format(new Date(x))),
      label: 'Admissions over time',
    });

    const rows = [...s.departments].sort((a, b) => b.admissions - a.admissions);
    const top = rows[0]?.admissions || 1;
    bars($('#dept-bars'), rows.map((d) => ({
      name: d.name, width: (d.admissions / top) * 100, value: `${n0(d.admissions)} patients`,
      sub: [d.avg_stay_hours != null ? `Patients stay about ${stayWord(d.avg_stay_hours)}` : null, d.busiest_hour != null ? `most arrive around ${hourWord(d.busiest_hour)}` : null].filter(Boolean).join(' · '),
    })));

    const byUse = [...withBeds].sort((a, b) => b.bed_use_avg - a.bed_use_avg);
    bars($('#bed-bars'), byUse.map((d) => ({
      name: d.name, width: Math.min(d.bed_use_avg, 1) * 100, value: `${pct(d.bed_use_avg)} in use`, warm: d.bed_use_avg >= 0.85, marker: d.bed_use_peak,
      sub: d.bed_use_peak > 1 ? 'At its busiest there were more patients than beds.' : `At its busiest, ${pct(d.bed_use_peak)} of beds were in use${d.hours_over_90 ? ` · over 90% full for ${n0(d.hours_over_90)} hours` : ''}.`,
    })));
  }

  async function loadPattern() {
    const id = ++tick.pattern;
    const { start, end } = window_();
    let p;
    try { p = await api(`/overview/weekly-pattern?start_date=${start}&end_date=${end}${deptQuery()}`); }
    catch (e) { if (!e.signedOut) { fail($('#pattern')); $('#pattern-note').textContent = ''; } return; }
    if (id !== tick.pattern) return;

    const mk = (arr) => arr.map((y, h) => ({ x: h, y }));
    const hi = p.weekday.reduce((b, v, h) => (v != null && v > (p.weekday[b] ?? -1) ? h : b), 0);
    const avg = (a) => { const v = a.filter((x) => x != null); return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null; };
    const aw = avg(p.weekday), ae = avg(p.weekend);
    let note = p.weekday[hi] != null ? `On weekdays, beds are fullest around ${hourWord(hi)}, when about ${pct(p.weekday[hi])} are in use.` : '';
    if (aw != null && ae != null) {
      const diff = Math.round((ae - aw) * 100);
      note += Math.abs(diff) < 2 ? ' Weekends look much the same.' : ` Weekends are ${Math.abs(diff)} points ${diff < 0 ? 'quieter' : 'busier'} on average.`;
    }
    $('#pattern-note').textContent = note;
    const vals = [...p.weekday, ...p.weekend].filter((v) => v != null);
    const zoomLo = Math.max(0, Math.floor((Math.min(...vals) - 0.04) / 0.05) * 0.05);
    let zoomHi = Math.ceil((Math.max(...vals) + 0.04) / 0.05) * 0.05;
    if (zoomHi - zoomLo < 0.2) zoomHi = zoomLo + 0.2;
    $('#pattern-note').textContent += ' The scale is zoomed in so small differences are easy to see.';
    lineChart($('#pattern'), [
      { name: 'Weekdays', color: '#0e7c86', area: true, points: mk(p.weekday) },
      { name: 'Weekends', color: '#98a2b3', dash: true, points: mk(p.weekend) },
    ], {
      yMin: zoomLo, yMax: zoomHi, yFmt: (v) => pct(v), xFmt: hourWord, xTicks: [0, 6, 12, 18, 23],
      tipTitle: (x) => hourWord(Math.round(x)), label: 'Share of beds in use by hour of day, weekdays and weekends',
    });
  }

  const SEVERITY = { critical: ['urgent', 'Urgent', 3], high: ['important', 'Important', 2], moderate: ['', 'Worth a look', 1] };

  function describe(a) {
    const s = new Date(a.window_start), e = new Date(a.window_end);
    const daily = a.metric_name === 'daily_admissions';
    let when;
    if (daily) {
      const last = addDays(e, -1);
      when = last <= s ? dWeek.format(s) : `${dShort.format(s)} to ${dFull.format(last)}`;
    } else {
      const sameDay = isoDay(s) === isoDay(new Date(e.getTime() - 1));
      when = sameDay ? `${dWeek.format(s)}, ${hourWord(s.getUTCHours())} to ${hourWord(e.getUTCHours())}`
        : `${dShort.format(s)}, ${hourWord(s.getUTCHours())} to ${dFull.format(e)}, ${hourWord(e.getUTCHours())}`;
    }
    const high = a.z_score >= 0, o = a.observed_value, x = a.expected_value;
    let why;
    if (a.metric_name === 'daily_admissions') {
      why = high ? `About ${n0(o)} patients a day were admitted, ${compare(o, x)} (${n0(x)} is typical).`
        : `Only about ${n0(o)} patients a day were admitted, where ${n0(x)} is typical. An unusually quiet stretch.`;
    } else if (a.metric_name === 'hourly_admissions') {
      const few = (v) => (v < 1.5 ? 'one or none' : `about ${Math.round(v)}`);
      why = high ? `About ${Math.round(o)} patients arrived in that hour, where ${few(x)} would be usual.`
        : `Only ${few(o)} patients arrived in that hour, where about ${Math.round(x)} would be usual.`;
    } else {
      why = high ? `Beds were ${pct(o)} in use on average, where ${pct(x)} is typical.${o >= 1 ? ' That is more patients than beds.' : ''}`
        : `Only ${pct(o)} of beds were in use, where ${pct(x)} is typical.`;
    }
    return { who: deptName(a.department_id), when, why, sev: SEVERITY[a.severity_label] ?? SEVERITY.moderate };
  }

  function renderNotes() {
    const list = state.anomalies
      .filter((a) => (state.filter === 'serious' ? a.severity_label !== 'moderate' : true))
      .sort((a, b) => (SEVERITY[b.severity_label]?.[2] ?? 0) - (SEVERITY[a.severity_label]?.[2] ?? 0) || Math.abs(b.z_score) - Math.abs(a.z_score));
    const ul = $('#notes'), more = $('#more');
    if (!list.length) {
      ul.innerHTML = `<li class="empty" style="display:block;border:0">${state.filter === 'serious' ? 'Nothing urgent stood out in this period. You can switch to “Everything unusual” to see smaller things.' : 'Nothing unusual in this period.'}</li>`;
      more.hidden = true;
      return;
    }
    ul.innerHTML = list.slice(0, state.shown).map((a) => {
      const d = describe(a);
      return `<li class="${d.sev[0]}"><span class="dot"></span><div><p class="what">${esc(d.who)} <span>· ${esc(d.when)}</span></p><p class="why">${esc(d.why)}</p></div><span class="tag">${d.sev[1]}</span></li>`;
    }).join('');
    const rest = list.length - state.shown;
    more.hidden = rest <= 0;
    more.textContent = `Show ${Math.min(rest, 10)} more`;
  }

  async function loadNotes() {
    const id = ++tick.notes;
    const { start, end } = window_();
    try {
      const r = await api(`/analytics/anomalies?start=${start}T00:00:00Z&end=${isoDay(addDays(parseDay(end), 1))}T00:00:00Z&limit=1000${deptQuery()}`);
      if (id !== tick.notes) return;
      state.anomalies = r.data;
      renderNotes();
    } catch (e) { if (!e.signedOut) fail($('#notes')); }
  }
  $('#att-filter').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-filter]');
    if (!b) return;
    state.filter = b.dataset.filter; state.shown = 6;
    $$('#att-filter button').forEach((x) => x.classList.toggle('on', x === b));
    renderNotes();
  });
  $('#more').addEventListener('click', () => { state.shown += 10; renderNotes(); });

  /* ───────────────────────── run an analysis ───────────────────────── */
  function setupRunForm() {
    const canRun = state.role === 'analyst' || state.role === 'admin';
    $('#run-form').hidden = !canRun;
    $('#run-intro').hidden = !canRun;
    $('#run-viewer').hidden = canRun;
    if (!state.range) return;
    const last = parseDay(state.range.last);
    $('#run-to').value = state.range.last;
    $('#run-from').value = isoDay(new Date(Math.max(parseDay(state.range.first), addDays(last, -29))));
  }

  function friendlyError(e) {
    if (e.status === 403) return 'Your role cannot start an analysis. Please ask an analyst.';
    if (e.status === 503) return 'The analysis service is not reachable right now. New events are still being recorded safely. Please try again in a minute.';
    return e.message;
  }

  $('#run-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const go = $('#run-go'), box = $('#progress'), text = $('#progress-text'), fill = $('#progress-fill');
    go.disabled = true; box.hidden = false; fill.style.width = '0%'; text.textContent = 'Starting…';
    try {
      const body = { job_type: $('#run-type').value, start_date: $('#run-from').value, end_date: $('#run-to').value };
      const accepted = await api('/jobs', { method: 'POST', body: JSON.stringify(body) });
      const t0 = Date.now();
      for (;;) {
        await new Promise((r) => setTimeout(r, 700));
        const j = await api(`/jobs/${accepted.job_id}`);
        const done = j.partitions_completed, total = j.partitions_total;
        if (j.status === 'queued') text.textContent = 'Waiting for a free worker…';
        else if (j.status === 'running') {
          const left = j.progress_pct > 5 ? Math.max(1, Math.round(((Date.now() - t0) / 1000) * (100 - j.progress_pct) / j.progress_pct)) : null;
          text.textContent = `Working through ${n0(done)} of ${n0(total)} parts (${Math.round(j.progress_pct)}%)${left ? `, about ${left} seconds to go` : ''}.`;
          fill.style.width = `${j.progress_pct}%`;
        } else {
          fill.style.width = '100%';
          if (j.status === 'completed') text.textContent = total === 0 ? 'There were no events in those dates, so there is nothing new to show.' : `Done in ${j.duration_seconds} seconds. We looked at ${n0(j.events_processed ?? 0)} events and refreshed everything above.`;
          else if (j.status === 'partial') text.textContent = 'Finished, but a few parts could not be read, so some results are left out. Starting it again should fix that.';
          else text.textContent = 'This analysis could not be completed. Nothing has been changed. Please try again shortly.';
          if (j.status !== 'failed') { await loadDepartments(); refreshAll(); loadHealth(); }
          break;
        }
      }
    } catch (err) {
      if (!err.signedOut) text.textContent = friendlyError(err);
    } finally { go.disabled = false; }
  });

  /* ───────────────────────── behind the scenes (admins) ───────────────────────── */
  async function loadHealth() {
    if (state.role !== 'admin') return;
    $('#behind').hidden = false; $('#nav-behind').hidden = false;
    let m;
    try { m = await api('/metrics'); } catch { return; }
    const items = [];
    const last = m.M1_job_processing_time.recent_jobs[0];
    if (last && last.duration_seconds) {
      items.push(`The last analysis worked through <b>${n0(last.events_processed ?? 0)} events in ${last.duration_seconds} seconds</b>, split into ${n0(last.partitions_total)} parts shared between the workers.`);
    }
    const lat = m.M3_api_latency_ms;
    const key = Object.keys(lat).find((k) => k.includes('/overview/summary')) ?? Object.keys(lat).find((k) => k.includes('/analytics/'));
    if (key) items.push(`Pages like this one are usually ready in about <b>${Math.round(lat[key].p50)} milliseconds</b>; nineteen times out of twenty, within ${Math.round(lat[key].p95)}.`);
    if (m.M4_cache.hit_ratio != null) items.push(`<b>${pct(m.M4_cache.hit_ratio)}</b> of questions were answered from a recent copy, which keeps the database free for new events.`);
    if (m.M2_ingestion_throughput.single_events.available) items.push(`In the last five minutes, <b>${n0(m.M2_ingestion_throughput.single_events.events)} new events</b> were recorded.`);
    $('#health').innerHTML = items.map((t) => `<li>${t}</li>`).join('') || '<li class="empty">No measurements yet.</li>';
  }

  /* ───────────────────────── start-up ───────────────────────── */
  async function loadDepartments() {
    const d = await api('/overview/departments');
    state.depts = d.departments;
    state.range = d.analysed_range;
    const sel = $('#dept'), keep = sel.value;
    sel.innerHTML = '<option value="">All departments</option>' + d.departments.map((x) => `<option value="${x.department_id}">${esc(x.name)}</option>`).join('');
    sel.value = keep;
  }

  function refreshAll() {
    if (!state.range) return;
    loadSummary(); loadPattern(); loadNotes();
  }

  function highlightNav() {
    const links = $$('.top nav a');
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) links.forEach((a) => a.classList.toggle('on', a.getAttribute('href') === `#${en.target.id}`)); });
    }, { rootMargin: '-40% 0px -55% 0px' });
    $$('.page > section').forEach((s) => io.observe(s));
  }

  async function start() {
    $('#login-view').hidden = true;
    $('#app-view').hidden = false;
    const roleWord = { viewer: 'Viewer', analyst: 'Analyst', admin: 'Administrator' }[state.role] ?? '';
    $('#who').textContent = [state.email, roleWord].filter(Boolean).join(' · ');
    try { await loadDepartments(); } catch (e) { if (!e.signedOut) $('#lede').textContent = 'We could not reach the service. Please try again in a moment.'; return; }
    setupRunForm();
    if (!state.range) {
      $('#lede').innerHTML = 'There are no results to show yet. Use <b>Refresh</b> below to look at the events and build the first picture.';
      $('#figures').innerHTML = '';
      return;
    }
    refreshAll();
    loadHealth();
  }

  highlightNav();
  if (state.access) start(); else showLogin();
})();
