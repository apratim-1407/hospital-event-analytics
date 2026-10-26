import { api, friendly } from '../api.js';
import { $, esc, n0, pct, signed, hourWord, dShort, dWeekday, addDays, isoDay, parseDay, badge, skBlock, skRows, skMetrics, emptyState, callout } from '../util.js';
import { state, currentWindow, previousWindow, getSummary, getPattern, deptName, vsPrevious, occupied, bedsOf, freeBeds, bedUse } from '../data.js';
import { lineChart, rows, heatmap, COLOR, niceMax } from '../charts.js';
import { head, filterTools, bindFilters, rangeText } from '../ui.js';

/* How each department is classified, from the share of hours at 90%+ full. Shown to the reader. */
function status(d, hours) {
  const share = hours ? d.hours_over_90 / hours : 0;
  if (d.bed_use_peak > 1 || share >= 0.02) return { cls: 'crit', label: d.bed_use_peak > 1 ? 'Over capacity at times' : 'Constrained', rank: 3 };
  if (share > 0 || d.bed_use_avg >= 0.8) return { cls: 'high', label: 'Watch', rank: 2 };
  return { cls: 'ok', label: 'Healthy', rank: 1 };
}
let heatDept = '';

export default {
  async render(root, ctx) {
    if (!state.range) { root.innerHTML = head('Resource utilisation', '') + emptyState({ title: 'No results yet', text: 'Run an analysis to see bed utilisation.', action: '<a class="btn primary" href="#/jobs">Run an analysis</a>' }); return; }
    const w = currentWindow();
    root.innerHTML = head('Resource utilisation', `How full beds have been between <b>${rangeText(w)}</b>, and where capacity is tight.`, filterTools()) + `
      <section class="section" id="rs-metrics">${skMetrics()}</section>
      <section class="section" id="rs-flag" hidden></section>
      <section class="section"><div class="sec-head"><h2>By department</h2><p>Healthy: under 90% full. Watch: some hours at 90%+. Constrained: 2% or more of hours at 90%+.</p></div><div class="tbl-wrap" id="rs-table">${skRows(6)}</div></section>
      <section class="section cols c-6-6">
        <div><div class="sec-head"><h2>Across the day</h2><p id="rs-pattern-note"></p></div><div class="chart" id="rs-pattern">${skBlock('14rem')}</div>
          <div class="legend"><span><i></i>Weekdays</span><span><i class="dash"></i>Weekends</span></div></div>
        <div><div class="sec-head"><h2>Day by day</h2><p id="rs-heat-note"></p></div><div id="rs-heat">${skBlock('14rem')}</div></div>
      </section>`;
    bindFilters(root, ctx.rerender);

    const pw = state.period === 'all' ? null : previousWindow(w);
    const [sum, pat, prev] = await Promise.allSettled([getSummary(w), getPattern(w), pw ? getSummary(pw) : Promise.resolve(null)]);
    if (!ctx.current()) return;
    if (sum.status !== 'fulfilled') { $('#rs-metrics', root).innerHTML = callout('bad', `We could not load utilisation. ${esc(friendly(sum.reason))}`); return; }
    const s = sum.value;
    const hours = s.days * 24;
    const depts = s.departments.filter((d) => d.bed_use_avg != null).map((d) => ({ ...d, st: status(d, hours) }));
    if (!depts.length) { $('#rs-metrics', root).innerHTML = emptyState({ title: 'No bed data for this period', text: 'Run a resource utilisation analysis that covers these dates.', small: true }); return; }

    /* key figures, counts first, each set against the period before */
    const vs = vsPrevious();
    const before = prev.status === 'fulfilled' && prev.value ? new Map(prev.value.departments.map((d) => [d.department_id, d])) : null;
    const cap = depts.reduce((a, d) => a + d.bed_capacity, 0);
    const avg = bedUse(depts);
    const avgBefore = before ? bedUse(prev.value.departments) : null;
    const tightest = [...depts].sort((a, b) => b.bed_use_peak - a.bed_use_peak)[0];
    const over = depts.reduce((a, d) => a + d.hours_over_90, 0);
    const overBefore = before ? [...before.values()].reduce((a, d) => a + d.hours_over_90, 0) : null;
    const flagged = depts.filter((d) => d.st.rank === 3);
    $('#rs-metrics', root).innerHTML = `<div class="metrics">
      <div class="metric"><p class="k">Beds in use (average)</p><p class="v num">${n0(occupied(avg, cap))} of ${n0(cap)}</p><p class="d">${pct(avg)} · ${n0(freeBeds(avg, cap))} free</p>${avgBefore == null ? '' : `<p class="d">${avg >= avgBefore ? '↑' : '↓'} ${Math.abs(Math.round((avg - avgBefore) * 100))} pts ${vs}</p>`}</div>
      <div class="metric"><p class="k">Fullest moment</p><p class="v num">${bedsOf(tightest.bed_use_peak, tightest.bed_capacity)}</p><p class="d">${esc(tightest.name)} · ${pct(tightest.bed_use_peak)}</p></div>
      <div class="metric"><p class="k">Hours at 90% or more</p><p class="v num">${n0(over)}</p><p class="d">department-hours</p>${overBefore == null ? '' : `<p class="d">${over === overBefore ? 'unchanged' : `${over > overBefore ? '↑' : '↓'} ${n0(Math.abs(over - overBefore))}`} ${vs}</p>`}</div>
      <div class="metric"><p class="k">Departments flagged</p><p class="v num">${flagged.length}</p><p class="d ${flagged.length ? 'bad' : 'good'}">${flagged.length ? esc(flagged.map((d) => d.name).join(', ')) : 'none constrained'}</p></div></div>`;
    if (flagged.length) {
      const f = $('#rs-flag', root); f.hidden = false;
      f.innerHTML = callout('warn', `<b>${esc(flagged.map((d) => d.name).join(', '))}</b> ${flagged.length === 1 ? 'was' : 'were'} at 90% or more of beds for ${n0(flagged.reduce((a, d) => a + d.hours_over_90, 0))} hours in this period. Review the day-by-day view below for when.`);
    }

    /* department table: beds free first, with the 90% hours set against the period before */
    const list = [...depts].sort((a, b) => b.st.rank - a.st.rank || b.bed_use_avg - a.bed_use_avg);
    const hoursMove = (d) => {
      const was = before?.get(d.department_id)?.hours_over_90;
      return was == null ? '' : `<span class="sub">${d.hours_over_90 === was ? 'same as before' : `${signed(d.hours_over_90 - was)} vs before`}</span>`;
    };
    $('#rs-table', root).innerHTML = `<table class="tbl"><thead><tr><th>Department</th><th style="width:26%">Average, and busiest hour</th><th class="r">Beds in use, average</th><th class="r">Busiest hour</th><th class="r">Hours at 90%+</th><th>Status</th></tr></thead><tbody>${list.map((d) => `<tr>
      <td class="strong">${esc(d.name)}<span class="sub">${n0(d.bed_capacity)} beds</span></td>
      <td><span class="track"><i class="${d.st.rank === 3 ? 'red' : d.st.rank === 2 ? 'amber' : ''}" style="width:${Math.min(d.bed_use_avg, 1) * 100}%"></i><u style="left:90%"></u><em style="left:calc(${Math.min(d.bed_use_peak, 1) * 100}% - 1px)"></em></span></td>
      <td class="r num">${bedsOf(d.bed_use_avg, d.bed_capacity)}<span class="sub">${pct(d.bed_use_avg)} · ${n0(freeBeds(d.bed_use_avg, d.bed_capacity))} free</span></td>
      <td class="r num">${bedsOf(d.bed_use_peak, d.bed_capacity)}<span class="sub">${pct(d.bed_use_peak)}</span></td>
      <td class="r num">${n0(d.hours_over_90)}${hoursMove(d)}</td><td>${badge(d.st.cls, d.st.label)}</td></tr>`).join('')}</tbody></table>`;

    /* across the day */
    if (pat.status === 'fulfilled') {
      const p = pat.value, mk = (a) => a.map((y, h) => ({ x: h, y }));
      const vals = [...p.weekday, ...p.weekend].filter((v) => v != null);
      const lo = Math.max(0, Math.floor((Math.min(...vals) - 0.04) / 0.05) * 0.05);
      let hi = Math.ceil((Math.max(...vals) + 0.04) / 0.05) * 0.05; if (hi - lo < 0.2) hi = lo + 0.2;
      const peak = p.weekday.reduce((b, v, h) => (v != null && v > (p.weekday[b] ?? -1) ? h : b), 0);
      $('#rs-pattern-note', root).textContent = `Fullest around ${hourWord(peak)} on weekdays`;
      lineChart($('#rs-pattern', root), [
        { name: 'Weekdays', color: COLOR.blue, area: true, points: mk(p.weekday) },
        { name: 'Weekends', color: COLOR.grey, dash: true, points: mk(p.weekend) },
      ], { yMin: lo, yMax: hi, yFmt: (v) => pct(v), xFmt: hourWord, xTicks: [0, 6, 12, 18, 23], tipTitle: (x) => hourWord(x), label: 'Share of beds in use by hour of day', height: 240 });
    } else $('#rs-pattern', root).innerHTML = callout('bad', esc(friendly(pat.reason)));

    /* day-by-day heatmap for one department */
    const hostHeat = $('#rs-heat', root);
    const pick = state.dept || heatDept || String(list[0].department_id);
    const drawHeat = async (id) => {
      heatDept = id;
      const end = w.end, start = isoDay(new Date(Math.max(parseDay(w.start), addDays(parseDay(end), -13))));
      hostHeat.innerHTML = skBlock('14rem');
      try {
        const r = await api(`/analytics/resources?start_date=${start}&end_date=${end}&department_id=${id}&limit=2000`);
        if (!ctx.current()) return;
        const cells = new Map(r.data.map((x) => [`${x.metric_date}|${x.metric_hour}`, x.occupancy_rate]));
        const days = [...new Set(r.data.map((x) => x.metric_date))].sort();
        $('#rs-heat-note', root).innerHTML = `${esc(deptName(id))}, last ${days.length} days ${state.dept ? '' : `· <select id="rs-heat-dept" aria-label="Department for the day-by-day view" style="height:1.7rem;padding:0 1.5rem 0 .4rem">${list.map((d) => `<option value="${d.department_id}"${String(d.department_id) === String(id) ? ' selected' : ''}>${esc(d.name)}</option>`).join('')}</select>`}`;
        $('#rs-heat-dept', root)?.addEventListener('change', (e) => drawHeat(e.target.value));
        if (!days.length) hostHeat.innerHTML = emptyState({ title: 'No hourly data', text: 'Nothing recorded for these days.', small: true });
        else heatmap(hostHeat, days, cells, (d) => dWeekday(d).replace(/ 20\d\d$/, ''));
      } catch (e) { hostHeat.innerHTML = callout('bad', esc(friendly(e))); }
    };
    drawHeat(pick);
  },
};
