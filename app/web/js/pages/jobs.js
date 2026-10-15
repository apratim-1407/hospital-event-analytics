import { api, isAnalyst, friendly } from '../api.js';
import { $, $$, esc, n0, badge, dFull, dDayTime, ago, duration, isoDay, addDays, STATUS, JOB_TYPE, skRows, emptyState, callout, icon, toast } from '../util.js';
import { state, deptName } from '../data.js';
import { head } from '../ui.js';

const tone = { running: 'run', completed: 'completed', failed: 'failed', partial: 'partial', queued: 'queued', cancelled: 'cancelled' };
const resultsPage = { admission_patterns: '#/admissions', resource_utilisation: '#/resources', anomaly_detection: '#/anomalies', full_analysis: '#/overview' };
const isActive = (j) => j.status === 'queued' || j.status === 'running';

export default {
  async render(root, ctx) {
    if (!isAnalyst()) {
      root.innerHTML = head('Analytics jobs', 'Analyses that run in the background across the worker pool.') + emptyState({ title: 'Available to analysts', text: 'Your role can read every result, but starting and managing analyses is for analysts and administrators.', ico: 'jobs' });
      return;
    }
    const L = { jobs: [], next: null, status: '', open: new Set(), timer: null };
    const today = isoDay(new Date());
    const defTo = state.range?.last ?? today;
    const defFrom = isoDay(addDays(new Date(defTo + 'T00:00:00Z'), -29));

    root.innerHTML = head('Analytics jobs', 'Analyses run in the background across the worker pool, so you can keep working while they finish.', `
      <label class="sr" for="jb-status">Status</label>
      <select id="jb-status"><option value="">All statuses</option>${Object.entries(STATUS).map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select>
      <button class="btn primary" id="jb-new-btn" aria-expanded="false" aria-controls="jb-new">${icon('plus')} New analysis</button>`) + `
      <section class="section" id="jb-new" hidden>
        <div class="sec-head"><h2>New analysis</h2><p>Choose what to work out and the dates to cover.</p></div>
        <form class="form" id="jb-form">
          <label class="field"><span>What to work out</span><select id="jb-type">${Object.entries(JOB_TYPE).map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select></label>
          <label class="field"><span>From</span><input type="date" id="jb-from" value="${defFrom}" required></label>
          <label class="field"><span>To</span><input type="date" id="jb-to" value="${defTo}" required></label>
          <label class="field"><span>Department</span><select id="jb-dept"><option value="">All departments</option>${state.depts.map((d) => `<option value="${d.department_id}">${esc(d.name)}</option>`).join('')}</select></label>
          <button class="btn primary" id="jb-go" type="submit">Start analysis</button>
          <button class="btn" id="jb-cancel" type="button">Cancel</button>
        </form>
        <div id="jb-msg" style="margin-top:1rem" aria-live="polite"></div>
      </section>
      <section class="section"><div class="tbl-wrap" id="jb-list">${skRows(6)}</div><p style="margin-top:1rem"><button class="btn" id="jb-more" hidden>Load more</button></p></section>`;

    const row = (j) => {
      const open = L.open.has(j.job_id);
      const pc = Math.round(j.progress_pct);
      const prog = j.status === 'running' ? `<div class="prog"><div class="bar"><i style="width:${pc}%"></i></div><span class="pc num">${pc}% · ${n0(j.partitions_completed)} of ${n0(j.partitions_total)}</span></div>`
        : j.status === 'queued' ? '<span class="slate">Waiting for a worker</span>'
        : j.status === 'completed' ? '<div class="prog done"><div class="bar"><i style="width:100%"></i></div><span class="pc num">Done</span></div>' : '<span class="slate">—</span>';
      const p = j.params;
      let r = `<tr class="click" tabindex="0" role="button" aria-expanded="${open}" data-id="${j.job_id}">
        <td class="strong">${esc(JOB_TYPE[j.job_type] ?? j.job_type)}<span class="sub">${esc(dFull(p.start_date))} – ${esc(dFull(p.end_date))}</span></td>
        <td>${badge(tone[j.status], STATUS[j.status])}</td><td>${prog}</td>
        <td class="slate">${esc(ago(j.created_at))}</td><td class="r num">${esc(duration(j.duration_seconds))}</td>
        <td class="r num">${j.events_processed != null ? n0(j.events_processed) : '—'}</td><td class="r"><span class="chev">${icon('chevron')}</span></td></tr>`;
      if (open) {
        const e = j.error_detail;
        const depts = (p.departments ?? []).length === state.depts.length || !(p.departments ?? []).length ? 'All departments' : p.departments.map(deptName).join(', ');
        r += `<tr class="detail"><td colspan="7"><dl class="facts">
          <div><dt>Job</dt><dd class="mono">${esc(j.job_id)}</dd></div>
          <div><dt>Departments</dt><dd>${esc(depts)}</dd></div>
          <div><dt>Submitted</dt><dd>${esc(dDayTime(j.created_at))} UTC</dd></div>
          <div><dt>Finished</dt><dd>${j.finished_at ? esc(dDayTime(j.finished_at)) + ' UTC' : '—'}</dd></div>
          <div><dt>Parts processed</dt><dd class="num">${n0(j.partitions_completed)} of ${n0(j.partitions_total)}</dd></div></dl>
          ${e ? `<div style="margin-top:1rem">${callout(j.status === 'cancelled' ? 'info' : 'bad', `<b>${esc(e.code === 'partitions_failed' ? `${n0(e.failed_partitions)} of ${n0(e.total_partitions)} parts could not be read` : e.message || e.code)}.</b> ${e.code === 'partitions_failed' ? 'Results for the affected departments were withheld rather than reported wrongly. Running it again should fix this.' : ''}${e.sample?.length ? `<details style="margin-top:.4rem"><summary>Details</summary><span class="mono" style="font-size:12px">${e.sample.slice(0, 3).map((s) => `${esc(s.partition)}: ${esc(String(s.error).split('\n')[0].slice(0, 110))}`).join('<br>')}</span></details>` : ''}`)}</div>` : ''}
          <div class="actions">
            ${isActive(j) ? `<button class="btn sm danger" data-act="cancel" data-id="${j.job_id}">Cancel job</button>` : ''}
            <button class="btn sm" data-act="rerun" data-id="${j.job_id}">${icon('refresh', 14)} Run again</button>
            ${j.status === 'completed' || j.status === 'partial' ? `<a class="btn sm" href="${resultsPage[j.job_type]}">View results</a>` : ''}
          </div></td></tr>`;
      }
      return r;
    };

    const draw = () => {
      const host = $('#jb-list', root);
      if (!L.jobs.length) { host.innerHTML = emptyState({ title: L.status ? 'No jobs with this status' : 'No analyses yet', text: L.status ? 'Try another status.' : 'Start your first analysis to build the overview, bed utilisation and unusual workload results.', ico: 'jobs', action: L.status ? '' : '<button class="btn primary" id="jb-empty-new">New analysis</button>' }); $('#jb-empty-new', root)?.addEventListener('click', () => toggleNew(true)); $('#jb-more', root).hidden = true; return; }
      host.innerHTML = `<table class="tbl"><thead><tr><th>Analysis</th><th>Status</th><th style="width:20%">Progress</th><th>Submitted</th><th class="r">Duration</th><th class="r">Events</th><th aria-hidden="true"></th></tr></thead><tbody>${L.jobs.map(row).join('')}</tbody></table>`;
      $('#jb-more', root).hidden = !L.next;
      const toggle = (tr) => { const id = tr.dataset.id; L.open.has(id) ? L.open.delete(id) : L.open.add(id); draw(); $(`tr.click[data-id="${id}"]`, root)?.focus(); };
      $$('tr.click', host).forEach((tr) => {
        tr.addEventListener('click', () => toggle(tr));
        tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(tr); } });
      });
      $$('[data-act]', host).forEach((b) => b.addEventListener('click', async (e) => {
        e.stopPropagation(); b.disabled = true;
        try {
          if (b.dataset.act === 'cancel') { const j = await api(`/jobs/${b.dataset.id}/cancel`, { method: 'POST' }); L.jobs = L.jobs.map((x) => (x.job_id === j.job_id ? j : x)); toast('Job cancelled'); }
          else { const a = await api(`/jobs/${b.dataset.id}/rerun`, { method: 'POST' }); const j = await api(`/jobs/${a.job_id}`); L.jobs.unshift(j); L.open.add(j.job_id); toast('Analysis started'); }
          draw(); poll();
        } catch (err) { b.disabled = false; toast(friendly(err)); }
      }));
    };

    const load = async (more = false) => {
      try {
        const r = await api(`/jobs?limit=20${L.status ? `&status=${L.status}` : ''}${more && L.next ? `&cursor=${encodeURIComponent(L.next)}` : ''}`);
        if (!ctx.current()) return;
        L.jobs = more ? [...L.jobs, ...r.data] : r.data; L.next = r.next_cursor; draw(); poll();
      } catch (e) { if (ctx.current()) $('#jb-list', root).innerHTML = callout('bad', `We could not load jobs. ${esc(friendly(e))}`); }
    };

    /* while anything is queued or running, refresh just those jobs */
    const poll = () => {
      clearInterval(L.timer);
      if (!L.jobs.some(isActive)) return;
      L.timer = setInterval(async () => {
        if (!ctx.current()) return clearInterval(L.timer);
        const active = L.jobs.filter(isActive);
        if (!active.length) { clearInterval(L.timer); return; }
        try {
          const fresh = await Promise.all(active.map((j) => api(`/jobs/${j.job_id}`)));
          const m = new Map(fresh.map((j) => [j.job_id, j]));
          L.jobs = L.jobs.map((j) => m.get(j.job_id) ?? j); draw();
          if (!L.jobs.some(isActive)) clearInterval(L.timer);
        } catch { /* transient: try again next tick */ }
      }, 1500);
    };

    const toggleNew = (show) => {
      $('#jb-new', root).hidden = !show; $('#jb-new-btn', root).setAttribute('aria-expanded', String(show));
      if (show) $('#jb-type', root).focus();
    };
    $('#jb-new-btn', root).addEventListener('click', () => toggleNew($('#jb-new', root).hidden));
    $('#jb-cancel', root).addEventListener('click', () => toggleNew(false));
    $('#jb-status', root).addEventListener('change', (e) => { L.status = e.target.value; load(); });
    $('#jb-more', root).addEventListener('click', () => load(true));
    $('#jb-form', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const go = $('#jb-go', root), msg = $('#jb-msg', root);
      msg.innerHTML = '';
      if ($('#jb-to', root).value < $('#jb-from', root).value) { msg.innerHTML = callout('warn', 'The end date must be on or after the start date.'); return; }
      go.disabled = true;
      try {
        const dept = $('#jb-dept', root).value;
        const a = await api('/jobs', { method: 'POST', body: { job_type: $('#jb-type', root).value, start_date: $('#jb-from', root).value, end_date: $('#jb-to', root).value, ...(dept ? { departments: [+dept] } : {}) } });
        const j = await api(`/jobs/${a.job_id}`);
        L.jobs.unshift(j); L.open.add(j.job_id); toggleNew(false); draw(); poll(); toast('Analysis started');
      } catch (err) {
        msg.innerHTML = callout('bad', esc(err.status === 503 ? 'The analysis queue is not reachable right now. New events are still being recorded safely. Please try again in a minute.' : friendly(err)));
      } finally { go.disabled = false; }
    });

    await load();
    return () => clearInterval(L.timer);
  },
};
