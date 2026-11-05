import { api, isAnalyst, friendly } from '../api.js';
import { $, esc, n0, pct, stayWord, dFull, dShort, dMonth, dWeekday, parseDay, ago, duration, badge, STATUS, JOB_TYPE, skMetrics, skBlock, skRows, emptyState, callout, icon } from '../util.js';
import { state, currentWindow, previousWindow, getSummary, getAnomalies, deptName, bedUse, countBySeverity, vsPrevious, occupied, bedsOf, freeBeds } from '../data.js';
import { lineChart, rows, COLOR } from '../charts.js';
import { head, filterTools, bindFilters, anomalyList, rangeText, changeText } from '../ui.js';

export default {
  async render(root, ctx) {
    if (!state.range) {
      root.innerHTML = head('Overview', 'How the hospital has been running.') + emptyState({
        title: 'No results yet',
        text: 'Run your first analysis and the overview fills in with admissions, bed use and unusual workloads.',
        action: '<a class="btn primary" href="#/jobs">Run an analysis</a>',
      });
      return;
    }
    const w = currentWindow();
    root.innerHTML = head('Overview', `Between <b>${rangeText(w)}</b>.`, filterTools()) + `
      <section class="section" id="ov-metrics">${skMetrics()}</section>
      <section class="section cols c-8-4">
        <div><div class="sec-head"><h2>Admissions</h2><p id="ov-trend-note"></p></div><div class="chart" id="ov-trend">${skBlock('15rem')}</div></div>
        <div><div class="sec-head"><h2>By department</h2><p>Patients admitted</p></div><ul class="rows" id="ov-depts">${skRows(6)}</ul></div>
      </section>
      <section class="section cols c-5-7">
        <div><div class="sec-head"><h2>Bed utilisation</h2><p>Average share of beds in use</p></div><ul class="rows" id="ov-beds">${skRows(6)}</ul>
          <p class="slate" style="margin-top:.8rem;font-size:12.5px">The tick marks the busiest hour; the dashed line is 90% full.</p></div>
        <div><div class="sec-head"><h2>Unusual workloads</h2><p><a href="#/anomalies">View all</a></p></div><ul class="events" id="ov-notes">${skRows(4)}</ul></div>
      </section>
      <section class="section" id="ov-jobs" hidden>
        <div class="sec-head"><h2>Recent analyses</h2><p><a href="#/jobs">All jobs</a></p></div><div id="ov-jobs-body"></div>
      </section>`;
    bindFilters(root, ctx.rerender);

    const pw = state.period === 'all' ? null : previousWindow(w);
    const [sum, prev, notes] = await Promise.allSettled([getSummary(w), pw ? getSummary(pw) : Promise.resolve(null), getAnomalies(w)]);
    if (!ctx.current()) return;

    /* — key figures — */
    const m = $('#ov-metrics', root);
    if (sum.status !== 'fulfilled') {
      m.innerHTML = callout('bad', `We could not load the overview. ${esc(friendly(sum.reason))}`);
    } else {
      const s = sum.value, p = prev.value;
      const use = bedUse(s.departments), usePrev = p ? bedUse(p.departments) : null;
      const over90 = s.departments.reduce((a, d) => a + d.hours_over_90, 0);
      const sev = notes.status === 'fulfilled' ? countBySeverity(notes.value) : null;
      const urgent = sev ? sev.critical + sev.high : null;
      const vs = vsPrevious();
      const cap = s.departments.reduce((a, d) => a + (d.bed_use_avg != null ? d.bed_capacity : 0), 0);
      const ch = p ? changeText(s.totals.admissions, p.totals.admissions) : null;
      const stayCh = p && s.totals.avg_stay_hours && p.totals.avg_stay_hours ? changeText(s.totals.avg_stay_hours, p.totals.avg_stay_hours) : null;
      m.innerHTML = `<div class="metrics">
        <div class="metric"><p class="k">Patients admitted</p><p class="v num">${n0(s.totals.admissions)}</p><p class="d">${ch ? `${ch} ${vs}` : `${s.days} days`}</p></div>
        <div class="metric"><p class="k">Average stay</p><p class="v num">${esc(stayWord(s.totals.avg_stay_hours))}</p><p class="d">${stayCh ? `${stayCh} ${vs}` : 'per patient'}</p></div>
        <div class="metric"><p class="k">Beds in use (average)</p><p class="v num">${use != null ? `${n0(occupied(use, cap))} of ${n0(cap)}` : '—'}</p><p class="d ${over90 ? 'warn' : ''}">${use != null ? `${pct(use)} · ${n0(freeBeds(use, cap))} free` : ''}</p>${usePrev != null ? `<p class="d">${use >= usePrev ? '↑' : '↓'} ${Math.abs(Math.round((use - usePrev) * 100))} pts ${vs}</p>` : ''}</div>
        <div class="metric"><p class="k">Unusual workloads</p><p class="v num">${urgent == null ? '—' : n0(urgent)}</p><p class="d ${sev?.critical ? 'bad' : ''}">${sev ? `${sev.critical} critical · ${sev.high} high · ${sev.moderate} moderate` : ''}</p></div>
      </div>`;

      /* — admissions trend — */
      const unit = { day: 'day', week: 'week', month: 'month' }[s.granularity];
      $('#ov-trend-note', root).textContent = `One point per ${unit}`;
      const shown = lineChart($('#ov-trend', root), [{ name: 'Admissions', color: COLOR.blue, area: true, points: s.trend.map((t) => ({ x: parseDay(t.period).getTime(), y: t.admissions })) }], {
        yFmt: (v, tip) => (tip ? `${n0(v)} admitted` : n0(v)),
        xFmt: (x) => (s.days > 200 ? dMonth(new Date(x)) : dShort(new Date(x))),
        tipTitle: (x) => (s.granularity === 'week' ? `Week of ${dShort(new Date(x))}` : s.granularity === 'month' ? dMonth(new Date(x)) : dWeekday(new Date(x))),
        label: 'Patients admitted over time', height: 330,
      });
      if (!shown) $('#ov-trend', root).innerHTML = emptyState({ title: 'No admissions in this period', text: 'Try a longer period.', small: true });

      /* — departments — */
      const byAdm = [...s.departments].sort((a, b) => b.admissions - a.admissions);
      const top = byAdm[0]?.admissions || 1;
      rows($('#ov-depts', root), byAdm.map((d) => ({ name: d.name, width: (d.admissions / top) * 100, value: n0(d.admissions) })));

      /* — bed utilisation — */
      const byUse = s.departments.filter((d) => d.bed_use_avg != null).sort((a, b) => b.bed_use_avg - a.bed_use_avg);
      if (!byUse.length) $('#ov-beds', root).innerHTML = `<li><span class="slate">No bed data for this period.</span></li>`;
      else rows($('#ov-beds', root), byUse.map((d) => ({
        name: d.name, width: Math.min(d.bed_use_avg, 1) * 100, value: `${bedsOf(d.bed_use_avg, d.bed_capacity)} · ${pct(d.bed_use_avg)}`, marker: d.bed_use_peak, ref: 0.9,
        tone: d.bed_use_avg >= 0.85 ? 'amber' : '',
      })));
    }

    /* — unusual workloads — */
    const ul = $('#ov-notes', root);
    if (notes.status !== 'fulfilled') ul.innerHTML = `<li><span class="slate">${esc(friendly(notes.reason))}</span></li>`;
    else if (!notes.value.length) ul.innerHTML = '';
    if (notes.status === 'fulfilled') {
      if (!notes.value.length) ul.parentElement.insertAdjacentHTML('beforeend', emptyState({ title: 'Nothing out of the ordinary', text: 'No unusual workload periods in this time frame.', ico: 'check', small: true }));
      else {
        const ranked = [...notes.value].sort((a, b) => (b.severity_label === 'critical') - (a.severity_label === 'critical') || (b.severity_label === 'high') - (a.severity_label === 'high') || b.window_start.localeCompare(a.window_start));
        anomalyList(ul, ranked.slice(0, 5), deptName);
      }
    }

    /* — recent analyses (analysts and admins) — */
    if (isAnalyst()) {
      try {
        const j = await api('/jobs?limit=4');
        if (!ctx.current()) return;
        if (j.data.length) {
          $('#ov-jobs', root).hidden = false;
          $('#ov-jobs-body', root).innerHTML = `<div class="tbl-wrap"><table class="tbl"><tbody>${j.data.map((x) => `<tr>
            <td class="strong">${esc(JOB_TYPE[x.job_type] ?? x.job_type)}<span class="sub">${esc(dFull(x.params.start_date))} – ${esc(dFull(x.params.end_date))}</span></td>
            <td>${badge(x.status === 'running' ? 'run' : x.status === 'completed' ? 'ok' : x.status === 'failed' ? 'crit' : x.status === 'partial' ? 'high' : 'mod', STATUS[x.status])}</td>
            <td class="slate">${x.events_processed != null ? `${n0(x.events_processed)} events` : ''}</td>
            <td class="r slate">${x.duration_seconds != null ? duration(x.duration_seconds) : ''}</td>
            <td class="r slate">${esc(ago(x.created_at))}</td></tr>`).join('')}</tbody></table></div>`;
        }
      } catch { /* the section simply stays hidden */ }
    }
  },
};
