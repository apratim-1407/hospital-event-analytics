import { api, isAnalyst, friendly } from '../api.js';
import { $, esc, n0, n1, pct, stayWord, dFull, hourWord, callout, icon, toast, skRows, badge, JOB_TYPE, STATUS, METRIC } from '../util.js';
import { state, currentWindow, getSummary, getAnomalies, fetchAll, deptName, deptQuery, bedUse, countBySeverity } from '../data.js';
import { head, filterTools, bindFilters, rangeText } from '../ui.js';

let dataset = 'admissions';
const csvCell = (v) => { const s = v == null ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
const toCsv = (cols, rows) => [cols.map(csvCell).join(','), ...rows.map((r) => r.map(csvCell).join(','))].join('\n');
function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* Each export reads existing endpoints only and shapes the rows for a spreadsheet. */
const DATASETS = {
  admissions: {
    label: 'Admissions by day and department', note: 'One row per department per day.',
    async build(w, progress) {
      const { rows, complete } = await fetchAll(`/analytics/admissions?start_date=${w.start}&end_date=${w.end}${deptQuery()}`, { limit: 1000, maxPages: 40, onPage: progress });
      return { complete, csv: toCsv(['date', 'department', 'admissions', 'discharges', 'transfers', 'average_severity', 'average_stay_hours', 'busiest_hour'],
        rows.map((r) => [r.metric_date, deptName(r.department_id), r.admission_count, r.discharge_count, r.transfer_count, r.avg_severity, r.avg_length_of_stay_hours, r.peak_hour])), count: rows.length };
    },
  },
  resources: {
    label: 'Bed utilisation by hour', note: 'One row per department per hour. Large for long periods: choose a department or a shorter period.',
    async build(w, progress) {
      const { rows, complete } = await fetchAll(`/analytics/resources?start_date=${w.start}&end_date=${w.end}${deptQuery()}`, { limit: 2000, maxPages: 30, onPage: progress });
      return { complete, csv: toCsv(['date', 'hour', 'department', 'beds_occupied', 'bed_capacity', 'utilisation_percent', 'staff_per_patient'],
        rows.map((r) => [r.metric_date, r.metric_hour, deptName(r.department_id), r.occupied_beds, r.bed_capacity, (r.occupancy_rate * 100).toFixed(1), r.staff_to_patient_ratio])), count: rows.length };
    },
  },
  anomalies: {
    label: 'Unusual workloads', note: 'One row per unusual period, with how far it was from normal.',
    async build(w) {
      const rows = await getAnomalies(w);
      return { complete: true, count: rows.length, csv: toCsv(['start_utc', 'end_utc', 'department', 'measure', 'direction', 'severity', 'observed', 'expected', 'distance_from_normal'],
        rows.map((a) => [a.window_start, a.window_end, deptName(a.department_id), METRIC[a.metric_name], a.direction, a.severity_label, a.observed_value, a.expected_value, a.z_score])) };
    },
  },
  jobs: {
    label: 'Analytics jobs', note: 'The history of analyses. Not filtered by period or department.', needsAnalyst: true,
    async build(_w, progress) {
      const { rows, complete } = await fetchAll('/jobs?', { limit: 200, maxPages: 20, onPage: progress });
      return { complete, count: rows.length, csv: toCsv(['job_id', 'analysis', 'status', 'from', 'to', 'submitted_utc', 'finished_utc', 'duration_seconds', 'events_processed', 'parts_total', 'parts_completed'],
        rows.map((j) => [j.job_id, JOB_TYPE[j.job_type], STATUS[j.status], j.params.start_date, j.params.end_date, j.created_at, j.finished_at, j.duration_seconds, j.events_processed, j.partitions_total, j.partitions_completed])) };
    },
  },
};

export default {
  async render(root, ctx) {
    if (!state.range) { root.innerHTML = head('Reports', '') + callout('info', 'Run an analysis first, then reports and exports will be available here.'); return; }
    const w = currentWindow();
    const options = Object.entries(DATASETS).filter(([, d]) => !d.needsAnalyst || isAnalyst());
    if (!options.some(([k]) => k === dataset)) dataset = 'admissions';
    root.innerHTML = head('Reports', 'Take the results with you: download the data for a spreadsheet, or print a summary of the period.', filterTools()) + `
      <section class="section"><div class="sec-head"><h2>Export data</h2><p id="rp-note"></p></div>
        <form class="form" id="rp-form"><label class="field"><span>What to export</span><select id="rp-set" style="min-width:18rem">${options.map(([k, d]) => `<option value="${k}"${k === dataset ? ' selected' : ''}>${d.label}</option>`).join('')}</select></label>
          <button class="btn primary" type="submit" id="rp-go">${icon('download')} Download CSV</button></form>
        <div id="rp-out" style="margin-top:1rem" aria-live="polite"></div></section>
      <hr class="rule">
      <section class="section" id="rp-summary"><div class="sec-head"><h2>Period summary</h2><p class="noprint"><button class="btn sm" id="rp-print">${icon('print', 14)} Print or save as PDF</button></p></div>${skRows(5)}</section>
      <section class="section" id="rp-measured" hidden></section>`;
    bindFilters(root, ctx.rerender);

    const note = () => { $('#rp-note', root).textContent = DATASETS[dataset].note; };
    note();
    $('#rp-set', root).addEventListener('change', (e) => { dataset = e.target.value; note(); $('#rp-out', root).innerHTML = ''; });
    $('#rp-form', root).addEventListener('submit', async (e) => {
      e.preventDefault();
      const out = $('#rp-out', root), btn = $('#rp-go', root);
      btn.disabled = true; out.innerHTML = callout('info', 'Preparing your file…');
      try {
        const r = await DATASETS[dataset].build(w, (n) => { out.innerHTML = callout('info', `Preparing your file… ${n0(n)} rows so far`); });
        if (!r.count) { out.innerHTML = callout('warn', 'There is nothing to export for these filters.'); return; }
        download(`${dataset}_${w.start}_to_${w.end}.csv`, r.csv);
        out.innerHTML = callout(r.complete ? 'good' : 'warn', r.complete ? `Downloaded ${n0(r.count)} rows.` : `Downloaded the first ${n0(r.count)} rows. The period is large: choose a department or a shorter period to export everything.`);
      } catch (err) { out.innerHTML = callout('bad', `We could not prepare the export. ${esc(friendly(err))}`); }
      finally { btn.disabled = false; }
    });
    $('#rp-print', root)?.addEventListener('click', () => window.print());

    /* printable summary */
    const [sum, notes] = await Promise.allSettled([getSummary(w), getAnomalies(w)]);
    if (!ctx.current()) return;
    const host = $('#rp-summary', root);
    if (sum.status !== 'fulfilled') host.querySelector('.sec-head').insertAdjacentHTML('afterend', callout('bad', esc(friendly(sum.reason))));
    else {
      const s = sum.value, use = bedUse(s.departments), sev = notes.status === 'fulfilled' ? countBySeverity(notes.value) : null;
      host.innerHTML = `<div class="sec-head"><h2>Period summary</h2><p class="noprint"><button class="btn sm" id="rp-print2">${icon('print', 14)} Print or save as PDF</button></p></div>
        <p style="max-width:44rem;font-size:15px">Between <b>${rangeText(w)}</b>${state.dept ? ` in <b>${esc(deptName(state.dept))}</b>` : ''}, <b>${n0(s.totals.admissions)}</b> patients were admitted and <b>${n0(s.totals.discharges)}</b> discharged, with an average stay of <b>${esc(stayWord(s.totals.avg_stay_hours))}</b>. ${use != null ? `Beds were <b>${pct(use)}</b> in use on average. ` : ''}${sev ? `We found <b>${n0(sev.critical + sev.high + sev.moderate)}</b> unusual workload periods, <b>${sev.critical}</b> of them critical.` : ''}</p>
        <div class="tbl-wrap" style="margin-top:1.4rem"><table class="tbl"><thead><tr><th>Department</th><th class="r">Admitted</th><th class="r">Average stay</th><th class="r">Beds in use</th><th class="r">Busiest</th><th class="r">Hours at 90%+</th></tr></thead><tbody>${s.departments.map((d) => `<tr><td class="strong">${esc(d.name)}</td><td class="r num">${n0(d.admissions)}</td><td class="r num">${esc(stayWord(d.avg_stay_hours))}</td><td class="r num">${d.bed_use_avg != null ? pct(d.bed_use_avg) : '—'}</td><td class="r num">${d.bed_use_peak != null ? pct(d.bed_use_peak) : '—'}</td><td class="r num">${n0(d.hours_over_90)}</td></tr>`).join('')}</tbody></table></div>
        <p class="slate" style="margin-top:1rem;font-size:12.5px">Generated ${esc(dFull(new Date()))}. Demonstration data only.</p>`;
      $('#rp-print2', root)?.addEventListener('click', () => window.print());
    }

    /* measured results: shown only when the server provides them */
    try {
      // Ask the API what it offers first, so a server without this endpoint costs no failing request.
      const spec = await fetch('/openapi.json').then((r) => r.json());
      if (!spec.paths?.['/api/v1/reports/benchmarks']) return;
      const m = await api('/reports/benchmarks');
      if (!ctx.current() || !m.available) return;
      const el = $('#rp-measured', root); el.hidden = false;
      const lt = m.load_test ?? [], fx = m.failure_experiment;
      el.innerHTML = `<hr class="rule" style="margin:0 0 2.75rem"><div class="sec-head"><h2>Measured system results</h2><p>Recorded by the benchmark and load-test tools.</p></div>
        ${lt.length ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Load level</th><th class="r">Users</th><th class="r">Requests/s</th><th class="r">Median</th><th class="r">95th percentile</th><th class="r">Errors</th></tr></thead><tbody>${lt.map((r) => `<tr><td class="strong">${esc(r.level)}</td><td class="r num">${n0(r.users)}</td><td class="r num">${n1(r.rps)}</td><td class="r num">${n0(r.p50_ms)} ms</td><td class="r num">${n0(r.p95_ms)} ms</td><td class="r num">${r.error_rate_pct}%</td></tr>`).join('')}</tbody></table></div>` : ''}
        ${fx ? `<div style="margin-top:1.4rem">${callout(fx.identical ? 'good' : 'bad', `<b>Worker failure test:</b> a worker was stopped mid-analysis; the job still finished${fx.identical ? ' with identical results' : ', but the results differed'} (${fx.baseline?.duration_seconds}s normally, ${fx.failure_run?.duration_seconds}s with the failure).`)}</div>` : ''}`;
    } catch { /* endpoint not available on this server: leave the section hidden */ }
  },
};
