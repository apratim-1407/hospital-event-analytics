import { friendly } from '../api.js';
import { $, $$, esc, n0, n1, stayWord, dShort, dMonth, dWeekday, dFull, parseDay, hourWord, SOURCE, skBlock, skRows, skMetrics, emptyState, callout, icon, addDays, diffDays } from '../util.js';
import { state, currentWindow, previousWindow, getSummary, fetchAll, deptQuery, periodLabel } from '../data.js';
import { lineChart, rows, stack, COLOR } from '../charts.js';
import { head, filterTools, bindFilters, rangeText } from '../ui.js';

const SOURCE_COLOR = { emergency: '#2563EB', referral: '#60A5FA', transfer: '#94A3B8', scheduled: '#C7D2E3' };
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
let compare = false;
let sort = { key: 'admissions', dir: -1 };

export default {
  async render(root, ctx) {
    if (!state.range) { root.innerHTML = head('Admission patterns', '') + emptyState({ title: 'No results yet', text: 'Run an analysis to see admission patterns.', action: '<a class="btn primary" href="#/jobs">Run an analysis</a>' }); return; }
    const w = currentWindow();
    const pw = state.period === 'all' ? null : previousWindow(w);
    if (!pw) compare = false;
    const tools = `${filterTools()}<label class="check" title="${pw ? '' : 'Not enough earlier data to compare'}"><input type="checkbox" id="cmp" ${compare ? 'checked' : ''} ${pw ? '' : 'disabled'}> Compare with previous period</label>`;
    root.innerHTML = head('Admission patterns', `How many patients were admitted, when, and through which door, between <b>${rangeText(w)}</b>.`, tools) + `
      <section class="section" id="ad-metrics">${skMetrics()}</section>
      <section class="section"><div class="sec-head"><h2>Admissions over time</h2><p id="ad-note"></p></div><div class="chart" id="ad-trend">${skBlock('16rem')}</div><div class="legend" id="ad-legend"></div></section>
      <section class="section cols c-6-6">
        <div><div class="sec-head"><h2>Busiest days of the week</h2><p>Average patients admitted per day</p></div><ul class="rows" id="ad-dow">${skRows(7)}</ul></div>
        <div><div class="sec-head"><h2>How patients arrive</h2><p>Share of admissions by source</p></div><div id="ad-source">${skRows(2)}</div></div>
      </section>
      <section class="section"><div class="sec-head"><h2>By department</h2><p>Click a heading to sort</p></div><div class="tbl-wrap" id="ad-table">${skRows(6)}</div></section>`;
    bindFilters(root, ctx.rerender);
    $('#cmp', root)?.addEventListener('change', (e) => { compare = e.target.checked; ctx.rerender(); });

    const [sum, prev, adm] = await Promise.allSettled([
      getSummary(w), compare && pw ? getSummary(pw) : Promise.resolve(null),
      fetchAll(`/analytics/admissions?start_date=${w.start}&end_date=${w.end}${deptQuery()}`, { limit: 1000, maxPages: 12 }),
    ]);
    if (!ctx.current()) return;
    if (sum.status !== 'fulfilled') { $('#ad-metrics', root).innerHTML = callout('bad', `We could not load admissions. ${esc(friendly(sum.reason))}`); return; }
    const s = sum.value, p = prev.value;

    /* key figures */
    const days = s.days;
    const peak = s.busiest_day;
    $('#ad-metrics', root).innerHTML = `<div class="metrics">
      <div class="metric"><p class="k">Patients admitted</p><p class="v num">${n0(s.totals.admissions)}</p><p class="d">${n0(s.totals.admissions / days)} a day on average</p></div>
      <div class="metric"><p class="k">Discharged</p><p class="v num">${n0(s.totals.discharges)}</p><p class="d">in the same period</p></div>
      <div class="metric"><p class="k">Busiest day</p><p class="v num">${peak ? dShort(peak.date) : '—'}</p><p class="d">${peak ? `${n0(peak.admissions)} admissions` : ''}</p></div>
      <div class="metric"><p class="k">Average stay</p><p class="v num">${esc(stayWord(s.totals.avg_stay_hours))}</p><p class="d">${s.totals.avg_severity ? `severity ${n1(s.totals.avg_severity)} of 5` : ''}</p></div></div>`;

    /* trend (+ previous period overlay, shifted onto the current dates) */
    const series = [{ name: 'This period', color: COLOR.blue, area: !p, points: s.trend.map((t) => ({ x: parseDay(t.period).getTime(), y: t.admissions })) }];
    if (p) {
      const shift = diffDays(p.start, s.start) * 86400000;
      series.push({ name: 'Previous period', color: COLOR.grey, dash: true, points: p.trend.map((t) => ({ x: parseDay(t.period).getTime() + shift, y: t.admissions })) });
    }
    $('#ad-note', root).textContent = `One point per ${s.granularity}`;
    const ok = lineChart($('#ad-trend', root), series, {
      yFmt: (v, tip) => (tip ? `${n0(v)} admitted` : n0(v)), xFmt: (x) => (days > 200 ? dMonth(new Date(x)) : dShort(new Date(x))),
      tipTitle: (x) => (s.granularity === 'week' ? `Week of ${dShort(new Date(x))}` : s.granularity === 'month' ? dMonth(new Date(x)) : dWeekday(new Date(x))),
      label: 'Patients admitted over time', height: 280,
    });
    if (!ok) $('#ad-trend', root).innerHTML = emptyState({ title: 'No admissions in this period', text: 'Try a longer period.', small: true });
    $('#ad-legend', root).innerHTML = ok ? `<span><i></i>This period</span>${p ? '<span><i class="dash"></i>Previous period</span>' : ''}` : '';

    /* weekday rhythm + source mix: computed from the daily rows */
    if (adm.status !== 'fulfilled') {
      $('#ad-dow', root).innerHTML = `<li><span class="slate">${esc(friendly(adm.reason))}</span></li>`; $('#ad-source', root).innerHTML = '';
    } else {
      const perDay = new Map(), src = {};
      for (const r of adm.value.rows) {
        perDay.set(r.metric_date, (perDay.get(r.metric_date) ?? 0) + r.admission_count);
        for (const [k, v] of Object.entries(r.source_breakdown ?? {})) src[k] = (src[k] ?? 0) + v;
      }
      const sum7 = Array(7).fill(0), cnt7 = Array(7).fill(0);
      for (const [d, n] of perDay) { const wd = (parseDay(d).getUTCDay() + 6) % 7; sum7[wd] += n; cnt7[wd] += 1; }
      const avg7 = sum7.map((t, i) => (cnt7[i] ? t / cnt7[i] : 0));
      const hi = Math.max(...avg7, 1);
      rows($('#ad-dow', root), avg7.map((v, i) => ({ name: WEEKDAYS[i], width: (v / hi) * 100, value: n0(v), tone: v === Math.max(...avg7) ? '' : '' })));
      stack($('#ad-source', root), Object.entries(src).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: SOURCE[k] ?? k, value: v, color: SOURCE_COLOR[k] ?? COLOR.grey })));
      if (!adm.value.complete) $('#ad-source', root).insertAdjacentHTML('beforeend', `<p class="slate" style="margin-top:.8rem;font-size:12.5px">Based on the first ${n0(adm.value.rows.length)} daily records.</p>`);
    }

    /* department table */
    const table = () => {
      const cols = [['name', 'Department'], ['admissions', 'Admitted'], ['avg_stay_hours', 'Average stay'], ['avg_severity', 'Severity'], ['busiest_hour', 'Busiest hour'], ['peak', 'Busiest day']];
      const list = [...s.departments].sort((a, b) => {
        const f = (d) => (sort.key === 'name' ? d.name : sort.key === 'peak' ? d.busiest_day?.admissions ?? 0 : d[sort.key] ?? -1);
        return (f(a) > f(b) ? 1 : f(a) < f(b) ? -1 : 0) * sort.dir;
      });
      const top = Math.max(...s.departments.map((d) => d.admissions), 1);
      $('#ad-table', root).innerHTML = `<table class="tbl"><thead><tr>${cols.map(([k, l]) => `<th class="${k === 'name' ? '' : 'r'}" aria-sort="${sort.key === k ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none'}"><button type="button" data-sort="${k}">${l}${sort.key === k ? ` ${sort.dir > 0 ? '↑' : '↓'}` : ''}</button></th>`).join('')}</tr></thead>
        <tbody>${list.map((d) => `<tr><td class="strong">${esc(d.name)}</td>
          <td class="r num"><span style="display:inline-flex;align-items:center;gap:.6rem;justify-content:flex-end"><span class="track" style="width:4.5rem;height:4px"><i style="width:${(d.admissions / top) * 100}%"></i></span>${n0(d.admissions)}</span></td>
          <td class="r num">${esc(stayWord(d.avg_stay_hours))}</td><td class="r num">${d.avg_severity ? n1(d.avg_severity) : '—'}</td>
          <td class="r num">${d.busiest_hour != null ? hourWord(d.busiest_hour) : '—'}</td>
          <td class="r num">${d.busiest_day ? `${dShort(d.busiest_day.date)} <span class="slate">(${n0(d.busiest_day.admissions)})</span>` : '—'}</td></tr>`).join('')}</tbody></table>`;
      $$('[data-sort]', $('#ad-table', root)).forEach((b) => b.addEventListener('click', () => { sort = { key: b.dataset.sort, dir: sort.key === b.dataset.sort ? -sort.dir : (b.dataset.sort === 'name' ? 1 : -1) }; table(); }));
    };
    table();
  },
};
