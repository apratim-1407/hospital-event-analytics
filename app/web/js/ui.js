/* Shared interface pieces: page header, period/department filters, anomaly wording. */
import { $, $$, esc, dFull, dShort, dWeekday, hourWord, isoDay, n0, n1, pct, signed, SEVERITY, METRIC, badge, icon } from './util.js';
import { PERIODS, state } from './data.js';

export function head(title, description, tools = '') {
  return `<header class="head"><div><h1>${esc(title)}</h1>${description ? `<p>${description}</p>` : ''}</div><div class="tools">${tools}</div></header>`;
}

/** Period segmented control + department select. Pass what to show. */
export function filterTools({ period = true, dept = true } = {}) {
  const per = period ? `<div class="seg" role="group" aria-label="Time period" data-f="period">${PERIODS.map(([k, l]) => `<button type="button" data-v="${k}" aria-pressed="${state.period === k}">${l}</button>`).join('')}</div>` : '';
  const dep = dept ? `<label class="sr" for="f-dept">Department</label><select id="f-dept" data-f="dept"><option value="">All departments</option>${state.depts.map((d) => `<option value="${d.department_id}"${String(d.department_id) === String(state.dept) ? ' selected' : ''}>${esc(d.name)}</option>`).join('')}</select>` : '';
  return per + dep;
}

/** Wire the controls from filterTools(); `onChange` is called after the shared state is updated. */
export function bindFilters(root, onChange) {
  $$('[data-f="period"]', root).forEach((seg) => seg.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-v]');
    if (!b) return;
    state.period = b.dataset.v;
    $$('button', seg).forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    onChange();
  }));
  $$('[data-f="dept"]', root).forEach((sel) => sel.addEventListener('change', () => { state.dept = sel.value; onChange(); }));
}

/** Words for one anomaly window: where, when, what, and how far from normal. */
export function describe(a) {
  const s = new Date(a.window_start), e = new Date(a.window_end);
  const daily = a.metric_name === 'daily_admissions';
  let when;
  if (daily) {
    const last = new Date(e.getTime() - 86400000);
    when = last <= s ? dWeekday(s) : `${dShort(s)} – ${dFull(last)}`;
  } else if (isoDay(s) === isoDay(new Date(e.getTime() - 1))) {
    when = `${dWeekday(s)}, ${hourWord(s.getUTCHours())} – ${hourWord(e.getUTCHours())}`;
  } else {
    when = `${dShort(s)}, ${hourWord(s.getUTCHours())} – ${dShort(e)}, ${hourWord(e.getUTCHours())}`;
  }
  const o = a.observed_value, x = a.expected_value, up = a.z_score >= 0;
  let detail, magnitude;
  if (a.metric_name === 'occupancy_rate') {
    detail = `${pct(o)} of beds in use, against ${pct(x)} expected${o >= 1 ? ' (over capacity)' : ''}`;
    magnitude = signed(Math.round((o - x) * 100), ' pts');
  } else {
    const unit = daily ? 'a day' : 'an hour';
    detail = `${n1(o)} ${unit}, against ${n1(x)} expected`;
    magnitude = x > 0 ? signed(Math.round(((o - x) / x) * 100), '%') : '';
  }
  return { when, detail, magnitude, up, metric: METRIC[a.metric_name] ?? a.metric_name, sev: SEVERITY[a.severity_label] ?? SEVERITY.moderate };
}

/** Compact list used on the overview. */
export function anomalyList(ul, list, deptName) {
  ul.innerHTML = list.map((a) => {
    const d = describe(a);
    return `<li><span class="sev ${d.sev.cls}" title="${d.sev.label}"></span>
      <div><p class="t">${esc(deptName(a.department_id))} <span>· ${esc(d.metric)}</span></p><p class="s">${esc(d.when)}. ${esc(d.detail)}.</p></div>
      <span class="m ${d.up ? 'up' : 'down'}">${esc(d.magnitude)}<br>${badge(d.sev.cls, d.sev.label)}</span></li>`;
  }).join('');
}

export const link = (href, text) => `<a href="${href}">${esc(text)}</a>`;
export const rangeText = (w) => `${dFull(w.start)} and ${dFull(w.end)}`; // always used after "between"
export const changeText = (now, before) => {
  if (!before) return null;
  const c = ((now - before) / before) * 100;
  return `${c >= 0 ? '↑' : '↓'} ${Math.abs(c).toFixed(Math.abs(c) < 10 ? 1 : 0)}%`;
};
export { icon, n0 };
